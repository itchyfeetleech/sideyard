"""The aw command. All operations emit JSON for humans, agents and the shell."""
import argparse
import json
import subprocess
import sys
from . import api
from .session import AwError


def build_parser():
    parser = argparse.ArgumentParser(description='Sideyard — a desktop for your agent')
    commands = parser.add_subparsers(dest='op', required=True)
    for name in ('list', 'doctor', 'mcp'):
        commands.add_parser(name)
    for name in ('start', 'stop', 'status', 'open', 'control', 'screenshot', 'input', 'launch', 'windows', 'focus', 'delete'):
        p = commands.add_parser(name)
        p.add_argument('id', help='workspace name')
        if name == 'start':
            p.add_argument('--width', type=int)
            p.add_argument('--height', type=int)
            p.add_argument('--headless', action='store_true', help='start without a host workspace or viewer')
            p.add_argument('--project', help='project directory to make writable inside this desktop')
        elif name == 'delete':
            p.add_argument('--yes', dest='confirm', action='store_true', help='permanently remove workspace files and app profiles; attached projects are kept')
        elif name == 'control':
            p.add_argument('mode', choices=['human', 'agent', 'paused'])
        elif name == 'screenshot':
            p.add_argument('--out')
        elif name == 'input':
            p.add_argument('--generation', type=int, required=True)
            p.add_argument('actions', type=json.loads, help='JSON action array; see README')
        elif name == 'focus':
            p.add_argument('window', help='address from aw windows')
            p.add_argument('--generation', type=int, required=True)
        elif name == 'launch':
            p.add_argument('argv', nargs=argparse.REMAINDER, help='program and arguments after --')
    return parser


def main(argv=None):
    args = vars(build_parser().parse_args(argv))
    op = args.pop('op')
    if op == 'mcp':
        from . import mcp
        return mcp.main()
    if op == 'start':
        args['human'] = not args.get('headless')
    if op == 'launch' and args['argv'][:1] == ['--']:
        args['argv'] = args['argv'][1:]
    try:
        result = api.run(op, args)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get('ok') is False else 0
    except (AwError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': {'code': getattr(exc, 'code', 'unavailable'), 'message': str(exc)}}))
        return 1
    except KeyboardInterrupt:
        return 130
