Sideyard uses one CLI and one MCP server for the same desktop operations.
Independent Omarchy desktops for any agent. Start a named workspace, launch apps,
observe it, and take control when you need to. Your personal desktop stays usable.

The shell panel uses Omarchy’s own components, fonts and theme. Each desktop runs
installed Hyprland and the Omarchy shell, with a private app configuration and a
small bar. It takes a copy of your current theme and uses the Sideyard wallpaper when first created; it does
not clone your personal plugins, run personal startup scripts, or modify your
personal desktop configuration.


Keep this checkout in a permanent location, then run:

```sh
make install
```

This builds the small Wayland input helper, links `~/.local/bin/sideyard` (plus the compatible `aw` alias), and enables
one widget through Omarchy’s plugin command. It backs up `shell.json` first and
briefly restarts the shell to load updated widget code. There
is no background manager service, Python package installation, or agent-specific
plugin. The desktop icon beside your workspace numbers opens the workspace panel.

Required packages on Omarchy: `python`, `base-devel`, `wayland`, `libxkbcommon`,
`wlr-protocols`, `hyprland`, `quickshell`, `sway`, `wayvnc`, `tigervnc`, `grim`,
`bubblewrap`, and `dbus`. `sideyard doctor` reports missing runtime dependencies.
Install missing packages with `omarchy pkg add <names>`.


```sh
sideyard start work --project "$PWD"
sideyard launch work -- foot
sideyard open work
sideyard control work human
sideyard control work agent
sideyard stop work
```

`start` creates the desktop and opens it on its own numbered Hyprland workspace,
ready for keyboard and mouse control. It fills the display below the top bar.
The regular workspace strip shows its number, and **Super+number** switches to it
(**Super+0** for workspace 10). Free numbers 6–10 are preferred, then free numbers
1–5. Existing windows are never moved out of the way. Reopening keeps the same
number unless another app has occupied it.

The host bar permanently labels the active agent workspace **AGENT · name**, with
its current control owner and **Take control / Watch** and **Stop** buttons.
Your normal host Super shortcuts remain available. Focus the viewer to type into
apps after taking control; mouse focus follows your normal Omarchy settings.
**Ctrl+Alt** releases a manually enabled viewer keyboard grab.

**Watch / give to agent** returns input to the agent and keeps a read-only viewer
on the same workspace. **Take control** pauses agent input and makes the viewer
interactive. Every handover invalidates old agent input. Closing the viewer
leaves the desktop running; closing an interactive viewer leaves input paused
until you explicitly return it. `sideyard open work` reopens or focuses the workspace
and preserves its current control owner.

Up to two desktops can run at once. New desktops match the usable display size;
`--width 1920 --height 1080` overrides it. `sideyard start work --headless` skips the
host workspace and viewer, starts in agent control, and defaults to 1600×900.
An MCP `start` opens in watch mode so the agent can work immediately.

`sideyard control work paused` pauses input. `sideyard list` shows desktops, and
`sideyard windows work` lists their windows. Every CLI operation emits JSON; errors
have a code and message and exit unsuccessfully.

Without `--project`, apps work in the workspace’s private `work` directory.
An attached project is a writable bind of that exact directory; changes there
are real project changes. Use a Git worktree yourself when you want a separate
branch. Workspace configuration and app profiles persist across stop/start.
The project attachment also persists; stop the workspace before changing it.


Any agent that supports local stdio MCP can launch **`sideyard mcp`**. For clients
using JSON MCP configuration, replace the example home path with yours:

```json
{
  "mcpServers": {
    "sideyard": {
      "command": "/home/YOUR_USER/.local/bin/sideyard",
      "args": ["mcp"]
    }
  }
}
```

There are eleven tools: `list`, `start`, `stop`, `open`, `control`, `screenshot`,
`input`, `launch`, `windows`, `focus`, and `delete`. All desktop operations require an
explicit workspace name. No agent-specific plugin or SDK is needed.

The common agent loop is deliberately small:

1. `start` with `id` and optionally `project`; `launch` an installed app.
2. Use `windows` for compact app/title/geometry information, or `screenshot` to
   see the desktop. Screenshots return a PNG and a short `frame` identifier.
3. Pass that `frame` to `input`, with coordinates taken directly from the image.
   Set `screenshot: true` to get the next frame in the same round trip.

```json
{"id":"work","frame":"f1","actions":[{"type":"click","x":300,"y":200}],"screenshot":true}
```

Screenshots default to a 1280-pixel longest edge. Use `max_size` (320–3840) for
more detail, or `region: [x,y,width,height]` in native desktop pixels to zoom.
Frame mapping handles both resizing and cropping automatically. `image: false`
returns a local PNG path without putting an image in the agent context.
`focus` takes a window `address` from `windows`, plus the current `frame`.
Input can wait 0–2000 ms before its optional screenshot (`wait_ms`, default 150).

