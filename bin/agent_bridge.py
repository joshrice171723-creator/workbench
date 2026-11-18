"""Use the same local chat backend from the terminal."""
import argparse
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--message', required=True)
    parser.add_argument('--url', default='http://127.0.0.1:8792')
    parser.add_argument('--timeout', type=int, default=1800)
    options = parser.parse_args()
    parsed = urlparse(options.url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.username or parsed.password:
        raise ValueError('Use the local workbench HTTP address.')
    base = options.url.rstrip('/')
    def request(route, data=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-CSRF-Token'] = token
        payload = json.dumps(data).encode() if data is not None else None
        with urlopen(Request(base + route, data=payload, headers=headers), timeout=120) as response:
            return json.load(response)
    state = request('/api/state')
    project = state.get('project') or {}
    current = project.get('path') if isinstance(project, dict) else project
    if not current or Path(current).resolve() != Path(options.project).resolve():
        request('/api/project', {'path': options.project}, state['csrf_token'])
    job = request('/api/chat', {'message': options.message}, state['csrf_token'])
    if job.get('kind') in {'fixed_help', 'workflow'}:
        print(job['response'])
        return job.get('exit_code', 0)
    deadline = time.monotonic() + options.timeout
    while job.get('state') in {'created', 'starting', 'running', 'cancelling'}:
        if time.monotonic() > deadline:
            print('The wait timed out. The owned job remains available in the workbench; use Cancel to stop it.', file=sys.stderr)
            return 1
        time.sleep(.5)
        job = request('/api/job?id=' + job['id'])
    print(job.get('response') or job.get('error') or 'No agent response was returned.')
    return 0 if job.get('state') == 'completed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except HTTPError as error:
        try:
            message = json.load(error).get("error", str(error))
        except (OSError, ValueError):
            message = str(error)
        print("Chat could not run: " + message, file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, OSError, URLError) as error:
        print('Chat could not run: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
