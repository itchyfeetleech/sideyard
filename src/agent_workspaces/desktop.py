"""Prepare a small private desktop from installed Omarchy defaults."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from . import paths

# Aquamarine's nested backend on the supported build needs its first configure
# acknowledged before enabling the output. The supervisor removes this on boot.
OVERRIDE = 'hl.monitor({ output = "WAYLAND-1", disabled = true })\n'


def apply_monitor_override(work: Path) -> None:
    target = work / '.config/hypr/monitors.lua'
    text = target.read_text()
    if not text.startswith(OVERRIDE):
        target.write_text(OVERRIDE + text)


def clear_monitor_override(work: Path) -> bool:
    target = work / '.config/hypr/monitors.lua'
    text = target.read_text()
    if text.startswith(OVERRIDE):
        target.write_text(text[len(OVERRIDE):])
        return True
    return False


def prepare(session_id: str, width: int, height: int) -> None:
    work = paths.work_dir(session_id)
    marker = work / '.aw-desktop'
    if marker.exists():
        (work / ".config/hypr/monitors.lua").write_text(
            f'hl.monitor({{ output = "", mode = "{width}x{height}@60", position = "0x0", scale = 1 }})\n')
        return
    if work.exists():
        raise RuntimeError('This is a legacy replica. Choose a new workspace name; its files are preserved.')
    omarchy = Path(os.environ.get('OMARCHY_PATH', '/usr/share/omarchy'))
    if not (omarchy / 'default/hypr/bootstrap.lua').is_file():
        raise RuntimeError('The installed Omarchy Lua desktop is required.')
    hypr = paths.ensure_dir(work / '.config/hypr')
    try:
        # Use stock modules in place. Never copy personal plugins or autostarts.
        (hypr / 'hyprland.lua').write_text('''dofile((os.getenv("OMARCHY_PATH") or "/usr/share/omarchy") .. "/default/hypr/bootstrap.lua")
package.loaded["default.hypr.autostart"] = true
require("default.hypr.omarchy")
require("hypr.monitors")
hl.config({ xwayland = { enabled = false } })
''')
        (hypr / 'monitors.lua').write_text(
            f'hl.monitor({{ output = "", mode = "{width}x{height}@60", position = "0x0", scale = 1 }})\n')
        config = work / '.config/omarchy'
        config.mkdir(parents=True)
        (config / 'shell.json').write_text(json.dumps({
            'version': 1,
            'idle': {'screensaver': 0, 'lock': 0},
            'disabledPlugins': ['omarchy.idle', 'omarchy.lock', 'omarchy.polkit',
                                'omarchy.nightlight'],
            'bar': {'layout': {
                'left': [{'id': 'omarchy.menu'}, {'id': 'omarchy.workspaces'}],
                'center': [{'id': 'omarchy.clock'}],
                'right': [{'id': 'omarchy.keyboard-layout'}]}},
            'plugins': [],
        }, indent=2) + '\n')
        # Only appearance data is copied; app profiles and personal settings start empty.
        current = Path.home() / '.local/state/omarchy/current'
        dest = work / '.local/state/omarchy/current'
        dest.mkdir(parents=True)
        for name in ('theme', 'theme.name'):
            source = current / name
            if source.is_dir():
                shutil.copytree(source.resolve(), dest / name)
            elif source.is_file():
                shutil.copy2(source.resolve(), dest / name)
        shutil.copy2(Path(__file__).resolve().parents[2] / 'assets/wallpaper.svg', dest / 'background')
        for name in ('alacritty', 'foot', 'kitty', 'ghostty', 'fontconfig', 'gtk-3.0', 'gtk-4.0'):
            source = omarchy / 'config' / name
            if source.is_dir():
                shutil.copytree(source, work / '.config' / name, symlinks=True)
        marker.write_text('1\n')
    except Exception:
        shutil.rmtree(work)
        raise
