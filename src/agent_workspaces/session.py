"""Workspace lifecycle, isolated process launch, capture and control handover."""

from __future__ import annotations

import json
import os
import shutil
import signal
import secrets
import struct
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import paths, desktop

SRC_DIR = Path(__file__).resolve().parent.parent  # repo src/ for PYTHONPATH

SESSION_CAP = 2

# Viewer frame-rate limit.
LIMITS = {
    "view_fps": 30,
}

# TigerVNC viewer flags shared by both modes; only ViewOnly differs.
# AlertOnFatalError=0 keeps a dropped connection from parking a modal
# dialog on the host desktop; the manager reports transport state instead.
VIEWER_BASE_ARGV = [
    "vncviewer",
    "-AcceptClipboard=0", "-SendClipboard=0", "-SendPrimary=0",
    "-SetPrimary=0", "-RemoteResize=0", "-ReconnectOnError=0",
    "-AlertOnFatalError=0",
]


class AwError(Exception):
    """Structured manager error with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


def build_bwrap_argv(session_id: str, width: int, height: int) -> list[str]:
    """Bubblewrap mounts for the private desktop.

    Same HOME pathname with private config/state/share/cache overlaid, private
    runtime at /run/user/<uid>, private /tmp and /proc, minimal /dev with only
    the GPU render node, masked system bus, PID namespace. Host Wayland
    sockets, DRM card/input/hidraw nodes and the user-manager socket stay out.
    """
    session_id = paths.check_session_id(session_id)
    home = str(Path.home())
    uid = os.getuid()
    work = paths.work_dir(session_id)
    paths.ensure_dir(paths.runtime_root())
    paths.ensure_dir(paths.session_runtime_dir(session_id))
    empty = paths.session_dir(session_id) / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    cache = work / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    # Private state/share trees must exist before the first application starts.
    for rel in (".config", ".local/state", ".local/share", ".cache"):
        (work / rel).mkdir(parents=True, exist_ok=True)
    # The session dir must stay visible at its normal path inside the
    # namespace even though .local/state is overlaid.
    mountpoint = work / ".local" / "state" / "agent-workspaces" / session_id
    mountpoint.mkdir(parents=True, exist_ok=True)
    argv = [
        "bwrap",
        "--unshare-pid",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--dir", "/dev/dri",
        *[arg for node in sorted(Path("/dev/dri").glob("renderD*"))
          for arg in ("--dev-bind", str(node), str(node))],
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        # The host session runtime dir IS the private /run/user/<uid> inside
        # the namespace, so in-namespace socket paths stay under the Unix
        # length limit despite Hyprland's long generated instance names.
        "--bind", str(paths.session_runtime_dir(session_id)),
        f"/run/user/{uid}",
        "--bind", str(empty), "/run/dbus",
        "--bind", str(work / ".config"), f"{home}/.config",
        "--bind", str(work / ".local/state"), f"{home}/.local/state",
        "--bind", str(paths.session_dir(session_id)),
        f"{home}/.local/state/agent-workspaces/{session_id}",
        "--bind", str(work / ".local/share"), f"{home}/.local/share",
        "--bind", str(cache), f"{home}/.cache",
        "--bind", str(work), str(work),
        "--setenv", "PYTHONPATH", str(SRC_DIR),
        "--setenv", "XDG_CONFIG_HOME", f"{home}/.config",
        "--setenv", "XDG_STATE_HOME", f"{home}/.local/state",
        "--setenv", "XDG_DATA_HOME", f"{home}/.local/share",
        "--setenv", "XDG_CACHE_HOME", f"{home}/.cache",
        "--setenv", "XDG_RUNTIME_DIR", f"/run/user/{uid}",
        "--unsetenv", "DISPLAY",
        "--unsetenv", "SWAYSOCK",
        "--unsetenv", "WAYLAND_DISPLAY",
        "--unsetenv", "HYPRLAND_INSTANCE_SIGNATURE",
        "--unsetenv", "HYPRLAND_INSTANCE_ROOT",
        "--",
        sys.executable, "-m", "agent_workspaces.nsinit",
        session_id, str(width), str(height),
    ]
    project = (read_state(session_id) or {}).get("project")
    if project:
        index = argv.index("--setenv")
        argv[index:index] = ["--bind", project, project]
    return argv


def read_ready(session_id: str) -> dict | None:
    ready = paths.session_dir(session_id) / "ready.json"
    if ready.is_file():
        obj = paths.read_json(ready)
        assert isinstance(obj, dict)
        return obj
    return None


def read_state(session_id: str) -> dict | None:
    """Manager-side lifecycle record (lifecycle/generation/geometry)."""
    state_path = paths.session_dir(session_id) / "state.json"
    if state_path.is_file():
        obj = paths.read_json(state_path)
        assert isinstance(obj, dict)
        return obj
    return None


def _write_state(session_id: str, **fields: object) -> dict:
    state = read_state(session_id) or {"session_id": session_id,
                                      "generation": 0}
    state.update(fields)
    paths.atomic_write_json(paths.session_dir(session_id) / "state.json",
                            state)
    return state


def _proc_start(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def _bwrap_pid(session_id: str) -> int:
    try:
        record = paths.read_json(paths.session_dir(session_id) / "bwrap.pid")
        if isinstance(record, dict) and record.get("start") == _proc_start(record["pid"]):
            return record["pid"] if record.get("start") else 0
    except (OSError, ValueError, KeyError):
        pass
    return 0


def _track_owned(session_id: str, pid: int) -> None:
    paths.atomic_write_json(paths.session_dir(session_id) / f"process-{pid}.json",
                            {"start": _proc_start(pid)})


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_live(session_id: str) -> bool:
    """True when the session claims to run and its namespace is alive."""
    state = read_state(session_id)
    return (state is not None
            and state.get("lifecycle") in ("starting", "ready")
            and _pid_alive(_bwrap_pid(session_id)))


def live_sessions(except_id: str | None = None) -> list[str]:
    """Ids of sessions currently holding a replica slot."""
    root = paths.state_root()
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name == except_id:
            continue
        try:
            paths.check_session_id(child.name)
        except ValueError:
            continue
        if is_live(child.name):
            found.append(child.name)
    return found


def list_sessions() -> list[dict]:
    """One status row per known session id (read-only; no reaping)."""
    root = paths.state_root()
    rows = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not (child / "work/.aw-desktop").exists():
                continue
            try:
                sid = paths.check_session_id(child.name)
            except ValueError:
                continue
            state = read_state(sid) or {}
            transport = state.get("transport") or {"mode": "none"}
            viewer_open = _owned_pid(sid, transport.get("viewer_pid"), ("vncviewer", "Xvnc")) is not None
            owner = state.get("owner", "agent")
            if owner == "human" and transport.get("viewer_pid") and not viewer_open:
                owner = "paused"
            rows.append({
                "session_id": sid,
                "lifecycle": state.get("lifecycle", "new"),
                "owner": owner,
                "viewer_open": viewer_open,
                "generation": state.get("generation", 0),
                "geometry": state.get("geometry"),
                "transport": transport.get("mode", "none"),
                "live": is_live(sid),
                "directory": str(paths.work_dir(sid)),
                "host_workspace": state.get("host_workspace"),
            })
    return rows


def status(session_id: str) -> dict:
    """Reconciled session status: reaps dead transports/viewers first."""
    session_id = paths.check_session_id(session_id)
    if not paths.session_dir(session_id).is_dir():
        raise AwError("unknown_session", repr(session_id))
    state = _reconcile(session_id)
    ready = read_ready(session_id)
    err_path = paths.session_dir(session_id) / "boot-error.json"
    return {
        "session_id": session_id,
        "state": state,
        "ready": ready,
        "live": is_live(session_id),
        "boot_error": err_path.read_text().strip() if err_path.is_file()
        else None,
    }


def inner_env(ready: dict) -> dict[str, str]:
    """Environment routing a host-side tool at the inner compositor.

    Uses the host-visible runtime path. Only short socket filenames
    (Wayland display, D-Bus) are reachable this way; Hyprland IPC must go
    through the in-namespace helper (see hyprctl) because its paths exceed
    the Unix socket length limit under the host prefix.
    """
    env = dict(os.environ)
    for key in ("DISPLAY", "SWAYSOCK", "XDG_ACTIVATION_TOKEN"):
        env.pop(key, None)
    env.update({
        "WAYLAND_DISPLAY": str(ready["inner_display"]),
        "XDG_RUNTIME_DIR": str(ready["host_runtime_dir"]),
        "HYPRLAND_INSTANCE_SIGNATURE": str(ready["inner_signature"]),
        "DBUS_SESSION_BUS_ADDRESS": str(ready["host_dbus_address"]),
    })
    return env


def ns_request(session_id: str, req: dict, timeout: float = 15.0) -> dict:
    session_id = paths.check_session_id(session_id)
    if not paths.session_dir(session_id).is_dir():
        raise AwError("unknown_session", repr(session_id))
    sock_path = paths.session_dir(session_id) / "ns.sock"
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        try:
            conn.connect(str(sock_path))
        except OSError:
            raise AwError("not_ready",
                          f"session {session_id} supervisor is not serving")
        conn.sendall((json.dumps(req) + "\n").encode())
        fh = conn.makefile("r", encoding="utf-8")
        try:
            line = fh.readline()
        except (TimeoutError, socket.timeout) as exc:
            raise AwError("timeout", f"{req.get('op')}: {exc}")
    finally:
        conn.close()
    if not line:
        raise AwError("unavailable", "supervisor closed the connection")
    resp = json.loads(line)
    assert isinstance(resp, dict)
    return resp


def _checked(session_id: str, req: dict, timeout: float = 15.0) -> dict:
    """Send a supervisor request; map its error codes to AwError."""
    resp = ns_request(session_id, req, timeout=timeout)
    if not resp.get("ok"):
        err = str(resp.get("error", "unavailable"))
        code, _, detail = err.partition(": ")
        if code in ("stale_generation", "bad_batch", "bad_action",
                    "bad_argv", "bad_env", "bad_log", "bad_args",
                    "unavailable", "spawn_failed"):
            raise AwError(code, detail or err)
        raise AwError("unavailable", err)
    return resp


def spawn_app(session_id: str, argv: list[str],
              env: dict[str, str] | None = None,
              cwd: str | None = None) -> dict:
    return _checked(session_id, {"op": "spawn", "argv": argv,
                                 "env": env or {}, "cwd": cwd})


def require_agent(session_id: str, generation: int) -> None:
    state = _reconcile(session_id)
    if state.get("session_id") != session_id or not paths.session_dir(
            session_id).is_dir():
        raise AwError("unknown_session", repr(session_id))
    current = int(state.get("generation", 0))
    if generation != current:
        # Stale callers get the specific signal first; the supervisor
        # re-checks generation anyway before each batch lands.
        raise AwError("stale_generation",
                      f"session {session_id} has {current}, want {generation}")
    if state.get("owner", "agent") != "agent":
        raise AwError(
            "not_owner",
            f"session {session_id} owned by {state.get('owner', 'agent')}")


def input_batch(session_id: str, actions: list[str],
                generation: int,
                timeout: float = 30.0) -> dict:
    """Run a bounded input batch on the session's owned helper.

    The caller must supply the generation from its last observation.
    Rejected unless the agent currently owns control.
    """
    require_agent(session_id, generation)
    return _checked(session_id, {"op": "input", "actions": actions,
                                 "generation": generation}, timeout=timeout)


def cancel_input(session_id: str, timeout: float = 15.0) -> dict:
    """Release all helper-held keys/buttons; batches are short so this
    runs as soon as the in-flight batch (at most 32 acked actions) lands."""
    return _checked(session_id, {"op": "cancel"}, timeout=timeout)


# -- human handover: watch / take_control / return / pause ------------------
# Owner gates agent input batches;
# observation (screenshots, window queries, app launches) stays available
# so supervision and the return-time fresh screenshot keep working.

def _owned_cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().split(b"\0")[:-1]
    except OSError:
        return []


def _owned_pid(session_id: str, pid: object, names: tuple[str, ...]) -> int | None:
    """Return pid only if it is alive and runs one of the owned binaries.

    Guards stop/kill paths against PID reuse after a manager restart.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        record = paths.read_json(paths.session_dir(session_id) / f"process-{pid}.json")
        if not record.get("start") or record["start"] != _proc_start(pid):
            return None
    except (OSError, ValueError):
        return None
    parts = _owned_cmdline(pid)
    if not parts:
        return None
    if Path(parts[0].decode("utf-8", "replace")).name not in names:
        return None
    return pid


