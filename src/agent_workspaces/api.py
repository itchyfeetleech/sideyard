"""One operation dispatcher shared by CLI, shell panel and MCP."""
from __future__ import annotations

import fcntl
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from . import paths, session


@contextmanager
def locked(name: str):
    root = paths.ensure_dir(paths.runtime_root())
    with open(root / f'{name}.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def doctor() -> dict:
    commands = ('Hyprland', 'hyprctl', 'sway', 'swaymsg', 'bwrap', 'dbus-daemon',
                'quickshell', 'grim', 'wayvnc', 'vncviewer')
    missing = [command for command in commands if not shutil.which(command)]
    helper = Path(__file__).resolve().parents[2] / 'build/aw_input/aw-input'
    if not helper.is_file():
        missing.append('aw-input (run make)')
    if not list(Path('/dev/dri').glob('renderD*')):
        missing.append('GPU render node')
    return {'ok': not missing, 'missing': missing}


def run(op: str, args: dict | None = None) -> dict:
    args = args or {}
    if op == 'doctor':
        return doctor()
    if op == 'list':
        return {'sessions': session.list_sessions()}
    sid = paths.check_session_id(args.get('id', ''))
    # Every entry point shares these locks. Start also serializes slot allocation.
    with locked(sid):
        if op == 'start':
            with locked('_start'):
                deps = doctor()
                if not deps['ok']:
                    raise session.AwError('missing_dependencies', ', '.join(deps['missing']))
                project = args.get('project')
                if project:
                    project = Path(project).expanduser().resolve(strict=True)
                    if not project.is_dir() or project in (Path('/'), Path.home()):
                        raise ValueError('Choose a project directory, not the whole home or filesystem.')
                    if session.is_live(sid) and str(project) != (session.read_state(sid) or {}).get('project'):
                        raise ValueError('Stop the workspace before changing its project.')
                    session._write_state(sid, project=str(project))
                from . import host
                native = not args.get('headless', False) and host.available()
                width, height = host.geometry() if native else (1600, 900)
                ready = session.start(sid, int(args.get('width') or width), int(args.get('height') or height))
                if native:
                    view = (session.take_control(sid) if args.get('human') else session.watch(sid))
                    ready = {**ready, 'host_workspace': session.read_state(sid).get('host_workspace'), 'control': view['mode']}
                return ready
        if op == 'delete':
            if args.get('confirm') is not True:
                raise ValueError('Deleting removes workspace files and app profiles; pass confirm=true (CLI: --yes)')
            return session.delete(sid)
        if op == 'stop':
            return session.stop(sid)
        if op == 'status':
            return session.status(sid)
        if op == 'open':
            return session.watch(sid, with_viewer=args.get('viewer', True))
        if op == 'control':
            mode = args.get('mode')
            if mode == 'human':
                return session.take_control(sid, with_viewer=args.get('viewer', True))
            if mode == 'agent':
                return session.return_to_agent(sid)
            if mode == 'paused':
                return session.pause(sid)
            raise ValueError('mode must be human, agent or paused')
        if op == 'screenshot':
            dest = Path(args.get('out') or paths.artifacts_dir(sid) / 'shot.png').absolute()
            return session.screenshot(sid, dest, args.get("max_size"), args.get("region"))
        if op == 'input':
            if type(args.get('generation')) is not int:
                raise ValueError('generation from a fresh screenshot is required')
            from .input import encode
            return session.input_batch(sid, encode(args.get('actions')), args['generation'])
        if op == 'launch':
            argv = args.get('argv')
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and '\0' not in a for a in argv):
                raise ValueError('argv must be a non-empty array of strings')
            return {'session_id': sid, **session.spawn_app(sid, argv)}
        if op == 'focus':
            if type(args.get('generation')) is not int:
                raise ValueError('generation from a fresh screenshot is required')
            session.require_agent(sid, args['generation'])
            address = args.get('window')
            clients = json.loads(session.hyprctl(sid, ['clients', '-j']))
            if not isinstance(address, str) or address not in {w['address'] for w in clients}:
                raise ValueError('window must be an address from this workspace’s windows tool')
            session.hyprctl(sid, ['dispatch', f'hl.dsp.focus({{ window = "address:{address}" }})'])
            return {'session_id': sid, 'focused': address}
        if op == 'windows':
            return {'session_id': sid, 'windows': json.loads(session.hyprctl(sid, ['clients', '-j']))}
        raise ValueError(f'Unknown operation: {op}')
