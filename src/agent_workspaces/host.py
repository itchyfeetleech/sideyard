"""Present an agent desktop on an ordinary, numbered host workspace."""
import json
import os
import subprocess
import time
from . import paths


def ctl(*args):
    result = subprocess.run(['hyprctl', *args], capture_output=True, text=True, timeout=10)
    if result.returncode or (args[0] == 'dispatch' and result.stdout.strip() != 'ok'):
        raise RuntimeError('Host Hyprland: ' + (result.stderr or result.stdout).strip())
    return result.stdout


def available():
    return bool(os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'))


def geometry():
    monitors = json.loads(ctl('monitors', '-j'))
    monitor = next((m for m in monitors if m.get('focused')), monitors[0])
    left, top, right, bottom = monitor.get('reserved', [0, 0, 0, 0])
    width, height = monitor['width'], monitor['height']
    if monitor.get('transform', 0) % 2:
        width, height = height, width
    return int(width / monitor['scale'] - left - right), int(height / monitor['scale'] - top - bottom)


def allocate(session_id):
    from . import api, session
    with api.locked('_host'):
        clients = json.loads(ctl('clients', '-j'))
        state = session.read_state(session_id) or {}
        existing = state.get('host_workspace')
        own_pid = (state.get('transport') or {}).get('viewer_pid')
        used = {c['workspace']['id'] for c in clients if c.get('pid') != own_pid}
        used.update(s.get('host_workspace') for s in session.list_sessions()
                    if s['session_id'] != session_id and s['live'])
        active = json.loads(ctl('activeworkspace', '-j'))['id']
        choices = ([existing] if existing and existing not in used else [])
        choices += [n for n in range(6, 11) if n not in used and n != active]
        if not choices:
            choices = [n for n in range(1, 6) if n not in used and n != active]
        if not choices:
            raise RuntimeError('All ten numbered workspaces are occupied; free one before opening an agent desktop.')
        number = choices[0]
        session._write_state(session_id, host_workspace=number)
        return number


def present(session_id, pid, number=None):
    from . import session
    number = number or allocate(session_id)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        clients = json.loads(ctl('clients', '-j'))
        viewer = next((c for c in clients if c.get('pid') == pid and c.get('mapped')), None)
        if viewer:
            address = 'address:' + viewer['address']
            ctl('dispatch', f'hl.dsp.window.set_prop({{ window = "{address}", prop = "no_shortcuts_inhibit", value = "1" }})')
            # Native maximize fills the workspace while leaving the host bar visible.
            ctl('dispatch', f'hl.dsp.window.move({{ window = "{address}", workspace = "{number}", follow = false }})')
            ctl('dispatch', f'hl.dsp.window.fullscreen({{ window = "{address}", mode = "maximized", action = "set" }})')
            ctl('dispatch', f'hl.dsp.focus({{ workspace = "{number}" }})')
            ctl('dispatch', f'hl.dsp.focus({{ window = "{address}" }})')
            session._write_state(session_id, host_workspace=number, viewer_address=viewer['address'])
            return
        if session.proc_gone(pid):
            raise RuntimeError('Viewer exited before opening its workspace')
        time.sleep(0.1)
    raise RuntimeError('Viewer window did not appear in host Hyprland')