def proc_gone(pid: int) -> bool:
    """True when pid is dead, including the unreaped-zombie case.

    Transport procs are spawned detached (no Popen handle retained), so a
    SIGTERMed child lingers as a zombie of this process and kill(pid, 0)
    keeps succeeding; only the /proc state tells the truth.
    """
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state == "Z"


def _terminate(pid: int, wait: float) -> bool:
    """SIGTERM then SIGKILL; True when the process is gone."""
    if proc_gone(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc_gone(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return proc_gone(pid)


def _reconcile(session_id: str) -> dict:
    """Reap dead viewer/wayvnc procs; a closed interactive viewer pauses.

    A dead human viewer never silently resumes the agent: the owner flips
    to paused until an explicit return. Returns the reconciled state.
    """
    state = read_state(session_id) or {"session_id": session_id,
                                       "generation": 0}
    transport = state.get("transport") or {"mode": "none"}
    mode = transport.get("mode", "none")
    if mode == "none":
        return state
    wayvnc = _owned_pid(session_id, transport.get("wayvnc_pid"), ("wayvnc",))
    viewer = _owned_pid(session_id, transport.get("viewer_pid"), ("vncviewer", "Xvnc"))
    changed = False
    if wayvnc is None:
        for key in ("view_sock", "ctl_sock"):
            try:
                Path(str(transport[key])).unlink(missing_ok=True)
            except OSError:
                pass
        state["transport"] = {"mode": "none"}
        if state.get("owner") == "human":
            state["owner"] = "paused"
        changed = True
    else:
        if transport.get("wayvnc_pid") != wayvnc:
            transport["wayvnc_pid"] = wayvnc
            changed = True
        if transport.get("viewer_pid") and viewer is None:
            transport["viewer_pid"] = None
            if mode == "human" and state.get("owner") == "human":
                # Viewer closed: desktop keeps running; input stays paused
                # until an explicit return (never silently resumed).
                state["owner"] = "paused"
            changed = True
    if changed:
        paths.atomic_write_json(paths.session_dir(session_id) / "state.json",
                                state)
    return state


def _require_live(session_id: str) -> dict:
    state = read_state(session_id)
    if state is None:
        raise AwError("unknown_session", repr(session_id))
    if not is_live(session_id):
        raise AwError("not_ready", f"session {session_id} is not running")
    return state


def _stop_transport(session_id: str, transport: dict) -> dict:
    """Kill a transport's owned viewer/wayvnc and retire its endpoint."""
    notes: dict[str, object] = {}
    viewer = _owned_pid(session_id, transport.get("viewer_pid"), ("vncviewer", "Xvnc"))
    if viewer is not None:
        notes["viewer_killed"] = _terminate(viewer, 5.0)
    wayvnc = _owned_pid(session_id, transport.get("wayvnc_pid"), ("wayvnc",))
    if wayvnc is not None:
        notes["wayvnc_killed"] = _terminate(wayvnc, 10.0)
    for key in ("view_sock", "ctl_sock"):
        sock = transport.get(key)
        if sock:
            try:
                Path(str(sock)).unlink(missing_ok=True)
            except OSError as exc:
                notes[key] = f"unlink failed: {exc}"
    return notes


def _start_wayvnc(session_id: str, ready: dict, generation: int,
                  mode: str) -> dict:
    """Start a manager-owned wayvnc on a generation+mode endpoint.

    The mode suffix keeps the human endpoint retired after return: the
    restarted watch transport never reuses the human socket name, so a
    disconnected human viewer cannot reconnect to anything input-enabled.
    """
    allow_input = mode == "human"
    rt = Path(str(ready["host_runtime_dir"]))
    view_sock = rt / f"view-g{generation}-{mode}.sock"
    ctl_sock = rt / f"view-g{generation}-{mode}-ctl.sock"
    for stale in (view_sock, ctl_sock):
        stale.unlink(missing_ok=True)
    # Private explicit config so no inherited user wayvnc config can leak
    # in; all behavior still comes from the argv below.
    conf = paths.session_dir(session_id) / f"wayvnc-g{generation}.conf"
    conf.write_text("use_relative_paths=true\n", encoding="utf-8")
    os.chmod(conf, 0o600)
    argv = ["wayvnc", "-n", f"Sideyard · AGENT — {session_id}", "-C", str(conf), "-o", str(ready["inner_output"]),
           "-f", str(LIMITS["view_fps"]), "-R", "-S", str(ctl_sock),
           "-u", str(view_sock)]
    if not allow_input:
        argv.insert(1, "-d")
    log_path = (paths.session_dir(session_id)
                / f"wayvnc-g{generation}.log")
    log_fh = open(log_path, "w", encoding="utf-8")
    os.chmod(log_path, 0o600)
    proc = subprocess.Popen(argv, env=inner_env(ready),
                            stdin=subprocess.DEVNULL,
                            stdout=log_fh, stderr=subprocess.STDOUT,
                            start_new_session=True, close_fds=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        if view_sock.exists():
            break
        if proc.poll() is not None:
            raise AwError("unavailable",
                          f"wayvnc exited code={proc.returncode}")
        time.sleep(0.2)
    else:
        proc.terminate()
        raise AwError("unavailable", "wayvnc socket did not appear")
    _track_owned(session_id, proc.pid)
    return {"view_sock": str(view_sock), "ctl_sock": str(ctl_sock),
            "conf": str(conf), "wayvnc_pid": proc.pid}


def _start_viewer(session_id: str, view_sock: str, generation: int,
                  interactive: bool) -> int:
    """Start the packaged TigerVNC viewer on a private endpoint."""
    argv = [*VIEWER_BASE_ARGV,
            "-ShortcutModifiers=Ctrl,Alt", "-Fullscreen=0", "-FullscreenSystemKeys=0",
            f"-ViewOnly={0 if interactive else 1}", view_sock]
    from . import host
    number = host.allocate(session_id) if host.available() else None
    kind = "human" if interactive else "watch"
    log_path = (paths.session_dir(session_id)
                / f"viewer-{kind}-g{generation}.log")
    log_fh = open(log_path, "w", encoding="utf-8")
    os.chmod(log_path, 0o600)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                            stdout=log_fh, stderr=subprocess.STDOUT,
                            start_new_session=True, close_fds=True)
    _track_owned(session_id, proc.pid)
    time.sleep(0.3)
    if proc.poll() is not None:
        raise AwError("viewer_failed", log_path.read_text().strip()[-500:])
    if number is not None:
        try:
            host.present(session_id, proc.pid, number)
        except Exception:
            _terminate(proc.pid, 2)
            raise
    return proc.pid


def _open_transport(session_id, ready, generation, mode, with_viewer):
    transport = {"mode": mode, "generation": generation, "viewer_pid": None,
                 **_start_wayvnc(session_id, ready, generation, mode)}
    try:
        if with_viewer:
            transport["viewer_pid"] = _start_viewer(
                session_id, transport["view_sock"], generation, mode == "human")
        return transport
    except Exception:
        _stop_transport(session_id, transport)
        raise


def watch(session_id: str, with_viewer: bool = True) -> dict:
    """Start (or reuse) the read-only watch transport at this generation.

    Server-side --disable-input enforces read-only; the viewer only mirrors
    it. Opening a human-owned desktop preserves interactive control.
    """
    session_id = paths.check_session_id(session_id)
    _require_live(session_id)
    state = _reconcile(session_id)
    if state.get("owner", "agent") == "human":
        return take_control(session_id, with_viewer=with_viewer)
    ready = read_ready(session_id)
    assert ready is not None
    gen = int(state.get("generation", 0))
    transport = state.get("transport") or {"mode": "none"}
    if (transport.get("mode") == "watch"
            and transport.get("generation") == gen
            and _owned_pid(session_id, transport.get("wayvnc_pid"), ("wayvnc",))):
        if with_viewer and not _owned_pid(session_id, transport.get("viewer_pid"),
                                          ("vncviewer", "Xvnc")):
            transport["viewer_pid"] = _start_viewer(
                session_id, str(transport["view_sock"]), gen, False)
            _write_state(session_id, transport=transport)
        if with_viewer:
            from . import host
            if host.available():
                host.present(session_id, transport['viewer_pid'])
        return {"session_id": session_id, "mode": "watch",
                "generation": gen, **transport}
    _stop_transport(session_id, transport)
    transport = _open_transport(session_id, ready, gen, "watch", with_viewer)
    _write_state(session_id, transport=transport)
    return {"session_id": session_id, "mode": "watch", "generation": gen,
            **transport}


def take_control(session_id: str, with_viewer: bool = True) -> dict:
    """Hand control to a human: bump generation, start input-enabled VNC.

    Pauses agent input first, then requires the helper's cancellation
    acknowledgement; on failure the session is left paused. Stops the
    watch transport and retires its endpoint before starting the human
    one. No agent input batches are accepted while human owns control.
    """
    session_id = paths.check_session_id(session_id)
    _require_live(session_id)
    state = _reconcile(session_id)
    if state.get("owner") == "human":
        transport = state.get("transport") or {"mode": "none"}
        if with_viewer:
            from . import host
            if host.available() and transport.get('viewer_pid'):
                host.present(session_id, transport['viewer_pid'])
        return {"session_id": session_id, "note": "already human",
                "generation": state.get("generation", 0), **transport}
    gen = int(state.get("generation", 0)) + 1
    _write_state(session_id, owner="paused", generation=gen)
    try:
        cancel_input(session_id)
    except AwError as exc:
        raise AwError("unavailable",
                      f"helper did not release input; left paused: {exc}")
    _stop_transport(session_id, state.get("transport") or {"mode": "none"})
    ready = read_ready(session_id)
    assert ready is not None
    transport = _open_transport(session_id, ready, gen, "human", with_viewer)
    _write_state(session_id, owner="human", transport=transport)
    return {"session_id": session_id, "mode": "human", "generation": gen,
            **transport}


def return_to_agent(session_id: str) -> dict:
    """End human control: tear down input, require fresh observation.

    Closes the interactive viewer, stops the input-enabled wayvnc and
    retires its endpoint (a disconnected human viewer cannot reconnect),
    then restarts the read-only watch transport and resumes agent
    ownership only after a fresh screenshot. Uncertain teardown or a
    failed screenshot leaves the session paused with an explicit error.
    """
    session_id = paths.check_session_id(session_id)
    _require_live(session_id)
    state = _reconcile(session_id)
    if state.get("owner", "agent") == "agent":
        return {"session_id": session_id, "owner": "agent",
                "note": "already agent"}
    _write_state(session_id, owner="paused", generation=int(state.get("generation", 0)) + 1)
    transport = state.get("transport") or {"mode": "none"}
    if transport.get("mode") == "human":
        notes = _stop_transport(session_id, transport)
        if notes.get("viewer_killed") is False:
            _write_state(session_id, transport={"mode": "none"})
            raise AwError("unavailable",
                          "interactive viewer would not exit; left paused")
        if notes.get("wayvnc_killed") is False:
            _write_state(session_id, transport={"mode": "none"})
            raise AwError("unavailable",
                          "input-enabled wayvnc would not exit; left paused")
        _write_state(session_id, transport={"mode": "none"})
    try:
        shot = screenshot(
            session_id,
            paths.artifacts_dir(session_id) / "return-fresh.png")
    except AwError as exc:
        raise AwError("unavailable",
                      f"no fresh observation; left paused: {exc}")
    # Watch/agent mode is the default resting state; the read-only
    # transport keeps the native viewer visible when a workspace was assigned.
    state = _reconcile(session_id)
    gen = int(state.get("generation", 0))
    ready = read_ready(session_id)
    assert ready is not None
    transport = state.get("transport") or {"mode": "none"}
    if not (transport.get("mode") == "watch"
            and transport.get("generation") == gen
            and _owned_pid(session_id, transport.get("wayvnc_pid"), ("wayvnc",))):
        _stop_transport(session_id, transport)
        transport = _open_transport(session_id, ready, gen, "watch",
                                    bool(state.get("host_workspace")))
    _write_state(session_id, owner="agent", transport=transport)
    return {"session_id": session_id, "owner": "agent", "generation": gen,
            "screenshot": shot, "transport": transport}


def pause(session_id: str) -> dict:
    """Pause all input and retire any interactive viewer connection."""
    session_id = paths.check_session_id(session_id)
    _require_live(session_id)
    state = _reconcile(session_id)
    _stop_transport(session_id, state.get("transport") or {"mode": "none"})
    _write_state(session_id, owner="paused", transport={"mode": "none"},
                 generation=int(state.get("generation", 0)) + 1)
    try:
        cancel_input(session_id)
    except AwError as exc:
        raise AwError("unavailable",
                      f"paused but helper did not release: {exc}")
    if state.get('host_workspace'):
        watch(session_id, with_viewer=True)
    return {"session_id": session_id, "owner": "paused"}


def start(session_id: str, width: int, height: int,
          timeout: float = 120.0) -> dict:
    """Launch the session namespace; wait for ready.json or boot-error."""
    session_id = paths.check_session_id(session_id)
    sdir = paths.session_dir(session_id)
    if not (320 <= width <= 7680 and 240 <= height <= 4320):
        raise AwError("bad_geometry", "use 320x240 through 7680x4320")
    if is_live(session_id):
        return read_ready(session_id) or {"session_id": session_id, "lifecycle": "starting"}
    holders = live_sessions(except_id=session_id)
    if len(holders) >= SESSION_CAP:
        raise AwError("session_cap",
                      f"two sessions already live: {', '.join(holders)}")
    desktop.prepare(session_id, width, height)
    for stale in ("ready.json", "boot-error.json", "ns.sock"):
        try:
            (sdir / stale).unlink()
        except FileNotFoundError:
            pass
    _write_state(session_id, lifecycle="starting", owner="agent",
                 generation=int((read_state(session_id) or {}).get("generation", secrets.randbits(48))) + 1,
                 transport={"mode": "none"},
                 geometry={"width": width, "height": height})
    # A previous boot cleared the ack-gated monitor override; re-apply it
    # (idempotent) so every start boots with the pre-ack guard in place.
    desktop.apply_monitor_override(paths.work_dir(session_id))
    rt = paths.ensure_dir(paths.session_runtime_dir(session_id))
    # Drop stale sockets/instance dirs from previous runs (owned runtime).
    for child in rt.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    log_path = sdir / "bwrap.log"
    log_fh = open(log_path, "w", encoding="utf-8")
    os.chmod(log_path, 0o600)
    argv = build_bwrap_argv(session_id, width, height)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                            stdout=log_fh, stderr=subprocess.STDOUT,
                            start_new_session=True, close_fds=True)
    paths.atomic_write_json(sdir / "bwrap.pid", {"pid": proc.pid, "start": _proc_start(proc.pid)})
    deadline = time.time() + timeout
    while time.time() < deadline:
        err_path = sdir / "boot-error.json"
        if err_path.is_file():
            stop(session_id)
            _write_state(session_id, lifecycle="failed")
            raise AwError("unavailable",
                          f"boot failed: {err_path.read_text().strip()}")
        ready = read_ready(session_id)
        if ready is not None and (sdir / "ns.sock").exists():
            ready["bwrap_pid"] = proc.pid
            paths.atomic_write_json(sdir / "ready.json", ready)
            _write_state(session_id, lifecycle="ready")
            return ready
        if proc.poll() is not None:
            detail = ""
            if err_path.is_file():
                detail = err_path.read_text().strip()
            _write_state(session_id, lifecycle="failed")
            raise AwError(
                "unavailable",
                f"namespace exited code={proc.returncode} {detail}")
        time.sleep(0.3)
    stop(session_id)
    _write_state(session_id, lifecycle="failed")
    raise AwError("timeout", f"session {session_id} did not become ready")


def _bwrap_gone(pid: int) -> bool:
    """True if bwrap has exited; reaps it when it is our own child.

    Without the waitpid, a stop() in the same process that called
    start() sees its own unreaped bwrap zombie via os.kill(pid, 0) and
    misreports a clean shutdown as a leftover process.
    """
    try:
        done, _status = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return True
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def stop(session_id: str, timeout: float = 25.0) -> dict[str, object]:
    """Shut down the session; verify only owned processes are gone.

    Idempotent for a stopped session; an unknown id fails explicitly.
    """
    session_id = paths.check_session_id(session_id)
    sdir = paths.session_dir(session_id)
    if not sdir.is_dir():
        raise AwError("unknown_session", repr(session_id))
    outcome: dict[str, object] = {"session_id": session_id}
    _write_state(session_id, lifecycle="stopping")
    # Viewer transports are host-side owned processes depending
    # on the inner compositor; stop them before namespace shutdown.
    state = _reconcile(session_id)
    transport = state.get("transport") or {"mode": "none"}
    if transport.get("mode") != "none":
        outcome["transport"] = _stop_transport(session_id, transport)
        if any(outcome["transport"].get(key) is False for key in ("viewer_killed", "wayvnc_killed")):
            raise AwError("unavailable", "Viewer transport did not stop; process records and files were kept")
        _write_state(session_id, transport={"mode": "none"})
    try:
        resp = ns_request(session_id, {"op": "shutdown"}, timeout=10.0)
        outcome["shutdown_ack"] = bool(resp.get("ok"))
    except AwError as exc:
        outcome["shutdown_ack"] = f"no ack: {exc}"
    bwrap_pid = _bwrap_pid(session_id)
    if bwrap_pid:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _bwrap_gone(bwrap_pid):
                break
            # nsinit owns SIGTERM ordering; escalate the group if stuck.
            try:
                os.killpg(bwrap_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.3)
        else:
            try:
                os.killpg(bwrap_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        outcome["bwrap_alive"] = not _bwrap_gone(bwrap_pid)
    # Owned-process check: every session process shares the bwrap process
    # group (in-namespace PIDs are meaningless on the host, so scan by pgid).
    # Allow a short drain: slow teardown (Qt, compositor) may outlive bwrap.
    def group_pids() -> list[int]:
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            try:
                # stat[0] is state, stat[2] is pgrp. Zombies hold no
                # resources and are reaped by their (possibly new) parent;
                # they must not fail a clean shutdown.
                if stat[0] != "Z" and int(stat[2]) == bwrap_pid:
                    found.append(int(entry.name))
            except (ValueError, IndexError):
                continue
        return sorted(found)

    group_left = group_pids() if bwrap_pid else []
    if group_left:
        deadline = time.time() + 5
        while group_left and time.time() < deadline:
            time.sleep(0.2)
            group_left = group_pids()
    outcome["group_pids_left"] = group_left
    if outcome.get("bwrap_alive") or group_left:
        raise AwError("unavailable", "Workspace processes did not stop; process records and files were kept")
    (sdir / "ready.json").unlink(missing_ok=True)
    _write_state(session_id, lifecycle="stopped", owner="agent")
    for record in sdir.glob("process-*.json"):
        record.unlink(missing_ok=True)
    return outcome


def delete(session_id: str) -> dict:
    """Stop and remove only this desktop's owned data, never an attached project."""
    session_id = paths.check_session_id(session_id)
    sdir = paths.session_dir(session_id)
    targets = (paths.session_runtime_dir(session_id), paths.share_root() / session_id, sdir)
    if any(p.is_symlink() for p in targets) or paths.work_dir(session_id).is_symlink():
        raise AwError("unsafe_delete", "Workspace directories must not be symbolic links")
    if not (paths.work_dir(session_id) / '.aw-desktop').is_file():
        raise AwError("unknown_session", "Only a Sideyard workspace can be deleted")
    state = read_state(session_id) or {}
    project = Path(state['project']).resolve() if state.get('project') else None
    if project and any(project.is_relative_to(p.resolve()) for p in targets):
        raise AwError("project_inside_workspace", "Move the attached project outside this workspace before deleting it")
    stopped = stop(session_id)
    transport = stopped.get('transport', {})
    if (stopped.get('bwrap_alive') or stopped.get('group_pids_left') or
            any(transport.get(key) is False for key in ('viewer_killed', 'wayvnc_killed'))):
        raise AwError("unavailable", "Some workspace processes are still running; its files were kept")
    for target in targets:
        if target.exists():
            shutil.rmtree(target)
    return {"session_id": session_id, "deleted": True, "project_preserved": str(project) if project else None}


def hyprctl(session_id: str, args: list[str], timeout: float = 20.0) -> str:
    resp = _checked(session_id, {"op": "hyprctl", "args": args},
                    timeout=timeout)
    return str(resp.get("stdout", ""))


def screenshot(session_id: str, dest: Path, max_size=None, region=None) -> dict[str, object]:
    """Capture the inner output with grim; return frame metadata."""
    _require_live(session_id)
    ready = read_ready(session_id)
    if ready is None:
        raise AwError("not_ready", f"session {session_id} is not ready")
    dest.parent.mkdir(parents=True, exist_ok=True)
    width, height = ready['geometry']['width'], ready['geometry']['height']
    x, y, capture_width, capture_height = 0, 0, width, height
    options = ["-o", str(ready["inner_output"])]
    if region is not None:
        if not isinstance(region, list) or len(region) != 4 or any(type(v) is not int for v in region):
            raise ValueError('region must be [x, y, width, height] in desktop pixels')
        x, y, capture_width, capture_height = region
        if x < 0 or y < 0 or capture_width <= 0 or capture_height <= 0 or x + capture_width > width or y + capture_height > height:
            raise ValueError('region is outside the desktop')
        options = ['-g', f'{x},{y} {capture_width}x{capture_height}']
    scale = 1.0
    if max_size is not None:
        if type(max_size) is not int or not 320 <= max_size <= 3840:
            raise ValueError('max_size must be 320–3840')
        scale = min(1.0, max_size / max(capture_width, capture_height))
        options += ['-s', str(scale)]
    before = time.monotonic()
    r = subprocess.run(
        ["grim", *options, str(dest)],
        env=inner_env(ready), capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AwError("unavailable", f"grim failed: {r.stderr.strip()[:300]}")
    with dest.open("rb") as png:
        header = png.read(24)
    image_width, image_height = struct.unpack(">II", header[16:24])
    return {
        "session_id": session_id,
        "path": str(dest),
        "monotonic": before,
        "generation": (read_state(session_id) or {}).get("generation", 0),
        "owner": (read_state(session_id) or {}).get("owner", "agent"),
        "width": image_width,
        "height": image_height,
        "region": [x, y, capture_width, capture_height],
        "scale": scale,
        "coordinates": "output pixels, origin top-left",
        "output": ready["inner_output"],
        "inner_display": ready["inner_display"],
    }
