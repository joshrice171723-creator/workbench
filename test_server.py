import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        (self.project / 'app').mkdir()
        (self.project / 'app/model.rb').write_text('class Model; end')
        self.server = server.create_server(self.project, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.app.pool.shutdown()
        self.temp.cleanup()

    def request(self, route, data=None, token=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-CSRF-Token'] = self.server.app.token
        if origin:
            headers['Origin'] = origin
        raw = json.dumps(data).encode() if data is not None else None
        with urlopen(Request(self.base + route, data=raw, headers=headers), timeout=180) as response:
            return json.load(response)

    def test_state_is_read_only_and_exposes_real_project(self):
        data = self.request('/api/state')
        self.assertFalse(data['installed'])
        self.assertEqual(data['project']['path'], str(self.project.resolve()))
        self.assertFalse((self.project / '.harness').exists())

    def test_unreadable_lifecycle_blocks_agent_and_checks(self):
        self.request('/api/setup', {'action': 'install'})
        self.request('/api/task', {'mode': 'audit', 'action': 'Read model', 'scope': 'app/model.rb'})
        with patch.object(Path, 'iterdir', side_effect=PermissionError('Access denied by the host')):
            state = self.request('/api/state')
            self.assertEqual(state['active_task']['status'], 'unverified')
            self.assertEqual(state['runs'][0]['status'], 'unverified')
            self.assertIn('Access denied by the host', state['active_task']['lifecycle']['reason'])
            for route, payload in [('/api/chat', {'message': 'Read model'}), ('/api/check', {'name': 'check', 'phase': 'baseline'})]:
                with self.assertRaises(HTTPError) as rejected:
                    self.request(route, payload)
                self.assertIn('Access denied by the host', rejected.exception.read().decode())
            self.assertEqual(self.server.app.jobs, {})

    def test_mutations_require_token_and_same_origin(self):
        for token, origin in [(False, None), (True, 'https://external.invalid')]:
            with self.assertRaises(HTTPError) as error:
                self.request('/api/setup', {'action': 'install'}, token=token, origin=origin)
            self.assertEqual(error.exception.code, 403)
        self.assertFalse((self.project / '.harness').exists())

    def test_shell_endpoint_and_unowned_cancellation_are_rejected(self):
        for route, payload in [('/api/shell', {'command': 'echo unsafe'}), ('/api/chat/cancel', {'id': 'not-owned'})]:
            with self.assertRaises(HTTPError) as error:
                self.request(route, payload)
            self.assertEqual(error.exception.code, 400)

    def test_chat_requires_a_recorded_task_before_launching_model(self):
        with patch.object(server, 'AgentJob') as agent:
            with self.assertRaises(HTTPError) as error:
                self.request('/api/chat', {'message': 'Refactor this project'})
            self.assertEqual(error.exception.code, 400)
            agent.assert_not_called()

    def test_real_cli_install_task_questions_and_lifecycle(self):
        installed = self.request('/api/setup', {'action': 'install'})
        self.assertEqual(installed['exit_code'], 0)
        state = self.request('/api/state')
        self.assertTrue(state['installed'])
        self.assertGreater(len(state['questions']), 0)
        result = self.request('/api/task', {'mode': 'audit', 'action': 'Inspect model readability', 'scope': 'app'})
        task = result['state']['active_task']
        self.assertEqual(task['mode'], 'audit')
        self.assertEqual(task['answers'], {})
        self.request('/api/answer', {'question': 'code_changes', 'option': 'deny'})
        state = self.request('/api/state')
        self.assertEqual(state['active_task']['answers']['code_changes'], 'deny')
        result = self.request('/api/task/action', {'action': 'pause', 'reason': 'Pause synthetic test'})
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.request('/api/state')['active_task']['status'], 'paused')
        self.assertEqual(self.request('/api/task/action', {'action': 'resume'})['exit_code'], 0)
        result = self.request('/api/task/action', {'action': 'verify'})
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual(self.request('/api/state')['active_task']['status'], 'active')

    def test_task_creation_validates_all_answers_before_creating_a_record(self):
        self.request('/api/setup', {'action': 'install'})
        with self.assertRaises(HTTPError):
            self.request('/api/task', {'mode': 'audit', 'action': 'Inspect model', 'scope': 'app',
                'answers': {'scope_confirmed': 'confirmed', 'code_changes': 'allow_scoped'}})
        self.assertIsNone(self.request('/api/state')['active_task'])

    def test_chat_intake_waits_for_explicit_choices_and_uses_shared_cli_records(self):
        def send(message):
            return self.request('/api/chat', {'message': message})
        send('/setup')
        first = send('audit Inspect the model')
        self.assertIsNotNone(first['intake_id'])
        self.assertIsNone(self.request('/api/state')['active_task'])
        second = send('app/model.rb')
        task_id = second['task_id']
        self.assertIn('recommended', second['response'])
        send('Please pick for me')
        self.assertEqual(self.request('/api/state')['active_task']['answers'], {})
        for value in ['confirmed', 'deny', 'deny', 'deny', 'confirmed']:
            result = send(value)
        self.assertIn('Agreement recorded', result['response'])
        saved = self.request('/api/state')['active_task']
        self.assertEqual(saved['answer_sources']['dependencies'], {'kind': 'task_answer', 'channel': 'chat'})
        records = self.request('/api/state')['workflow_messages']
        self.assertEqual(next(row['task_id'] for row in records if row['id'] == first['id']), task_id)
        status = send('/status')
        self.assertEqual(json.loads(status['response'])['task_id'], task_id)
        self.assertNotEqual(send('/finish')['exit_code'], 0)
        send('/stop End the synthetic audit')
        self.assertEqual(self.request('/api/state')['active_task']['status'], 'stopped')

    def test_installed_adapter_checks_fixed_help_and_current_engine_without_launching_an_agent(self):
        self.request('/api/setup', {'action': 'install'})
        checked = self.request('/api/setup', {'action': 'verify-adapter'})
        self.assertEqual(checked['fixed_help'], 'matched')
        self.assertRegex(checked['engine']['sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(self.request('/api/state')['jobs'], [])
        engine = self.project / checked['engine']['instructions_path']
        engine.write_text('Changed rules')
        with self.assertRaises(HTTPError):
            self.request('/api/setup', {'action': 'verify-adapter'})

    def test_real_cli_agreement_then_mock_agent_resumes_only_its_own_task(self):
        calls = []
        thread = str(uuid.uuid4())

        class RecordedTurn:
            def __init__(self, prompt, project, log_dir, **options):
                self.id = str(uuid.uuid4())
                self.log_dir = log_dir
                self.options = options
                calls.append((prompt, options))

            def start(self):
                self.log_dir.mkdir()
                return self

            def snapshot(self, after=0):
                return {'id': self.id, 'state': 'completed', 'thread_id': thread,
                        'task_id': self.options['task_id'], 'user_message': self.options['user_message'],
                        'response': 'Mocked turn', 'events': [], 'next_cursor': 0}

            def wait(self):
                return self.snapshot()

        self.server.app.agent_factory = RecordedTurn
        self.request('/api/setup', {'action': 'install'})
        task = {'mode': 'audit', 'action': 'Review model', 'scope': 'app', 'answers': {
            'scope_confirmed': 'confirmed', 'code_changes': 'deny', 'migrations': 'deny',
            'dependencies': 'deny', 'engine_integration': 'confirmed'}}
        self.request('/api/task', task)
        self.assertEqual(self.request('/api/chat', {'message': '/help'})['kind'], 'fixed_help')
        self.request('/api/chat', {'message': 'Inspect the model'})
        self.request('/api/chat', {'message': 'Explain your finding'})
        self.assertIsNone(calls[0][1]['thread_id'])
        self.assertEqual(calls[1][1]['thread_id'], thread)
        self.assertEqual(calls[1][1]['sandbox'], 'read-only')
        self.assertEqual((calls[1][1]['model'], calls[1][1]['effort']), ('gpt-6.2-astra', 'high'))
        self.assertIn('Review model', calls[0][0])
        self.request('/api/task/action', {'action': 'stop', 'reason': 'End first test task'})
        self.request('/api/task', task)
        self.request('/api/chat', {'message': 'Inspect the new task'})
        self.assertIsNone(calls[2][1]['thread_id'])
        self.assertNotEqual(calls[0][1]['task_id'], calls[2][1]['task_id'])
        self.server.app.pool.shutdown(wait=True)
        for _, options in calls:
            saved = self.project / '.harness/chat' / options['task_id'] / 'thread.json'
            self.assertEqual(json.loads(saved.read_text())['thread_id'], thread)


if __name__ == '__main__':
    unittest.main()
