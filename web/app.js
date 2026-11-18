"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const sections = {
    overview: ["Overview", "Select a project, agree on a task, and use the same workflow in CLI or chat."],
    setup: ["Project setup", "Install the local orchestration layer. Keep the project’s coding harness and configure relevant checks."],
    task: ["New task", "Define the action and scope. Record permissions before an agent changes code."],
    chat: ["Agent chat", "Start a task, record its decisions, or continue work with the coding agent."],
    runs: ["Task runs", "Inspect task records, checks, and reports. Compare scope before comparing results."],
  };
  const modes = { audit: "Inspect and report. Do not change application code.", refactor: "Improve code while preserving accepted behavior.", feature: "Deliver the requested feature within its scope.", build: "Build from separately supplied requirements." };
  const activeStates = new Set(["created", "starting", "running", "cancelling"]);
  let state = null, view = Object.hasOwn(sections, location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  let pollRequest = null, polling = false, pending = false, lastSuccess = null, failed = false, configTab = "guided";
  let wizardStep = 0, draft = { mode: "audit", action: "", scope: "", answers: {} };
  let rawConfig = null, pinDraft = null;
  let setupDraft = null, composeDraft = "", selectedRuns = new Set(), runSearch = "";
  const transcripts = new Map(), helpTurns = [], shownJobErrors = new Set();
  const arr = value => Array.isArray(value) ? value : [];
  const val = (value, fallback = "Not recorded") => value === null || value === undefined || value === "" ? fallback : String(value);
  const projectPath = () => typeof state?.project === "string" ? state.project : state?.project?.path;
  const task = () => state?.active_task;
  const taskStatus = () => task()?.status || task()?.lifecycle?.status || "unknown";
  const jobs = () => arr(state?.jobs);
  const busy = () => pending || jobs().some(j => activeStates.has(j.status || j.state));
  const chatBusy = () => jobs().some(j => (j.type || j.kind || "agent") === "agent" && activeStates.has(j.status || j.state));
  function node(tag, className, text) { const n = document.createElement(tag); if (className) n.className = className; if (text !== undefined) n.textContent = val(text, ""); return n; }
  function add(parent, ...children) { children.filter(Boolean).forEach(child => parent.append(child)); return parent; }
  function btn(text, fn, style = "button", disabled = false) { const n = node("button", style, text); n.type = "button"; n.disabled = disabled; n.addEventListener("click", fn); return n; }
  function pill(value) { const s = val(value, "unknown").toLowerCase(); return node("span", `status ${/^[a-z_-]+$/.test(s) ? s : "unknown"}`, s.replaceAll("_", " ")); }
  function fact(label, value, metric = false) { return add(node("div", "fact"), node("span", "", label), node("strong", metric ? "metric" : "", val(value))); }
  function heading(title, extra) { return add(node("div", "heading-row"), node("h2", "", title), extra); }
  function field(label, input, hint) { const l = node("label", "field"); add(l, document.createTextNode(label), input); if (hint) l.append(node("span", "hint", hint)); return l; }
  function input(value = "", type = "text") { const n = node("input"); n.type = type; n.value = val(value, ""); return n; }
  function textarea(value = "", code = false) { const n = node("textarea", code ? "code" : ""); n.value = val(value, ""); return n; }
  function select(options, value) { const n = node("select"); options.forEach(o => { const v = typeof o === "string" ? o : o.value ?? o.id ?? o.option; n.append(new Option(typeof o === "string" ? o.replaceAll("_", " ") : o.label || o.text || v, v)); }); if (value !== undefined) n.value = value; return n; }
  function empty(title, description, action) { const n = add(node("section", "empty"), node("h2", "", title), node("p", "", description)); if (action) n.append(add(node("div", "buttons"), action)); return n; }
  function notify(message, warning = false) { const n = $("notice"); n.textContent = message; n.className = `notice${warning ? " warning" : ""}`; n.hidden = false; }
  function error(message) { $("errors").textContent = message; $("errors").hidden = false; }
  function clearError() { $("errors").hidden = true; }
  function showRecord(title, content) { $("record-title").textContent = title; $("record-content").replaceChildren(content instanceof Node ? content : node("pre", "output", typeof content === "string" ? content : JSON.stringify(content, null, 2))); if (!$("record-dialog").open) $("record-dialog").showModal(); }
  async function request(url, payload) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), payload ? 120000 : 15000);
    try {
      const response = await fetch(url, { method: payload ? "POST" : "GET", cache: "no-store", signal: controller.signal, headers: payload ? { "Content-Type": "application/json", "X-CSRF-Token": state?.csrf_token || "" } : { Accept: "application/json" }, ...(payload ? { body: JSON.stringify(payload) } : {}) });
      const data = await response.json(); if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`); return data;
    } catch (e) { if (e.name === "AbortError") throw new Error("The request timed out. Refresh the task state before repeating the action."); throw e; }
    finally { clearTimeout(timer); }
  }
  async function mutate(url, payload, options = {}) {
    if (pending) return null;
    if (failed) { error("Refresh the server state before running an action."); return null; }
    pending = true; clearError(); updateHeader();
    try {
      const result = await request(url, payload);
      if (result.exit_code !== undefined && result.exit_code !== 0) { error(result.output || result.response || `The command exited with ${result.exit_code}.`); if (options.output) showRecord(options.title || "Command result", result.output || result); }
      else { if (options.message) notify(options.message); if (options.output) showRecord(options.title || "Command result", result.output || result.response || result); }
      await refresh(true);
      return result;
    } catch (e) { error(e.message); return null; }
    finally { pending = false; updateHeader(); render(); }
  }
  function agreement() {
    const answers = task()?.answers || {};
    const missing = arr(state?.questions).filter(q => q.required && !answers[q.id]);
    return { missing, ready: Boolean(task() && !missing.length && ["active", "running"].includes(taskStatus())) };
  }
  function updateHeader() {
    const path = projectPath(); $("project-name").textContent = state?.project?.label || (path ? path.replaceAll("\\", "/").split("/").at(-1) : "No project selected"); $("project-path").textContent = path || "Choose a project in Project setup.";
    $("server-state").textContent = pending ? "Running command" : failed ? "Disconnected" : lastSuccess ? "Local server connected" : "Connecting"; $("server-state").dataset.state = failed ? "error" : lastSuccess ? "live" : "unknown";
    $("last-refresh").textContent = lastSuccess ? `Last refresh: ${lastSuccess.toLocaleTimeString()}` : "No successful refresh yet";
    $("run-count").textContent = arr(state?.runs).length || "";
    const cap = state?.capabilities?.chat || state?.capabilities || {};
    $("chat-nav-state").textContent = chatBusy() ? "Busy" : cap.available === true ? "Available" : cap.available === false ? "Unavailable" : "Not checked";
    $("footer-status").textContent = task() ? `Task ${task().id} · ${taskStatus()}` : "No task selected";
    $("refresh").disabled = polling || pending;
  }
  function navigate(next) { if (!Object.hasOwn(sections, next)) next = "overview"; view = next; if (location.hash !== `#${next}`) location.hash = next; render(); }
  function render() {
    $("page-title").textContent = sections[view][0]; $("page-description").textContent = sections[view][1];
    document.querySelectorAll("[data-view]").forEach(a => { if (a.dataset.view === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    if (!state) return;
    const builders = { overview, setup, task: taskWizard, chat, runs: runsView };
    $("content").replaceChildren(builders[view]()); $("content").setAttribute("aria-busy", "false"); updateHeader();
  }
  function overview() {
    const root = node("div"), c = node("section", "card pad");
    add(c, heading("Project workflow", pill(state.installed ? "ready" : "pending")), node("p", "", "Use one task record from agreement through checks, repairs, and the final report."));
    const steps = node("div", "steps section-gap");
    const items = [["Select and configure a project", state.installed ? "The orchestration layer is installed. Check the configured commands before use." : "Select an existing project. Install the layer without replacing project instructions.", state.installed ? "ready" : "pending", "setup"], ["Agree on the task", task() ? task().action : "Choose a mode, action, scope, and permissions.", agreement().ready ? "ready" : "pending", "task"], ["Work with the coding agent", "Agent messages use the configured coding harness and the active task agreement.", chatBusy() ? "running" : "pending", "chat"], ["Check and report", "Run relevant baseline and after checks. Finish only when the current task gates pass.", taskStatus() === "completed" ? "completed" : "pending", "runs"]];
    items.forEach(([title, summary, s, target], i) => add(steps, add(node("div", "step"), node("span", "step-number", i + 1), add(node("div"), btn(title, () => navigate(target), "button link"), node("p", "", summary)), pill(s)))); c.append(steps); root.append(c);
    const cols = node("div", "two-col section-gap"), active = node("section", "card pad"), caps = node("section", "card pad");
    add(active, heading("Active task", pill(taskStatus())));
    if (taskStatus() === "unverified") active.append(node("p", "", task().lifecycle.reason));
    if (task()) { add(active, node("h3", "", task().action), node("p", "", task().scope), add(node("div", "fact-grid section-gap"), fact("Mode", task().mode), fact("Task ID", task().id), fact("Unanswered requirements", agreement().missing.length)), add(node("div", "buttons"), btn("Open agent chat", () => navigate("chat")), btn("View task record", () => showRun(task().id), "button"))); }
    else add(active, node("p", "", "No task is selected. Define the task before starting an agent."), add(node("div", "buttons"), btn("Create a task", () => navigate("task"), "button primary", !state.installed)));
    const capability = state.capabilities?.chat || state.capabilities || {};
    add(caps, heading("Agent connection", pill(capability.available === true ? "available" : capability.available === false ? "unavailable" : "unknown")), node("p", "", capability.note || capability.detail || "Check the local Codex CLI before starting chat."), add(node("div", "buttons"), btn("Check agent availability", () => mutate("/api/capabilities", {}, { message: "Agent availability checked.", output: true, title: "Agent availability" }), "button", busy())));
    add(caps, node("p", "help-line", "A detected CLI is not proof of a successful agent turn. The transcript and process result show what ran.")); add(cols, active, caps); root.append(cols);
    const scope = node("section", "card pad section-gap"); add(scope, heading("Verification scope"), node("p", "", "This workbench records actual commands and agent events. A passing command does not establish code quality by itself. Read the task report and its revision-specific evidence.")); root.append(scope); return root;
  }
  function setup() {
    const root = node("div"), project = node("section", "card pad");
    add(project, heading("1. Select a project", state.installed ? pill("installed") : null)); const path = input(projectPath() || ""); path.placeholder = "C:/path/to/project";
    add(project, field("Project directory", path, "Select an existing directory. This does not create or clone an application."), add(node("div", "buttons"), btn("Select project", async () => { const r = await mutate("/api/project", { path: path.value.trim() }, { message: "Project selected." }); if (r) { setupDraft = null; rawConfig = pinDraft = null; render(); } }, "button primary", busy()))); root.append(project);
    if (!projectPath()) return root;
    const install = node("section", "card pad section-gap"); add(install, heading("2. Install and inspect", pill(state.installed ? "installed" : "pending")), node("p", "", "Install the orchestration layer in .harness. Existing project instructions stay in place. Installation refuses to overwrite an existing layer."), add(node("div", "buttons"), btn("Install in this project", async () => { const r = await mutate("/api/setup", { action: "install" }, { message: "The local orchestration layer is installed.", output: true, title: "Installation result" }); if (r) { setupDraft = null; rawConfig = pinDraft = null; render(); } }, "button primary", state.installed || busy()), btn("Inspect project commands", () => mutate("/api/setup", { action: "discover" }, { output: true, title: "Project discovery" }), "button", !state.installed || busy()))); root.append(install);
    if (!state.installed) return root;
    if (!setupDraft) setupDraft = structuredClone(state.config || {});
    const cfg = node("section", "card pad section-gap"); add(cfg, heading("3. Configure the workflow"), node("p", "section-intro", "Set the instructions and checks for this project."));
    const tabs = add(node("div", "config-tabs"), btn("Guided fields", () => { configTab = "guided"; render(); }, `button${configTab === "guided" ? " primary" : ""}`), btn("Full configuration JSON", () => { configTab = "json"; render(); }, `button${configTab === "json" ? " primary" : ""}`)); cfg.append(tabs);
    let readConfig;
    if (configTab === "json") { const json = textarea(rawConfig ?? JSON.stringify(setupDraft, null, 2), true); json.addEventListener("input", () => { rawConfig = json.value; try { const parsed = JSON.parse(json.value); if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) { setupDraft = parsed; pinDraft = null; } } catch {} }); cfg.append(field("Project configuration JSON", json, "The full configuration preserves engine manifests, agent settings, checks, and the benchmark contract.")); readConfig = () => JSON.parse(json.value); }
    else readConfig = guidedConfiguration(cfg);
    cfg.append(add(node("div", "buttons"), btn("Save configuration", async () => { try { const r = await mutate("/api/setup", { action: "configure", config: readConfig() }, { message: "Project configuration saved.", output: true, title: "Configuration result" }); if (r && r.exit_code === 0) { setupDraft = null; rawConfig = pinDraft = null; render(); } } catch (e) { error(e.message); } }, "button primary", busy()), btn("Check installed adapter", async () => {
      const result = await mutate("/api/setup", { action: "verify-adapter" });
      if (!result) return;
      const summary = add(node("div"), node("p", "", "Built-in chat returned the installed CLI help."),
        fact("Verified instructions", result.engine.instructions_path), fact("Pinned version", result.engine.version),
        fact("Installed agent guide", result.guide), node("p", "hint", result.live_model));
      showRecord("Installed adapter check", summary);
    }, "button", busy()), btn("Create a task", () => navigate("task")))); root.append(cfg); return root;
  }
  function guidedConfiguration(card) {
    const engine = setupDraft.engine || {};
    if (!pinDraft) {
      pinDraft = {
        path: engine.instructions_path || "",
        version: engine.version || "",
        files: Object.keys(engine.included_files || {}).filter(path => path !== engine.instructions_path).join("\n"),
        changed: false
      };
    }
    const instructions = node("details", "section-gap");
    instructions.append(node("summary", "", "Change engineering instructions"));
    const path = input(pinDraft.path), version = input(pinDraft.version), files = textarea(pinDraft.files);
    const rememberPin = () => {
      pinDraft = { path: path.value.trim(), version: version.value.trim(), files: files.value, changed: true };
    };
    [path, version, files].forEach(control => control.addEventListener("input", rememberPin));
    add(instructions, field("Instruction file", path, "Enter the path from the project directory."),
      field("Instruction version", version, "Use the version that identifies this set of rules."),
      field("Additional instruction files", files, "Enter one project-relative file path per line."),
      btn("Read instruction hashes", async () => {
        const proposal = { instructions_path: pinDraft.path, version: pinDraft.version,
          files: pinDraft.files.split(/\r?\n/).map(value => value.trim()).filter(Boolean) };
        const result = await mutate("/api/setup", { action: "inspect-engine", engine: proposal });
        if (result?.exit_code !== 0) return;
        setupDraft.engine = result.data;
        pinDraft.changed = false;
        rawConfig = null;
        notify("Instruction hashes recorded. Save the configuration to use them.");
        render();
      }, "button", busy()));
    add(card, node("h3", "section-gap", "Engineering instructions"),
      node("p", "mono", [engine.version, engine.instructions_path].filter(Boolean).join(" / ")), instructions);

    const checks = Array.isArray(setupDraft.checks) ? setupDraft.checks : [];
    setupDraft.checks = checks;
    const makeCheck = () => ({ name: "", argv: [""], cwd: ".", required: true, benchmark: false });
    if (!checks.length) checks.push(makeCheck());
    add(card, node("h3", "section-gap", "Project checks"),
      node("p", "hint", "Choose a command that tests the requested behavior. The workbench records its output and exit code."));
    for (const [index, check] of checks.entries()) {
      const group = node("fieldset", "fields section-gap");
      group.append(node("legend", "", "Check " + (index + 1)));
      const name = input(check.name), program = input(check.argv?.[0] || ""), args = textarea((check.argv || []).slice(1).join("\n"));
      const directory = input(check.cwd || "."), required = input("", "checkbox");
      required.checked = check.required === true;
      const update = () => {
        check.name = name.value.trim();
        check.argv = [program.value.trim(), ...args.value.split(/\r?\n/).filter(value => value.length)];
        check.cwd = directory.value.trim() || ".";
        check.required = required.checked;
        rawConfig = null;
      };
      [name, program, args, directory, required].forEach(control => control.addEventListener("input", update));
      add(group, field("Check name", name, "Use a short name such as behavior."),
        field("Program", program, "Enter an executable such as ruby or node."),
        field("Arguments", args, "Enter one argument per line. Keep a path with spaces on one line."),
        field("Working directory", directory, "Use a directory inside the project. A dot means the project root."),
        field("Required to finish", required));
      if (check.benchmark) group.append(node("p", "hint", "This check is part of the project benchmark."));
      if (checks.length > 1) group.append(btn("Remove check", () => { checks.splice(index, 1); rawConfig = null; render(); }, "button small"));
      card.append(group);
    }
    card.append(btn("Add another check", () => { checks.push(makeCheck()); rawConfig = null; render(); }));
    const rounds = input(setupDraft.execution?.max_repair_rounds ?? 3, "number");
    rounds.min = "1"; rounds.max = "20";
    rounds.addEventListener("input", () => {
      setupDraft.execution = { ...setupDraft.execution, max_repair_rounds: Number(rounds.value) };
      rawConfig = null;
    });
    card.append(field("Maximum repair rounds", rounds, "The task stops if another repair would exceed this limit."));
    return () => {
      if (pinDraft.changed) throw new Error("Read the instruction hashes before saving a changed engine.");
      if (checks.some(check => !check.name || !check.argv[0])) throw new Error("Enter a check name and a program.");
      return setupDraft;
    };
  }

  function questionFields(questions, answers, mode, onAnswer) {
    const root = node("div"); questions.forEach(q => { const row = node("div", "question"); add(row, node("div", "question-title", `${q.text || q.prompt || q.id}${q.required ? " (required)" : ""}`)); const options = arr(q.options);
      if (options.length) { const permitted = option => mode !== "audit" || q.id !== "code_changes" || (option.id || option) === "deny"; const details = arr(q.option_details).filter(permitted); const choice = select([{ value: "", label: "Choose an answer" }, ...(details.length ? details : options.filter(permitted))], answers[q.id] || ""); choice.setAttribute("aria-label", q.text || q.prompt || q.id); choice.addEventListener("change", () => onAnswer(q.id, choice.value)); const wrap = field("Answer", choice); row.append(wrap); for (const option of details) { row.append(node("p", "hint", [option.label + (option.id === (q.recommended_by_mode?.[mode] || q.suggested_answer) ? " (recommended)" : ""), option.effect].join(" - "))); } }
      else { const answer = textarea(answers[q.id] || ""); answer.setAttribute("aria-label", q.text || q.prompt || q.id); answer.addEventListener("input", () => onAnswer(q.id, answer.value)); row.append(field("Answer", answer)); }
      root.append(row);
    }); return root;
  }
  function sourceDescription(source) {
    if (source?.kind === "project_file") return source.path + ' — “' + source.quote + '”';
    if (source?.kind === "task_request") return 'Task request — “' + source.quote + '”';
    if (source?.kind === "task_answer") return ({ cli: "CLI answer", form: "Task form", chat: "Chat answer" })[source.channel] || "Task answer";
    return "Source was not recorded";
  }
  function describeAnswer(question, answer) {
    const value = typeof answer === "object" ? answer?.option : answer;
    const label = arr(question.option_details).find(option => option.id === value)?.label || value || "Not answered";
    return answer?.source ? label + " / " + sourceDescription(answer.source) : label;
  }
  function citedRestrictions() {
    const box = node("details", "section-gap");
    box.append(node("summary", "", "Use a restriction already stated for this task"));
    const choices = { code_changes: "No application code changes", migrations: "No schema changes", dependencies: "No dependency changes" };
    const question = select(Object.entries(choices).map(([value,label]) => ({ value, label })));
    const source = select([{ value: "task_request", label: "Task action or scope" }, { value: "project_file", label: "Project decision file" }]);
    const file = input(), quote = textarea();
    const fileField = field("Decision file path", file, "Use a Markdown or text file inside this project.");
    fileField.hidden = true;
    source.addEventListener("change", () => { fileField.hidden = source.value !== "project_file"; });
    add(box, field("Restriction", question), field("Decision source", source), fileField,
      field("Exact decision text", quote, "Copy the words that support this restriction."),
      btn("Use restriction for this task", () => {
        const reference = { kind: source.value, quote: quote.value.trim() };
        if (!reference.quote) return error("Enter the text that states this restriction.");
        if (reference.kind === "project_file") {
          reference.path = file.value.trim();
          if (!reference.path) return error("Enter the project decision file path.");
        } else if (![draft.action, draft.scope].some(text => text.includes(reference.quote))) {
          return error("Copy the decision text from this task’s action or scope.");
        }
        draft.answers[question.value] = { option: "deny", source: reference };
        clearError();
        render();
      }, "button"));
    return box;
  }
  function recordedRestrictions() {
    const records = Object.entries(draft.answers).filter(([,answer]) => answer?.source);
    if (!records.length) return null;
    const list = node("div", "section-gap");
    list.append(node("h3", "", "Restrictions recorded for this task"));
    for (const [id, answer] of records) {
      const q = arr(state.questions).find(question => question.id === id);
      const row = add(node("div", "question"), node("p", "", describeAnswer(q, answer)),
        btn("Remove restriction", () => { delete draft.answers[id]; render(); }, "button small"));
      list.append(row);
    }
    return list;
  }
  function taskWizard() {
    if (taskStatus() === "unverified") return empty("Task state is unverified", task().lifecycle.reason, btn("View task record", () => showRun(task().id)));
    if (!state.installed) return empty("Set up a project first", "Install and configure the orchestration layer before creating a task.", btn("Open project setup", () => navigate("setup"), "button primary"));
    const root = node("section", "card pad"); const steps = node("div", "wizard-steps"); ["Define task", "Record decisions", "Review and create"].forEach((s, i) => steps.append(add(node("span", i === wizardStep ? "current" : ""), node("span", "wizard-number", i + 1), document.createTextNode(s)))); root.append(steps);
    if (wizardStep === 0) {
      add(root, node("h2", "", "What must the agent do?"), node("p", "section-intro", "The mode sets the behavior boundary. Keep the action and affected scope explicit.")); const grid = node("div", "mode-grid");
      Object.entries(modes).forEach(([k, desc]) => { const radio = input(k, "radio"); radio.name = "task-mode"; radio.checked = draft.mode === k; radio.addEventListener("change", () => { draft.mode = k; delete draft.answers.code_changes; }); const label = node("label", "mode"); add(label, radio, node("strong", "", k.charAt(0).toUpperCase() + k.slice(1)), node("small", "", desc)); grid.append(label); }); root.append(grid);
      const action = textarea(draft.action), scope = textarea(draft.scope); action.placeholder = "State the requested change and expected result."; scope.placeholder = "Name the affected components and behavior that must stay unchanged."; action.addEventListener("input", () => draft.action = action.value); scope.addEventListener("input", () => draft.scope = scope.value);
      add(root, add(node("div", "fields section-gap"), field("Action", action), field("Scope and constraints", scope)), recordedRestrictions(), citedRestrictions());
    } else if (wizardStep === 1) {
      add(root, node("h2", "", "Record the task decisions"), node("p", "section-intro", "These answers apply to this task. Do not grant schema, dependency, or code permissions that the task does not need."));
      add(root, recordedRestrictions(), questionFields(arr(state.questions).filter(q => !draft.answers[q.id]?.source), draft.answers, draft.mode, (id, answer) => draft.answers[id] = answer));
    } else {
      add(root, node("h2", "", "Review the task agreement"), node("p", "section-intro", "Creating a task records this agreement. It does not start an agent or run checks."), node("div", "task-summary", `MODE\n${draft.mode}\n\nACTION\n${draft.action}\n\nSCOPE AND CONSTRAINTS\n${draft.scope}\n\nDECISIONS\n${arr(state.questions).map(q => `${q.text || q.id}: ${describeAnswer(q, draft.answers[q.id])}`).join("\n")}`));
    }
    const controls = node("div", "buttons");
    if (wizardStep > 0) controls.append(btn("Back", () => { wizardStep--; render(); }));
    if (wizardStep < 2) controls.append(btn("Continue", () => { clearError(); if (!draft.action.trim() || !draft.scope.trim()) return error("Enter the action and scope before continuing."); if (wizardStep === 1) { const missing = arr(state.questions).filter(q => q.required && !draft.answers[q.id]); if (missing.length) return error(`Answer the required questions: ${missing.map(q => q.text || q.id).join("; ")}`); } wizardStep++; render(); }, "button primary"));
    else controls.append(btn("Create task", async () => { const answers = Object.fromEntries(Object.entries(draft.answers).filter(([, value]) => value !== "")); const r = await mutate("/api/task", { ...draft, answers }, { message: "Task created. Its agreement now controls CLI and agent chat." }); if (r) { wizardStep = 0; draft = { mode: "audit", action: "", scope: "", answers: {} }; navigate("chat"); } }, "button primary", busy()));
    root.append(controls); return root;
  }
  function chat() {
    if (!projectPath()) return empty("Select a project first", "Choose the project directory to start a conversation.", btn("Open project setup", () => navigate("setup"), "button primary"));
    const layout = node("div", "chat-layout"), card = node("section", "card chat-card");
    add(card, add(node("div", "chat-head"), node("h2", "", "Coding agent transcript"), pill(chatBusy() ? "running" : agreement().ready ? "ready" : "pending")));
    const transcript = node("div", "chat-transcript"); transcript.id = "chat-transcript"; transcript.setAttribute("aria-label", "Agent transcript"); transcript.setAttribute("role", "log"); transcript.setAttribute("aria-live", "polite"); renderMessages(transcript); card.append(transcript);
    const compose = node("div", "chat-compose"), message = textarea(composeDraft);
    message.id = "chat-message";
    message.setAttribute("aria-label", "Chat message");
    message.placeholder = agreement().ready ? "Describe the next step or reply to the agent." : "Enter a task request or reply to a workflow question.";
    message.disabled = busy();
    message.maxLength = 64000;
    message.addEventListener("input", () => composeDraft = message.value);
    const send = async () => {
      const value = message.value.trim();
      if (!value) return error("Enter a message.");
      const started = new Date().toISOString();
      const r = await mutate("/api/chat", { message: value });
      if (!r) return;
      composeDraft = "";
      if (r.kind === "fixed_help") {
        helpTurns.push({ user_message: value, response: r.response, type: "help", task_id: task()?.id || null, intake_id: state.chat_intake?.id, started_at: started });
      }
      render();
      const log = $("chat-transcript");
      if (log) log.scrollTop = log.scrollHeight;
      const next = $("chat-message");
      if (next && !next.disabled) next.focus();
    };
    message.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (!message.disabled) send(); } });
    add(compose, message, add(node("div", "buttons"), node("span", "draft-note", "Ctrl+Enter sends your message."), btn(chatBusy() ? "Agent is working" : "Send message", send, "button primary", busy())));
    card.append(compose); layout.append(card);
    const side = node("aside", "chat-side"), current = node("section", "card pad");
    add(current, heading("Current task", pill(taskStatus())), node("p", "", task()?.action || "No task selected.")); if (task()) add(current, node("p", "mono", task().id), node("p", "small-text", `Mode: ${task().mode}`));
    if (taskStatus() === "unverified") current.append(node("p", "", task().lifecycle.reason));
    if (task() && Object.keys(task().answers || {}).length) {
      const decisions = node("details");
      decisions.append(node("summary", "", "Recorded decisions"));
      for (const [id, answer] of Object.entries(task().answers)) {
        const q = arr(state.questions).find(question => question.id === id);
        if (q) decisions.append(node("p", "hint", describeAnswer(q, answer) + " / " + sourceDescription(task().answer_sources?.[id])));
      }
      current.append(decisions);
    }
    const controls = node("div", "action-list"); if (task()) {
      ["pause", "resume", "stop", "verify", "finish", "report"].forEach(action => { const enabled = action === "report" || (action === "resume" ? ["paused", "stopped"].includes(taskStatus()) : ["active", "running"].includes(taskStatus())); controls.append(btn({ pause: "Pause task", resume: "Resume task", stop: "Stop task", verify: "Verify current task", finish: "Finish task", report: "Read task report" }[action], () => taskAction(action), `button${action === "stop" ? " danger" : ""}`, busy() || !enabled)); });
      const running = jobs().find(j => j.task_id === task().id && (j.type || j.kind || "agent") === "agent" && activeStates.has(j.status || j.state));
      if (running) controls.prepend(btn("Cancel agent process", async () => {
        const result = await mutate("/api/chat/cancel", { id: running.id });
        if (result) notify(result.cancel_requested ? "Cancellation requested for this agent process." : "The agent process has already ended.");
      }, "button danger", pending));
    } else controls.append(btn("Create a task", () => navigate("task"), "button primary")); current.append(controls); side.append(current);
    const missing = agreement().missing;
    if (taskStatus() === "active" && missing.length) { const q = node("section", "card pad"); add(q, heading("Required decisions"), node("p", "", "The agent stays disabled until the required answers are recorded.")); const answers = {}; q.append(questionFields(missing, answers, task().mode, (id, answer) => answers[id] = answer)); q.append(add(node("div", "buttons"), btn("Save answers", async () => { for (const item of missing) { if (!answers[item.id]) return error(`Answer: ${item.text || item.id}`); const r = await mutate("/api/answer", { question: item.id, option: answers[item.id] }); if (!r) return; } notify("Required answers recorded."); render(); }, "button primary", busy()))); side.append(q); }
    if (task()) {
      const checks = node("section", "card pad");
      const configured = arr(state.config?.checks);
      const phases = select(["baseline", "after"], "baseline");
      phases.setAttribute("aria-label", "Check phase");
      const choice = node("select");
      choice.setAttribute("aria-label", "Configured check");
      const run = btn("Run selected check", () => mutate("/api/check", { name: choice.value, phase: phases.value }, { message: "Check started." }));
      const updateChoices = () => {
        const previous = choice.value;
        const eligible = configured.filter(check => {
          const allowed = check.phases || ["baseline", "after"];
          return !check.benchmark && allowed.includes(phases.value) && (allowed.includes("baseline") || ["feature", "build"].includes(task()?.mode));
        });
        choice.replaceChildren(...eligible.map(check => new Option(check.name, check.name)));
        if (eligible.some(check => check.name === previous)) choice.value = previous;
        run.disabled = busy() || !agreement().ready || !eligible.length;
      };
      phases.addEventListener("change", updateChoices);
      updateChoices();
      const benchmark = btn("Run project benchmark", () => mutate("/api/benchmark", { phase: phases.value }, { message: "Benchmark started." }), "button", busy() || !agreement().ready || !arr(state.config?.benchmark?.checks).length);
      add(checks, heading("Run configured checks"), field("Phase", phases), add(node("div", "check-controls"), field("Check", choice), run, benchmark));
      side.append(checks);
    }
    const connection = node("section", "card pad"); add(connection, heading("Agent connection"), node("p", "", "Check availability separately. A successful process must still produce and verify the requested work."), add(node("div", "buttons"), btn("Check availability", () => mutate("/api/capabilities", {}, { output: true, title: "Agent availability" }), "button", busy()))); side.append(connection); layout.append(side); return layout;
  }
  function renderMessages(target) {
    target.replaceChildren(); const messages = [];
    const workflow = arr(state.workflow_messages).map(entry => ({ ...entry, type: "workflow" }));
    const currentIntake = state.chat_intake?.id;
    const turns = [...transcripts.values(), ...workflow, ...helpTurns].filter(turn => {
      if (currentIntake) return turn.intake_id === currentIntake;
      if (turn.intake_id && !turn.task_id) {
        const owner = workflow.find(entry => entry.intake_id === turn.intake_id && entry.task_id);
        return owner ? owner.task_id === task()?.id : !task();
      }
      return (turn.task_id || null) === (task()?.id || null);
    });
    turns.sort((a,b) => (Date.parse(a.started_at) || 0) - (Date.parse(b.started_at) || 0)).forEach(job => { if (job.user_message) messages.push({ label: "You", role: "user", text: job.user_message }); const events = arr(job.events); let hadAgentText = false;
      events.forEach(event => { const item = event.item || {}; if (event.type === "item.completed" && item.type === "agent_message" && item.text) { messages.push({ label: "Coding agent", role: "agent", text: item.text, at: event.at }); hadAgentText = true; } else if (event.type === "error" || event.type === "turn.failed") messages.push({ label: "Agent error", role: "system", text: event.message || event.error?.message || JSON.stringify(event) }); else if (item.type === "command_execution" && event.type === "item.completed") messages.push({ label: `Command · exit ${val(item.exit_code, "unknown")}`, role: "system", text: `${val(item.command, "Command not recorded")}\n${val(item.aggregated_output, "No captured output")}` }); });
      if (!hadAgentText && job.response) {
        const label = job.type === "help" ? "Fixed help" : job.type === "workflow" ? (job.exit_code ? "Workflow / exit " + job.exit_code : "Workflow") : job.type === "command" ? "Command result" : "Coding agent";
        messages.push({ label, role: label === "Coding agent" ? "agent" : "system", text: job.response });
      }
      if (job.status === "cancelled") messages.push({ label: "Cancelled", role: "system", text: "The agent process was cancelled." });
      if (job.error) messages.push({ label: "Job error", role: "system", text: typeof job.error === "string" ? job.error : JSON.stringify(job.error) });
    });
    if (!messages.length) {
      const hint = taskStatus() === "unverified" ? task().lifecycle.reason : !state.installed ? "Send /setup to install the workbench in this project." : agreement().ready ? "Send the next step to the coding agent." : task() && taskStatus() === "active" ? "Answer the required task decisions to enable the coding agent." : "Start with audit, refactor, feature, or build followed by the action you want.";
      target.append(add(node("div", "chat-empty"), node("h3", "", "No messages yet"), node("p", "", hint)));
    }
    messages.forEach(m => { const box = node("div", `message ${m.role}`); add(box, node("span", "message-label", m.label), document.createTextNode(m.text)); target.append(box); });
    jobs().filter(j => task() && j.task_id === task().id && activeStates.has(j.status || j.state)).forEach(j => target.append(add(node("div", "message system"), node("span", "message-label", "Live job"), document.createTextNode(`${val(j.type || j.kind, "agent")} · ${val(j.status || j.state)} · ${j.id}`))));
  }
  async function taskAction(action) {
    if (["pause", "stop", "repair-round"].includes(action)) {
      const body = node("div"); const reason = textarea(); add(body, field("Reason", reason, "Record why the task must pause, stop, or enter a repair round."), add(node("div", "buttons"), btn(`Confirm ${action}`, async () => { if (!reason.value.trim()) return error("Enter the reason before continuing."); $("record-dialog").close(); await mutate("/api/task/action", { action, id: task()?.id, reason: reason.value.trim() }, { output: true, title: "Task action result" }); }, "button primary"))); showRecord(`Task ${action}`, body); return;
    }
    await mutate("/api/task/action", { action, id: task()?.id }, { output: true, title: action === "report" ? "Task report" : "Task action result" });
  }
  async function showRun(id) { clearError(); try { const r = await request(`/api/run?id=${encodeURIComponent(id)}`); showRecord(`Task ${id}`, runDetail(r.data || r)); } catch (e) { error(e.message); } }
  function runDetail(record) {
    if (Array.isArray(record.runs)) {
      const comparison = node("div");
      comparison.append(node("p", "section-intro", record.note));
      for (const run of record.runs) {
        const section = node("section", "report-item");
        add(section, node("h3", "", run.action), node("p", "mono", run.id), node("p", "", run.scope));
        const results = arr(run.checks);
        section.append(node("p", "", [run.mode, run.lifecycle?.status, results.length + " recorded checks"].filter(Boolean).join(" / ")));
        for (const check of results) section.append(node("p", "", [check.name || check.check, check.phase, "exit " + val(check.exit_code)].join(" / ")));
        comparison.append(section);
      }
      const raw = node("details");
      add(raw, node("summary", "", "Full comparison records"), node("pre", "output", JSON.stringify(record, null, 2)));
      return add(comparison, raw);
    }
    const root = node("div"); add(root, node("h3", "", record.action || record.task?.action || "Task record"), node("p", "section-intro", record.scope || record.task?.scope || "Scope not recorded."));
    const records = record.checks || record.results || []; records.forEach(c => { const row = node("div", "report-item"); add(row, node("h3", "", `${val(c.name || c.check)} · exit ${val(c.exit_code)}`), node("p", "mono", `Phase: ${val(c.phase)} · Revision: ${val(c.source_after_sha256 || c.snapshot_sha256 || c.source_sha256)}`)); root.append(row); });
    root.append(node("pre", "output", JSON.stringify(record, null, 2))); return root;
  }
  function runsView() {
    if (!state.installed) return empty("No installed project", "Select a project to inspect its task records.", btn("Open setup", () => navigate("setup")));
    const root = node("div"), toolbar = node("div", "run-toolbar"), search = input(runSearch, "search"); search.setAttribute("aria-label", "Search task runs"); search.placeholder = "Search task action, mode, or ID"; search.addEventListener("input", () => { runSearch = search.value; renderRunTable(); });
    const compare = btn("Compare two selected runs", async () => { const r = await mutate("/api/runs/compare", { ids: [...selectedRuns] }, { title: "Run comparison" }); if (r) showRecord("Run comparison", runDetail(r.data || r)); }, "button", selectedRuns.size !== 2 || busy()); compare.id = "compare-runs"; add(toolbar, search, compare); root.append(toolbar);
    const container = node("div"); container.id = "run-table"; root.append(container); queueMicrotask(renderRunTable); return root;
  }
  function renderRunTable() {
    const target = $("run-table"); if (!target) return; target.replaceChildren(); const records = arr(state?.runs).filter(r => JSON.stringify(r).toLowerCase().includes(runSearch.toLowerCase()));
    if (!records.length) { target.append(empty("No matching task runs", "Create a task or clear the search filter.")); return; }
    const wrap = node("div", "table-wrap"), table = node("table"), head = node("tr"); ["Select", "Task", "Mode", "Lifecycle", "Created", "Record"].forEach(v => head.append(node("th", "", v))); table.append(add(node("thead"), head)); const body = node("tbody");
    records.forEach(r => { const row = node("tr"), choice = input("", "checkbox"); choice.checked = selectedRuns.has(r.id); choice.setAttribute("aria-label", `Select task ${r.id}`); choice.addEventListener("change", () => { if (choice.checked) selectedRuns.add(r.id); else selectedRuns.delete(r.id); $("compare-runs").disabled = selectedRuns.size !== 2 || busy(); }); const created = r.created_at ? new Date(r.created_at).toLocaleString() : "Not recorded"; add(row, add(node("td"), choice), add(node("td"), node("strong", "", r.action), node("small", "mono", r.id), node("small", "", r.scope)), node("td", "", r.mode), add(node("td"), pill(r.status || r.lifecycle?.status || "unknown")), node("td", "", created), add(node("td"), btn("Open record", () => showRun(r.id), "button small"))); body.append(row); }); table.append(body); wrap.append(table); target.append(wrap);
  }
  async function refresh(force = false) {
    if (pollRequest) {
      if (!force) return pollRequest;
      await pollRequest;
    }
    pollRequest = readState(force);
    try { await pollRequest; } finally { pollRequest = null; }
  }
  async function readState(force) {
    polling = true; updateHeader();
    try {
      const next = await request("/api/state"); const previousState = state, oldProject = projectPath(), oldTask = task()?.id, oldBusy = busy(), wasDisconnected = failed;
      state = next; lastSuccess = new Date(); failed = false;
      if (oldTask !== task()?.id) composeDraft = "";
      if (oldProject !== projectPath()) { transcripts.clear(); helpTurns.length = 0; shownJobErrors.clear(); runSearch = ""; setupDraft = null; rawConfig = pinDraft = null; composeDraft = ""; wizardStep = 0; draft = { mode: "audit", action: "", scope: "", answers: {} }; selectedRuns.clear(); }
      await Promise.all(jobs().map(async j => {
        const previous = transcripts.get(j.id);
        let events = previous?.events || [];
        if (j.type === "agent" && !j.restored && j.task_id === task()?.id && (j.next_cursor || 0) > events.length) {
          const delta = await request(`/api/job?id=${encodeURIComponent(j.id)}&after=${events.length}`);
          events = events.concat(arr(delta.events));
        }
        transcripts.set(j.id, { ...previous, ...j, events }); if (j.task_id === task()?.id && (j.status || j.state) === "failed" && !shownJobErrors.has(j.id)) { shownJobErrors.add(j.id); error(j.error || j.response || j.output || "A job failed. Open its recorded result."); } }));
      const controlsChanged = oldBusy !== busy() || JSON.stringify(previousState?.active_task) !== JSON.stringify(task()) || oldProject !== projectPath() || previousState?.installed !== state.installed ||
        JSON.stringify(previousState?.config) !== JSON.stringify(state.config) || JSON.stringify(previousState?.capabilities) !== JSON.stringify(state.capabilities);
      if (wasDisconnected && $("errors").textContent.startsWith("The local server is unavailable.")) clearError();
      if (force || !previousState || controlsChanged || (view === "runs" && JSON.stringify(previousState.runs) !== JSON.stringify(state.runs))) render();
      else if (view === "chat") { const target = $("chat-transcript"); if (target) { const atBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 60; renderMessages(target); if (atBottom) target.scrollTop = target.scrollHeight; } if (!chatBusy() && $("chat-message")?.disabled && agreement().ready && !busy()) render(); }
    } catch (e) { failed = true; error(`The local server is unavailable. Displayed records can be stale. Current job state is unknown. ${e.message}`); }
    finally { polling = false; updateHeader(); }
  }
  $("refresh").addEventListener("click", () => refresh(true)); $("close-dialog").addEventListener("click", () => $("record-dialog").close());
  window.addEventListener("hashchange", () => { const next = location.hash.slice(1); if (Object.hasOwn(sections, next) && next !== view) navigate(next); });
  setInterval(() => refresh(), 3000);
  refresh(true);
})();
