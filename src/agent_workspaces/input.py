"""Translate small, readable input actions to the persistent Wayland helper."""
import re
KEYS = {'ESC': 1, 'ESCAPE': 1, 'BACKSPACE': 14, 'TAB': 15, 'ENTER': 28,
        'CTRL': 29, 'CONTROL': 29, 'SHIFT': 42, 'ALT': 56, 'SPACE': 57,
        'SUPER': 125, 'META': 125, 'LEFT': 105, 'RIGHT': 106, 'UP': 103,
        'DOWN': 108, 'HOME': 102, 'END': 107, 'PAGEUP': 104, 'PAGEDOWN': 109,
        'DELETE': 111, 'INSERT': 110}
KEYS.update(zip('1234567890', range(2, 12)))
for letters, first in [('QWERTYUIOP', 16), ('ASDFGHJKL', 30), ('ZXCVBNM', 44)]:
    KEYS.update(zip(letters, range(first, first + len(letters))))
KEYS.update({f'F{i}': 58 + i for i in range(1, 11)})
KEYS.update(F11=87, F12=88)


def number(action, key):
    value = action.get(key)
    if type(value) is not int:
        raise ValueError(f'{key} must be an integer')
    return value


def encode(actions) -> list[str]:
    if not isinstance(actions, list) or not 1 <= len(actions) <= 16:
        raise ValueError('Provide 1–16 actions per input call')
    lines = []
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError('Each action must be an object')
        kind = action.get('type')
        if kind in ('move', 'click', 'drag'):
            x, y = number(action, 'x'), number(action, 'y')
            if x < 0 or y < 0:
                raise ValueError('Coordinates cannot be negative')
            lines.append(f'M {x} {y}')
            if kind in ('click', 'drag'):
                button = {'left': 272, 'right': 273, 'middle': 274}.get(action.get('button', 'left'))
                if button is None:
                    raise ValueError('button must be left, right or middle')
                count = action.get('count', 1)
                if type(count) is not int or count not in (1, 2):
                    raise ValueError('click count must be 1 or 2')
                lines.append(f'B {button} 1')
                if kind == 'drag':
                    lines.append(f'M {number(action, "to_x")} {number(action, "to_y")}')
                lines.append(f'B {button} 0')
                if kind == 'click' and count == 2:
                    lines.extend([f'B {button} 1', f'B {button} 0'])
        elif kind == 'type':
            value = action.get('text')
            if not isinstance(value, str) or not value:
                raise ValueError('text must be a non-empty string')
            if len(value.encode('utf-8')) > 4096 or any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127 for c in value):
                raise ValueError('Use up to 4096 UTF-8 bytes; only newline and tab control characters are supported')
            for piece in re.split(r'([\n\t])', value):
                if piece in ('\n', '\t'):
                    code = 28 if piece == '\n' else 15
                    lines.extend([f'K {code} 1', f'K {code} 0'])
                else:
                    lines.extend('T ' + piece[i:i + 240] for i in range(0, len(piece), 240))
        elif kind == 'key':
            keys = action.get('keys')
            if not isinstance(keys, list) or not 1 <= len(keys) <= 5:
                raise ValueError('keys must be an array of 1–5 names, e.g. ["CTRL", "A"]')
            try:
                codes = [KEYS[k.upper()] for k in keys]
            except (KeyError, AttributeError):
                raise ValueError('Unknown key name') from None
            lines += [f'K {code} 1' for code in codes]
            lines += [f'K {code} 0' for code in reversed(codes)]
        elif kind == 'scroll':
            lines.append(f'S {number(action, "dx")} {number(action, "dy")}')
        else:
            raise ValueError(f'Unknown action type: {kind}')
    if len(lines) > 32:
        raise ValueError('Input batch too large; split into smaller calls')
    return lines