A frame belongs to one workspace and this MCP connection; take a new screenshot
after reconnecting. The last 32 frames are retained. Takeover, pause, return and
restart invalidate their input generations. Legacy clients can still send
`generation` instead of `frame`, using **native desktop pixel** coordinates.
Always respect human or paused ownership.

Agents with shell access can use the same operations directly:

```sh
sideyard screenshot work
sideyard input work --generation 1 '[{"type":"click","x":300,"y":200}]'
sideyard input work --generation 1 '[{"type":"type","text":"Hello Ω"}]'
sideyard input work --generation 1 '[{"type":"key","keys":["CTRL","A"]}]'
```

Input supports `move`, `click`, `drag` (`x`, `y`, `to_x`, `to_y`), `type`,
`key`, and `scroll` (`dx`, `dy`, positive down/right). Coordinates are screenshot
pixels (CLI captures stay at native resolution). Click supports `count: 2` for
double-click. Text supports Unicode, newlines and tabs, with up to
4096 UTF-8 bytes per action; keep batches small and observe between actions.
The native input helper holds one connection so keyboard state remains stable.


- Desktop configuration and apps: `~/.local/state/agent-workspaces/NAME/work/`
- Logs and lifecycle: `~/.local/state/agent-workspaces/NAME/`
- Screenshots: `~/.local/share/agent-workspaces/NAME/artifacts/`

The default screenshot is overwritten; `sideyard screenshot NAME --out /path/file.png`
keeps a named capture. Logs are local to each workspace.

Stop your running desktops, then run `make uninstall` to remove the CLI and
shell widget. Workspace files and existing recordings are preserved. Use **Delete…** in the panel to permanently remove a workspace, or run
`sideyard delete NAME --yes`. Deletion stops its apps and removes its private
files, profiles, logs and captures. Attached project folders outside that data
are kept; if an attached project lives inside workspace data, move it first.
The MCP `delete` tool requires `confirm: true`.

Legacy prototype replicas are preserved but hidden from the panel. Choose a new
workspace name when moving to this version. Existing workspace names and the `aw` command remain compatible.


On this machine the reliable route is a private Bubblewrap namespace containing
headless Sway as a display transport, installed Hyprland, and the Omarchy shell.
Sway is invisible; the desktop and its window behavior are Hyprland/Omarchy.
There is one backend and one native viewer. The namespace separates display,
IPC, D-Bus, app profiles, input devices, and process lifetime. It is same-user
isolation, not a security boundary for hostile code: other host files are
readable and attached project files are writable. Hardware controls and
personal services are not replicated. Apps inside the workspace must support
Wayland; the host viewer uses the installed Xwayland display.

Tested locally on 13 September 2026: Omarchy 4.0.3, Hyprland 0.56.2,
Aquamarine 0.15.0, Quickshell 0.3.1, Sway 1.12, WayVNC 0.10.1,
TigerVNC 1.16.2 and Bubblewrap 0.12.0. These builds use Lua Hyprland config.

```sh
make check                 # 13 focused regressions and manifest validation
python3 tests/smoke.py      # uses free desktop slots; opens one viewer, then cleans up
```

The combined smoke checks independent routing, Unicode and Ctrl shortcuts,
viewer takeover, stale input rejection, pause, MCP crop/frame coordinates,
action-plus-image responses, and owned-process shutdown. Native Super+5/6
switching and the bar’s Watch, Take control and Stop buttons were also exercised
on the host desktop.
The panel is also loaded and visually checked in an isolated Omarchy desktop.
MCP uses the standard [stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
and [tool contract](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).


The audit compared [Playwright MCP](https://github.com/microsoft/playwright-mcp)
and Anthropic’s [computer-use interface](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool).
The useful patterns here are compact text inspection before images, bounded
screenshots with correct coordinate conversion, zooming for detail, and batching
short actions. Browser DOM tools are left to browser agents; this server owns
the desktop, apps, input, and human handover.

Responses omit compositor internals, transport sockets and repeated filesystem
paths. Images use MCP image blocks, never base64 embedded in text. Screenshot
files sent as images are temporary and removed after encoding. Read-only tools
carry the standard MCP annotation. The schema remains a single small set of
tools rather than a catalogue of individual keys or per-agent wrappers.

On this display a full observation is 1280×707 rather than 2560×1414: 75% fewer
pixels. Exact token savings depend on the receiving model’s image processing.
Use cropped detail only when needed and `windows` when an image adds no value.
The package retains its original internal `agent-workspaces` storage paths and
`hoppcx.agent-workspaces` plugin ID so upgrades keep existing desktops and bar
settings. The public name and preferred command are Sideyard / `sideyard`.
