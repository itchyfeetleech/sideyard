"""Small stdio surface: compact observations and frame-relative desktop input."""
import base64
import copy
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from . import __version__, api
from .session import AwError

ID = {'type': 'string'}
ACTION = {'type': 'object', 'properties': {
    'type': {'type': 'string', 'enum': ['move', 'click', 'drag', 'type', 'key', 'scroll']},
    **{k: {'type': 'integer'} for k in ('x', 'y', 'to_x', 'to_y', 'dx', 'dy')},
    'button': {'type': 'string', 'enum': ['left', 'right', 'middle']},
    'count': {'type': 'integer', 'enum': [1, 2]},
    'text': {'type': 'string'},
    'keys': {'type': 'array', 'items': {'type': 'string'}},
}, 'required': ['type'], 'additionalProperties': False}
FRAMES = {}  # Per connection; old frames cannot accumulate indefinitely.
FRAME_NUMBER = 0


class MethodNotFound(Exception):
    pass


def tool(name, description, properties=None, required=None, readonly=False):
    return {'name': name, 'description': description,
            'annotations': {'readOnlyHint': readonly},
            'inputSchema': {'type': 'object', 'properties': properties or {},
                            'required': required or [], 'additionalProperties': False}}


TOOLS = [
    tool('list', 'List workspaces and control owners.', readonly=True),
    tool('start', 'Start/reuse a desktop (two maximum). Opens a numbered host workspace in watch mode; headless skips the viewer.',
         {'id': ID, 'project': ID, 'headless': {'type': 'boolean'},
          'width': {'type': 'integer'}, 'height': {'type': 'integer'}}, ['id']),
    tool('stop', 'Close this desktop and its apps; preserve files.', {'id': ID}, ['id']),
    tool('delete', 'Permanently delete a workspace and its app profiles/files, stopping it first. Attached projects are kept. Requires explicit confirmation.',
         {'id': ID, 'confirm': {'type': 'boolean', 'enum': [True]}}, ['id', 'confirm']),
    tool('open', 'Show this desktop on its numbered host workspace; preserve control owner.', {'id': ID}, ['id']),
    tool('control', 'Set input owner. Human opens an interactive viewer. Observe again after changing ownership.',
         {'id': ID, 'mode': {'type': 'string', 'enum': ['human', 'agent', 'paused']}}, ['id', 'mode']),
    tool('screenshot', 'Observe: PNG + frame for input. Default longest edge 1280; region [x,y,w,h] zooms native desktop pixels. image=false returns a file path.',
         {'id': ID, 'max_size': {'type': 'integer', 'minimum': 320, 'maximum': 3840},
          'region': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 4, 'maxItems': 4},
          'image': {'type': 'boolean'}}, ['id'], readonly=True),
    tool('input', 'Act using frame and image-pixel coordinates. Optional screenshot=true observes afterward (wait_ms 0–2000, default 150). Key: ["CTRL","A"]. Scroll dx/dy: steps right/down. Drag: x,y,to_x,to_y. Type: Unicode, newline/tab, 4096 bytes. Legacy generation uses native pixels.',
         {'id': ID, 'frame': ID, 'generation': {'type': 'integer'},
          'actions': {'type': 'array', 'items': ACTION, 'minItems': 1, 'maxItems': 16},
          'screenshot': {'type': 'boolean'}, 'wait_ms': {'type': 'integer', 'minimum': 0, 'maximum': 2000}}, ['id', 'actions']),
    tool('launch', 'Launch installed app argv, e.g. ["foot"], in the attached project.',
         {'id': ID, 'argv': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}}, ['id', 'argv']),
    tool('windows', 'List window addresses, app, title and native geometry; no image.', {'id': ID}, ['id'], readonly=True),
    tool('focus', 'Focus a window address from windows. Requires an observed frame and agent ownership.',
         {'id': ID, 'window': ID, 'frame': ID}, ['id', 'window', 'frame']),
]


def compact(name, result):
    if name == 'list':
        return {'sessions': [{k: s[k] for k in ('session_id', 'live', 'owner', 'host_workspace')
                              if s.get(k) is not None} for s in result['sessions']]}
    if name == 'windows':
        return {'session_id': result['session_id'], 'windows': [
            {k: w[k][:240] if isinstance(w[k], str) else w[k]
             for k in ('address', 'class', 'title', 'at', 'size', 'focusHistoryID') if k in w}
            for w in result['windows']]}
    keys = ('session_id', 'ok', 'owner', 'mode', 'generation', 'geometry',
            'host_workspace', 'control', 'acked', 'pid', 'log', 'focused', 'note', 'deleted', 'project_preserved')
    return {k: result[k] for k in keys if k in result}


def text(value):
    return {'type': 'text', 'text': json.dumps(value, ensure_ascii=False, separators=(',', ':'))}


