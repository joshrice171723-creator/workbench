"""Time a live, isolated assessment replay alongside the scripted route tests."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time

from audited_flow import Runtime, read, run, save
from workflow_job import process_group, terminate_tree

ROOT = Path(__file__).resolve().parent
REQUEST = ("Handle a delayed YES to an appointment reminder. If it could refer to two sent "
           "reminders, send it to staff review and leave both appointments unconfirmed. "
           "A YES with one eligible appointment still confirms that appointment.")


def export(project, ref, dest):
    sha = subprocess.check_output(["git", "-C", str(project), "rev-parse", "--verify", ref + "^{commit}"],
                                  text=True).strip()
    archive = dest.parent / (dest.name + ".tar")
    subprocess.run(["git", "-C", str(project), "archive", "--format=tar", "-o", str(archive), sha], check=True)
    dest.mkdir()
    with tarfile.open(archive) as tar:
        for entry in tar:
            target = (dest / entry.name).resolve()
            if not target.is_relative_to(dest) or not (entry.isfile() or entry.isdir()):
                raise ValueError("Expected a plain-file assessment export")
            tar.extract(entry, dest, filter="data")
    archive.unlink()
    return sha


def verify(config, project):
    argv = config["independent_check"]
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
        raise ValueError("Supply independent_check as an argv array")
    return subprocess.run(argv, cwd=project, stdout=sys.stderr).returncode


def worker(config_path, folder, calibrate=False):
    config = read(config_path)
    source = Path(config["assessment_repo"]).resolve(strict=True)
    candidate = folder / "candidate"
    sha = export(source, "9c71028", candidate)
    if calibrate:
        repaired = folder / "control"
        export(source, "5f2500c", repaired)
        # The external evaluator must distinguish assertion failures (1) from setup errors (>1).
        if verify(config, candidate) != 1 or verify(config, repaired) != 0:
            raise ValueError("The independent checks did not distinguish the assessment controls")
        return {"outcome": "passed", "revision": sha}
    def cli(args):
        p = subprocess.run(["ruby", str(ROOT / "bin/harness"), *args, "--project", str(candidate)],
                           text=True, capture_output=True)
        if p.returncode:
            raise ValueError(p.stderr or p.stdout)
        return json.loads(p.stdout)
    cli(["setup", "init"])
    home = candidate / ".harness"
    settings = read(home / "project.json")
    settings["checks"] = config["checks"]
    settings["execution"]["workflow"] = "audited"
    if "agent" in config:
        settings["agent"] = config["agent"]
    save(home / "settings.json", settings)
    cli(["setup", "configure", "--file", str(home / "settings.json")])
    decisions = dict(scope_confirmed="confirmed", code_changes="allow_scoped", migrations="deny",
                     dependencies="deny", engine_integration="confirmed")
    save(home / "decisions.json", decisions)
    task = cli(["feature", "--action", REQUEST, "--scope", "Inbound appointment reply handling and relevant tests",
                "--file", str(home / "decisions.json")])
    rt = Runtime(candidate, task["task_id"])
    reply = run(rt)
    state = read(rt.folder / "state.json")
    while state["stage"] == "question":
        answer = config.get("answers", {}).get(state["question"])
        if not answer:
            return {"outcome": "blocked", "reason": reply}
        reply = run(rt, answer)
        state = read(rt.folder / "state.json")
    if state["stage"] != "completed":
        return {"outcome": "failed", "reason": reply}
    code = verify(config, candidate)
    return {"outcome": {0: "passed", 1: "failed"}.get(code, "blocked"), "independent_exit": code,
            "task": rt.id, "revision": sha, "returns": state["returns"], "stages": [
                {"stage": row["stage"], "at": row["time"]} for row in state["history"]]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--worker-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    fixture = (args.fixture or args.project / ".harness/quick-bench.json").resolve()
    if args.worker_dir:
        try:
            result = worker(fixture, args.worker_dir, args.calibrate)
        except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            result = {"outcome": "blocked", "reason": str(exc)}
        print(json.dumps(result))
        return
    start = time.monotonic()
    folder = Path(tempfile.mkdtemp(prefix="workbench-quick-"))
    commands = {"live": [sys.executable, __file__, "--fixture", str(fixture), "--worker-dir", str(folder)]}
    if args.calibrate:
        commands["live"].append("--calibrate")
    else:
        commands["scripted"] = [sys.executable, "-m", "unittest", "test_audited_flow.Routes"]
    jobs, results = {}, {}
    try:
        for name, argv in commands.items():
            with (folder / (name + ".out")).open("w") as out, (folder / (name + ".err")).open("w") as err:
                jobs[name] = subprocess.Popen(argv, cwd=ROOT, stdout=out, stderr=err, **process_group())
        while any(p.poll() is None for p in jobs.values()) and time.monotonic() - start < 570:
            time.sleep(.1)
        for name, p in jobs.items():
            if p.poll() is None:
                terminate_tree(p)
                results[name] = {"outcome": "timed_out"}
            elif name == "live" and p.returncode == 0:
                results[name] = read(folder / "live.out")
            else:
                results[name] = {"outcome": "passed" if p.returncode == 0 else "failed"}
    finally:
        for proc in jobs.values():
            terminate_tree(proc)
    results.update(seconds=round(time.monotonic() - start, 2), artifacts=str(folder))
    print(json.dumps(results, indent=2))
    return 0 if all(results[k]["outcome"] == "passed" for k in commands) else 1


if __name__ == "__main__":
    sys.exit(main())
