#!/usr/bin/env python3
"""Link this checkout into the user's CLI and Omarchy shell."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent
PLUGIN = 'hoppcx.agent-workspaces'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    links = {Path.home() / '.local/bin/sideyard': ROOT / 'bin/sideyard',
             Path.home() / '.local/bin/aw': ROOT / 'bin/aw',
             Path.home() / '.config/omarchy/plugins' / PLUGIN: ROOT / 'plugin'}
    for dest, source in links.items():
        if (dest.exists() or dest.is_symlink()) and not (dest.is_symlink() and dest.resolve() == source.resolve()):
            raise SystemExit(f'Preserving existing {dest}; move it aside before installing this checkout.')
    if args.remove:
        subprocess.run(['omarchy', 'plugin', 'disable', PLUGIN], check=True)
        for dest in links:
            dest.unlink(missing_ok=True)
        print('Removed CLI and shell integration. Workspace files and artifacts are preserved.')
        return
    subprocess.run([str(ROOT / 'bin/aw'), 'doctor'], check=True)
    subprocess.run(['omarchy', 'plugin', 'validate', str(ROOT / 'plugin')], check=True)
    config = Path.home() / '.config/omarchy/shell.json'
    if config.is_file():
        shutil.copy2(config, config.with_name(f'shell.json.bak.aw-{time.time_ns()}'))
    for dest, source in links.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_symlink():
            dest.symlink_to(source, target_is_directory=source.is_dir())
    subprocess.run(['omarchy-shell', 'shell', 'rescanPlugins'], check=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = subprocess.run(['omarchy-shell', 'shell', 'listPlugins'],
                                capture_output=True, text=True, check=True)
        if any(item['id'] == PLUGIN for item in json.loads(result.stdout)):
            break
        time.sleep(0.2)
    else:
        raise SystemExit('Omarchy did not discover the widget; run make install again once the shell is ready.')
    subprocess.run(['omarchy', 'plugin', 'enable', PLUGIN, '--section', 'left', '--after', 'omarchy.workspaces'], check=True)
    # Linked plugin URLs can remain in Qt's component cache after a rescan.
    subprocess.run(['omarchy', 'restart', 'shell'], check=True)
    print('Installed Sideyard. Open it from the bar, or run sideyard start work.')
    print('Connect an agent using command ' + str(Path.home() / '.local/bin/sideyard') + ' with args ["mcp"].')


if __name__ == '__main__':
    main()
