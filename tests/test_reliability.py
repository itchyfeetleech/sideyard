"""Failure-path regressions; no desktop or real input devices required."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_workspaces import api, input, mcp, nsinit, paths, session


class InputTests(unittest.TestCase):
    def test_invalid_drag_is_rejected_before_sending_any_input(self):
        for key in ('x', 'y', 'to_x', 'to_y'):
            for value in (-1, True, '10'):
                action = {'type': 'drag', 'x': 10, 'y': 10, 'to_x': 20, 'to_y': 20, key: value}
                with self.subTest(key=key, value=value), patch.object(session, 'input_batch') as send:
                    with tempfile.TemporaryDirectory() as runtime, patch.dict(os.environ, {'XDG_RUNTIME_DIR': runtime}):
                        with self.assertRaises(ValueError):
                            api.run('input', {'id': 'test', 'generation': 1, 'actions': [action]})
                    send.assert_not_called()

    def test_supervisor_rejects_negative_moves(self):
        for line in ('M -1 2', 'M 1 -2'):
            self.assertIsNotNone(nsinit.check_action(line))


class HelperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        state = patch.object(paths, 'state_root', return_value=self.root / 'state')
        state.start()
        self.addCleanup(state.stop)
        session._write_state('test', generation=1, owner='agent')
        self.supervisor = nsinit.Session('test', 800, 600)
        self.addCleanup(self.supervisor.log.close)

    def test_helper_error_after_button_down_releases_input(self):
        with patch.object(self.supervisor, 'helper_running', return_value=True), \
             patch.object(self.supervisor, 'helper_cmd', side_effect=['OK', 'ERR bad move', 'OK']) as cmd:
            result = self.supervisor.op_input(
                {'generation': 1, 'actions': ['B 272 1', 'M 5 5', 'B 272 0']}, {})
        self.assertFalse(result['ok'])
        self.assertEqual([call.args[0] for call in cmd.call_args_list], ['B 272 1', 'M 5 5', 'C'])

    def start_fake_helper(self):
        proof = self.root / 'released'
        proc = subprocess.Popen([sys.executable, '-c',
                                 'import sys; from pathlib import Path; '
                                 'sys.stdin.read(); Path(sys.argv[1]).write_text("released")', str(proof)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.supervisor.helper = proc
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()
        self.addCleanup(cleanup)
        return proc, proof

    def test_timeout_discards_helper_and_releases_on_eof(self):
        proc, proof = self.start_fake_helper()
        with patch.object(self.supervisor, 'helper_cmd', side_effect=['OK', RuntimeError('helper ack timeout')]) as cmd:
            result = self.supervisor.op_input(
                {'generation': 1, 'actions': ['K 29 1', 'K 30 1', 'K 29 0']}, {})
        self.assertFalse(result['ok'])
        self.assertIsNone(self.supervisor.helper)
        self.assertEqual(proc.wait(timeout=5), 0)
        self.assertEqual(proof.read_text(), 'released')
        # The old stream may have a late acknowledgement; never reuse it.
        self.assertEqual(cmd.call_count, 2)

    def test_failed_cancel_discards_helper(self):
        for failure in ('ERR cancel failed', RuntimeError('helper ack timeout')):
            with self.subTest(failure=str(failure)):
                proc, proof = self.start_fake_helper()
                with patch.object(self.supervisor, 'helper_cmd', side_effect=[failure]):
                    result = self.supervisor.op_cancel()
                self.assertFalse(result['ok'])
                self.assertIsNone(self.supervisor.helper)
                self.assertEqual(proc.wait(timeout=5), 0)
                self.assertEqual(proof.read_text(), 'released')


class McpTests(unittest.TestCase):
    def run_messages(self, messages):
        source = '\n'.join(json.dumps({'jsonrpc': '2.0', **m}) for m in messages) + '\n'
        output = io.StringIO()
        with patch.object(sys, 'stdin', io.StringIO(source)), patch.object(sys, 'stdout', output):
            self.assertEqual(mcp.main(), 0)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_unexpected_tool_errors_do_not_break_the_connection(self):
        for error in (KeyError('missing state'), AssertionError('unexpected state')):
            with self.subTest(error=type(error).__name__), patch.object(api, 'run', side_effect=error):
                rows = self.run_messages([
                    {'id': 1, 'method': 'tools/call', 'params': {'name': 'windows', 'arguments': {'id': 'test'}}},
                    {'id': 2, 'method': 'ping'},
                ])
            self.assertTrue(rows[0]['result']['isError'])
            self.assertEqual(rows[1], {'jsonrpc': '2.0', 'id': 2, 'result': {}})

    def test_internal_dispatch_errors_are_distinct_from_unknown_methods(self):
        with patch.object(mcp, 'dispatch', side_effect=[KeyError('bug'), {}]):
            rows = self.run_messages([{'id': 1, 'method': 'ping'}, {'id': 2, 'method': 'ping'}])
        self.assertEqual(rows[0]['error']['code'], -32603)
        self.assertEqual(rows[1]['result'], {})
        self.assertEqual(self.run_messages([{'id': 3, 'method': 'unknown'}])[0]['error']['code'], -32601)

    def test_unexpected_screenshot_error_does_not_report_completed_input_as_failed(self):
        with patch.object(api, 'run', return_value={'ok': True}) as run, \
             patch.object(mcp, 'observe', side_effect=KeyError('capture metadata')):
            result = mcp.dispatch('tools/call', {'name': 'input', 'arguments': {
                'id': 'test', 'generation': 1, 'actions': [{'type': 'key', 'keys': ['ENTER']}],
                'screenshot': True, 'wait_ms': 0}})
        self.assertFalse(result['isError'])
        self.assertIn('Input completed; do not repeat it.', result['content'][1]['text'])
        run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
