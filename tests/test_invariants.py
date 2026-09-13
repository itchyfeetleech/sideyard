"""Small regression suite for the desktop boundary and shared agent contract."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_workspaces import api, desktop, host, input, mcp, nsinit, paths, session


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = patch.object(paths, 'state_root', return_value=self.root / 'state')
        self.state.start()
        self.addCleanup(self.state.stop)
        env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(self.root / 'rt')})
        env.start()
        self.addCleanup(env.stop)

    def test_paths_reject_escape(self):
        for value in ('', '../host', '/tmp/host', 'a/b', 'a' * 65):
            with self.assertRaises(ValueError):
                api.run('stop', {'id': value})

    def test_input_validates_entire_batch_before_execution(self):
        lines = input.encode([{'type': 'click', 'x': 10, 'y': 20},
                              {'type': 'key', 'keys': ['CTRL', 'A']},
                              {'type': 'type', 'text': 'Hello Ω'}])
        self.assertEqual(lines, ['M 10 20', 'B 272 1', 'B 272 0',
                                 'K 29 1', 'K 30 1', 'K 30 0', 'K 29 0', 'T Hello Ω'])
        for bad in ([{'type': 'click', 'x': '10', 'y': 20}],
                    [{'type': 'key', 'keys': ['BOGUS']}],
                    [{'type': 'click', 'x': 1, 'y': 2}] * 16):
            with self.assertRaises(ValueError):
                input.encode(bad)

    def test_old_generation_and_paused_owner_reject_input(self):
        session._write_state('a', generation=4, owner='paused')
        with self.assertRaises(session.AwError) as stale:
            session.input_batch('a', ['M 1 1'], generation=3)
        self.assertEqual(stale.exception.code, 'stale_generation')
        with self.assertRaises(session.AwError) as owner:
            session.input_batch('a', ['M 1 1'], generation=4)
        self.assertEqual(owner.exception.code, 'not_owner')
        with self.assertRaises(ValueError):
            api.run('input', {'id': 'a', 'actions': []})

    def test_supervisor_rechecks_owner_between_actions(self):
        session._write_state('a', generation=1, owner='agent')
        supervisor = nsinit.Session('a', 800, 600)
        self.addCleanup(supervisor.log.close)
        def ack(line):
            session._write_state('a', generation=2, owner='paused')
            return 'OK'
        with patch.object(supervisor, 'helper_running', return_value=True), \
             patch.object(supervisor, 'helper_cmd', side_effect=ack) as helper:
            result = supervisor.op_input({'generation': 1, 'actions': ['M 1 1', 'M 2 2']}, {})
        self.assertFalse(result['ok'])
        self.assertEqual([call.args[0] for call in helper.call_args_list], ['M 1 1', 'C'])

    def test_process_birth_time_prevents_pid_reuse(self):
        pid = os.getpid()
        paths.ensure_dir(paths.session_dir('a'))
        paths.atomic_write_json(paths.session_dir('a') / 'bwrap.pid', {'pid': pid, 'start': 'wrong'})
        self.assertEqual(session._bwrap_pid('a'), 0)
        paths.atomic_write_json(paths.session_dir('a') / 'bwrap.pid', {'pid': pid, 'start': session._proc_start(pid)})
        self.assertEqual(session._bwrap_pid('a'), pid)

    def test_two_session_limit_precedes_desktop_creation(self):
        with patch.object(session, 'is_live', return_value=False), \
             patch.object(session, 'live_sessions', return_value=['a', 'b']), \
             patch.object(desktop, 'prepare') as prepare:
            with self.assertRaises(session.AwError) as cap:
                session.start('c', 800, 600)
        self.assertEqual(cap.exception.code, 'session_cap')
        prepare.assert_not_called()

    def test_namespace_routes_private_state_and_project(self):
        project = self.root / 'project with spaces'
        project.mkdir()
        session._write_state('a', project=str(project))
        argv = session.build_bwrap_argv('a', 800, 600)
        self.assertIn('--unshare-pid', argv)
        bindings = [(argv[i+1], argv[i+2]) for i,v in enumerate(argv) if v == '--bind']
        self.assertIn((str(project), str(project)), bindings)
        self.assertIn((str(paths.session_runtime_dir('a')), f'/run/user/{os.getuid()}'), bindings)
        self.assertNotIn('/dev/input', argv)
        self.assertNotIn(os.environ['XDG_RUNTIME_DIR'] + '/bus', argv)

    def test_frame_maps_resized_crop_and_rejects_cross_workspace(self):
        frame = {'session_id': 'a', 'generation': 4, 'region': [100, 50, 800, 600], 'width': 400, 'height': 300}
        with patch.dict(mcp.FRAMES, {'test': frame}, clear=True):
            request = {'id': 'a', 'frame': 'test', 'actions': [{'type': 'drag', 'x': 10, 'y': 20, 'to_x': 399, 'to_y': 299}]}
            mapped = mcp.from_frame(request)
            self.assertEqual(mapped['actions'][0], {'type': 'drag', 'x': 120, 'y': 90, 'to_x': 898, 'to_y': 648})
            self.assertEqual(mapped['generation'], 4)
            self.assertEqual(request['actions'][0]['x'], 10)
            for bad in ({**request, 'id': 'b'}, {**request, 'generation': 3},
                        {**request, 'actions': [{'type': 'click', 'x': 400, 'y': 0}]}):
                with self.assertRaises(ValueError):
                    mcp.from_frame(bad)

    def test_followup_validation_precedes_input_and_capture_failure_preserves_success(self):
        args = {'id': 'a', 'generation': 1, 'actions': [{'type': 'key', 'keys': ['ENTER']}], 'screenshot': True}
        with patch.object(api, 'run', return_value={'ok': True, 'generation': 1}) as run, \
             patch.object(mcp, 'observe', side_effect=RuntimeError('capture failed')):
            result = mcp.dispatch('tools/call', {'name': 'input', 'arguments': {**args, 'wait_ms': 3000}})
            self.assertTrue(result['isError'])
            run.assert_not_called()
            result = mcp.dispatch('tools/call', {'name': 'input', 'arguments': {**args, 'wait_ms': 0}})
            self.assertFalse(result['isError'])
            self.assertIn('Input completed', result['content'][1]['text'])
            run.assert_called_once()

    def test_native_allocation_preserves_numbers_without_using_occupied_workspaces(self):
        session._write_state('a', host_workspace=6, transport={'viewer_pid': 10})
        clients = [{'pid': 10, 'workspace': {'id': 6}}, {'pid': 20, 'workspace': {'id': 7}}]
        def ctl(*args):
            return json.dumps(clients if args[0] == 'clients' else {'id': 5})
        with patch.object(host, 'ctl', side_effect=ctl), patch.object(session, 'list_sessions', return_value=[]):
            self.assertEqual(host.allocate('a'), 6)
            self.assertEqual(host.allocate('b'), 8)
        self.assertEqual(input.encode([{'type': 'type', 'text': 'a\nb\tc'}]),
                         ['T a', 'K 28 1', 'K 28 0', 'T b', 'K 15 1', 'K 15 0', 'T c'])
        self.assertEqual(len(input.encode([{'type': 'click', 'x': 1, 'y': 1, 'count': 2}])), 5)

    def test_delete_preserves_attached_project_and_unrelated_workspaces(self):
        project = self.root / 'project'
        project.mkdir()
        (project / 'keep.txt').write_text('keep')
        work = paths.ensure_dir(paths.work_dir('a'))
        (work / '.aw-desktop').write_text('1')
        (work / 'project-link').symlink_to(project, target_is_directory=True)
        session._write_state('a', project=str(project))
        other = paths.ensure_dir(paths.work_dir('b'))
        with patch.object(paths, 'share_root', return_value=self.root / 'share'), \
             patch.object(session, 'stop', return_value={'group_pids_left': []}) as stop:
            with self.assertRaises(ValueError):
                api.run('delete', {'id': 'a'})
            stop.assert_not_called()
            result = api.run('delete', {'id': 'a', 'confirm': True})
        self.assertTrue(result['deleted'])
        self.assertFalse(paths.session_dir('a').exists())
        self.assertEqual((project / 'keep.txt').read_text(), 'keep')
        self.assertTrue(other.is_dir())

    def test_delete_keeps_files_if_shutdown_fails_or_project_is_inside(self):
        work = paths.ensure_dir(paths.work_dir('a'))
        (work / '.aw-desktop').write_text('1')
        with patch.object(session, 'stop', return_value={'bwrap_alive': True}):
            with self.assertRaises(session.AwError):
                api.run('delete', {'id': 'a', 'confirm': True})
        self.assertTrue(work.exists())
        session._write_state('a', project=str(work / 'important'))
        with patch.object(session, 'stop') as stop:
            with self.assertRaises(session.AwError) as error:
                api.run('delete', {'id': 'a', 'confirm': True})
            self.assertEqual(error.exception.code, 'project_inside_workspace')
            stop.assert_not_called()

    def test_mcp_stdio_initialization_tools_and_errors(self):
        requests = [
            {'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
            {'method': 'notifications/initialized'},
            {'id': 2, 'method': 'tools/list'},
            {'id': 3, 'method': 'tools/call', 'params': {'name': 'input', 'arguments': {'id': 'a'}}},
            {'id': 4, 'method': 'missing'},
        ]
        data = '\n'.join(json.dumps({'jsonrpc': '2.0', **r}) for r in requests) + '\n'
        result = subprocess.run([str(Path(__file__).resolve().parents[1] / 'bin/aw'), 'mcp'],
                                input=data, text=True, capture_output=True, check=True)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(len(rows[1]['result']['tools']), 11)
        self.assertTrue(rows[2]['result']['isError'])
        self.assertEqual(rows[3]['error']['code'], -32601)
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
