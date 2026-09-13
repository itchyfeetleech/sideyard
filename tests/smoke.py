"""One real-desktop check. Run explicitly: python3 tests/smoke.py.

Creates up to two disposable desktops in free slots, verifies input and handover, opens one
native viewer, then stops both. Does not touch personal desktop input.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_workspaces import api, paths, session


def wait_for(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError('Timed out waiting for desktop behavior')


def main():
    free = session.SESSION_CAP - len(session.live_sessions())
    if free < 1:
        raise SystemExit('No free desktop slot. Stop a workspace before running the live check.')
    ids = [f'check-{os.getpid()}-{suffix}' for suffix in ('a', 'b')[:free]]
    ready = []
    try:
        for sid in ids:
            ready.append(api.run('start', {'id': sid, 'width': 1280, 'height': 720, 'headless': True}))
            api.run('launch', {'id': sid, 'argv': ['foot', '--app-id', sid]})
            wait_for(lambda: any(w['class'] == sid for w in api.run('windows', {'id': sid})['windows']))
        if len(ready) == 2:
            assert ready[0]['host_runtime_dir'] != ready[1]['host_runtime_dir']
            assert ready[0]['inner_signature'] != ready[1]['inner_signature']
        for sid in ids:
            shot = api.run('screenshot', {'id': sid})
            command = "printf '%s' '" + sid + " Ω' > proof.txt"
            api.run('input', {'id': sid, 'generation': shot['generation'], 'actions': [
                {'type': 'type', 'text': 'discard me'}, {'type': 'key', 'keys': ['CTRL', 'U']},
                {'type': 'type', 'text': command + '\n'}]})
            proof = paths.work_dir(sid) / 'proof.txt'
            wait_for(lambda: proof.exists())
            assert proof.read_text() == sid + ' Ω'
        sid = ids[0]
        previous = api.run('screenshot', {'id': sid})['generation']
        human = api.run('control', {'id': sid, 'mode': 'human'})
        wait_for(lambda: session._owned_pid(sid, human['viewer_pid'], ('vncviewer',)) is not None)
        time.sleep(0.5)
        assert not session.proc_gone(human['viewer_pid'])
        try:
            api.run('input', {'id': sid, 'generation': previous, 'actions': [{'type': 'key', 'keys': ['A']}]})
            raise AssertionError('Stale input accepted')
        except session.AwError as exc:
            assert exc.code == 'stale_generation'
        returned = api.run('control', {'id': sid, 'mode': 'agent'})
        assert returned['generation'] > previous
        assert not Path(human['view_sock']).exists()
        api.run('control', {'id': sid, 'mode': 'paused'})
        paused = api.run('screenshot', {'id': sid})
        try:
            api.run('input', {'id': sid, 'generation': paused['generation'], 'actions': [{'type': 'key', 'keys': ['A']}]})
            raise AssertionError('Paused input accepted')
        except session.AwError as exc:
            assert exc.code == 'not_owner'
        api.run('control', {'id': ids[-1], 'mode': 'agent'})
        aw = str(Path(__file__).resolve().parents[1] / 'bin/aw')
        proc = subprocess.Popen([aw, 'mcp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        def mcp_call(name, args):
            proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                        'params': {'name': name, 'arguments': args}}) + '\n')
            proc.stdin.flush()
            result = json.loads(proc.stdout.readline())['result']
            assert not result.get('isError'), result
            return result['content']
        try:
            window = api.run('windows', {'id': ids[-1]})['windows'][0]
            content = mcp_call('screenshot', {'id': ids[-1], 'region': window['at'] + window['size'], 'max_size': 640})
            frame = json.loads(content[0]['text'])
            assert frame['session_id'] == ids[-1] and frame['width'] <= 640
            assert base64.b64decode(content[1]['data']).startswith(b'\x89PNG\r\n\x1a\n')
            content = mcp_call('input', {'id': ids[-1], 'frame': frame['frame'], 'actions': [
                {'type': 'click', 'x': frame['width']//2, 'y': frame['height']//2},
                {'type': 'type', 'text': "printf 'frame-ok' > mcp.txt\n"}], 'screenshot': True, 'wait_ms': 300})
            assert content[2]['type'] == 'image'
            proof = paths.work_dir(ids[-1]) / 'mcp.txt'
            wait_for(lambda: proof.exists())
            assert proof.read_text() == 'frame-ok'
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            assert not proc.stderr.read()
        old_generation = paused['generation']
        api.run('delete', {'id': sid, 'confirm': True})
        assert not paths.session_dir(sid).exists()
        api.run('start', {'id': sid, 'width': 1280, 'height': 720, 'headless': True})
        try:
            api.run('input', {'id': sid, 'generation': old_generation,
                              'actions': [{'type': 'key', 'keys': ['ENTER']}]})
            raise AssertionError('Input from a deleted desktop was accepted by its replacement')
        except session.AwError as exc:
            assert exc.code == 'stale_generation'
        print(f'PASS: {len(ids)} desktop(s), Unicode/Ctrl input, native handover, MCP frames, deletion and stale input after recreation')
    finally:
        for sid in ids:
            if paths.session_dir(sid).exists():
                stopped = api.run('stop', {'id': sid})
                assert not stopped.get('bwrap_alive') and not stopped['group_pids_left'], stopped
        print('PASS: owned processes stopped; work files preserved')
        for sid in ids:
            shutil.rmtree(paths.session_dir(sid), ignore_errors=True)
            shutil.rmtree(paths.share_root() / sid, ignore_errors=True)
            shutil.rmtree(paths.session_runtime_dir(sid), ignore_errors=True)


if __name__ == '__main__':
    main()