def observe(args):
    global FRAME_NUMBER
    include_image = args.get('image', True)
    temp = None
    capture = {k: v for k, v in args.items() if k in ('id', 'max_size', 'region')}
    capture.setdefault('max_size', 1280)
    # Each capture owns its file until encoded, even with concurrent MCP clients.
    if include_image:
        with tempfile.NamedTemporaryFile(suffix='.png', prefix='aw-frame-', delete=False) as file:
            temp = Path(file.name)
        capture['out'] = str(temp)
    try:
        result = api.run('screenshot', capture)
        image = base64.b64encode(Path(result['path']).read_bytes()).decode() if include_image else None
    finally:
        if temp:
            temp.unlink(missing_ok=True)
    FRAME_NUMBER += 1
    frame = f'f{FRAME_NUMBER}'
    FRAMES[frame] = result
    if len(FRAMES) > 32:
        del FRAMES[next(iter(FRAMES))]
    meta = {k: result[k] for k in ('session_id', 'generation', 'width', 'height', 'region', 'owner')}
    meta['frame'] = frame
    if not include_image:
        meta['path'] = result['path']
    content = [text(meta)]
    if image is not None:
        content.append({'type': 'image', 'mimeType': 'image/png', 'data': image})
    return content


def from_frame(args):
    args = copy.deepcopy(args)
    frame = FRAMES.get(args.pop('frame', None))
    if frame is None or frame['session_id'] != args['id']:
        raise ValueError('Unknown frame for this workspace; take a fresh screenshot')
    if 'generation' in args and args['generation'] != frame['generation']:
        raise ValueError('generation conflicts with frame')
    args['generation'] = frame['generation']
    x, y, width, height = frame['region']
    for action in args.get('actions', []):
        if not isinstance(action, dict):
            raise ValueError('Each action must be an object')
        for key, origin, extent, pixels in (('x', x, width, frame['width']), ('y', y, height, frame['height']),
                                            ('to_x', x, width, frame['width']), ('to_y', y, height, frame['height'])):
            if key in action:
                value = action[key]
                if type(value) is not int or not 0 <= value < pixels:
                    raise ValueError(f'{key} is outside the frame')
                action[key] = origin + min(extent - 1, round(value * extent / pixels))
    return args


def dispatch(method, params):
    if method == 'initialize':
        requested = params.get('protocolVersion')
        return {'protocolVersion': requested if requested in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25') else '2025-11-25',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'sideyard', 'version': __version__},
                'instructions': 'Name the workspace. Use windows for cheap inspection, screenshot for a frame, then input with that frame. Respect human/paused control. Frames expire after takeover/restart; observe again. Super shortcuts in the host switch native workspaces.'}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'tools/call':
        name = params.get('name')
        spec = next((t for t in TOOLS if t['name'] == name), None)
        args = params.get('arguments', {})
        try:
            if spec is None or not isinstance(args, dict):
                raise ValueError('Unknown tool or invalid arguments')
            schema = spec['inputSchema']
            if set(args) - set(schema['properties']) or set(schema['required']) - set(args):
                raise ValueError('Unexpected or missing arguments')
            for key, value in args.items():
                prop = schema['properties'][key]
                typ = prop['type']
                if (typ == 'string' and not isinstance(value, str) or
                    typ == 'integer' and type(value) is not int or
                    typ == 'boolean' and type(value) is not bool or
                    typ == 'array' and not isinstance(value, list)):
                    raise ValueError(f'Invalid type for {key}')
                if ('enum' in prop and value not in prop['enum'] or
                    'minimum' in prop and value < prop['minimum'] or
                    'maximum' in prop and value > prop['maximum']):
                    raise ValueError(f'Invalid value for {key}')
            if name == 'screenshot':
                content = observe(args)
            else:
                run_args = from_frame(args) if 'frame' in args else dict(args)
                followup = run_args.pop('screenshot', False)
                wait_ms = run_args.pop('wait_ms', 150)
                result = api.run(name, run_args)
                content = [text(compact(name, result))]
                if followup:
                    time.sleep(wait_ms / 1000)
                    try:
                        content.extend(observe({'id': args['id']}))
                    except Exception as exc:
                        content.append(text({'observation_error': str(exc), 'note': 'Input completed; do not repeat it.'}))
            return {'content': content, 'isError': False}
        except Exception as exc:
            # A tool failure must not tear down the connection or look like an
            # unknown JSON-RPC method. Preserve existing codes for expected errors.
            code = 'invalid_request' if isinstance(exc, (AwError, OSError, ValueError, RuntimeError, subprocess.SubprocessError)) else type(exc).__name__
            return {'content': [text({'code': getattr(exc, 'code', code), 'message': str(exc)})], 'isError': True}
    raise MethodNotFound(method)


def main():
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
                raise ValueError('Invalid JSON-RPC request')
            if 'id' not in request:
                continue
            params = request.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('params must be an object')
            response = {'result': dispatch(request['method'], params)}
        except json.JSONDecodeError:
            response = {'error': {'code': -32700, 'message': 'Parse error'}}
        except (TypeError, ValueError):
            response = {'error': {'code': -32600, 'message': 'Invalid request'}}
        except MethodNotFound:
            response = {'error': {'code': -32601, 'message': 'Method not found'}}
        except Exception as exc:
            response = {'error': {'code': -32603, 'message': f'Internal error: {type(exc).__name__}'}}
        response.update(jsonrpc='2.0', id=request.get('id') if isinstance(request, dict) else None)
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0
