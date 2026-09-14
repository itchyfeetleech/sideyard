"""In-namespace session supervisor: staged boot plus the session socket.

Runs as the bwrap payload (one per session). Boots private D-Bus, the
headless Sway host, the nested Hyprland desktop and the Omarchy shell, then
serves spawn/input/hyprctl/shutdown requests from the host-side manager
over a private Unix socket. Owns one persistent aw-input helper for the
session lifetime (started lazily on first input).

Boot failures are recorded in boot-error.json; success in ready.json.
"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import paths

INPUT_BATCH_CAP = 32  # Keep cancellation latency bounded
HELPER_BIN = (Path(__file__).resolve().parent.parent.parent
              / "build" / "aw_input" / "aw-input")

_ACTION_RES = (
    re.compile(r"M \d+ \d+"),
    re.compile(r"B \d+ [01]"),
    re.compile(r"S -?\d+ -?\d+"),
    re.compile(r"K \d+ [01]"),
    re.compile(r"T .+"),
    re.compile(r"C"),
)


def check_action(line: str) -> str | None:
    """Validate one helper command line; None means acceptable."""
    if not isinstance(line, str):
        return "action must be a string"
    if len(line) > 4000:
        return "action too long"
    if re.search(r"[\x00-\x1f]", line):
        return "action has control characters"
    if not any(rx.fullmatch(line) for rx in _ACTION_RES):
        return f"malformed action: {line[:60]!r}"
    if line.startswith("T "):
        nbytes = len(line[2:].encode("utf-8"))
        if not 1 <= nbytes <= 1024:
            return "typed text must be 1..1024 bytes"
    return None


def current_generation(sdir: Path) -> int:
    """Control generation from the manager-owned lifecycle record."""
    state_path = sdir / "state.json"
    if state_path.is_file():
        obj = paths.read_json(state_path)
        assert isinstance(obj, dict)
        try:
            return int(obj.get("generation", 0))
        except (TypeError, ValueError):
            pass
    return 0


def _log_fh(session: Path, name: str):
    fh = open(session / name, "a", encoding="utf-8", buffering=1)
    os.chmod(session / name, 0o600)
    return fh


class Session:
    def __init__(self, session_id: str, width: int, height: int):
        self.id = paths.check_session_id(session_id)
        self.width = width
        self.height = height
        self.sdir = paths.session_dir(self.id)
        self.wdir = paths.work_dir(self.id)
        # In-namespace runtime root (/run/user/<uid>); the same directory is
        # visible on the host under the longer session store path.
        self.ns_rt = Path(os.environ["XDG_RUNTIME_DIR"])
        self.host_rt = paths.session_runtime_dir(self.id)
        self.log = _log_fh(self.sdir, "nsinit.log")
        self.spawn_seq = 0
        self.children: dict[int, subprocess.Popen] = {}
        # Persistent input helper. Kept out of self.children: it may be
        # reaped by the SIGCHLD handler, so liveness is probed explicitly.
        self.helper: subprocess.Popen | None = None
        self.base_env = self._clean_env()
        self.swaysock = ""
        self.outer_display = ""
        self.outer_output = ""
        self.inner_sig = ""
        self.inner_display = ""
        self.inner_output = ""

    def say(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, file=self.log, flush=True)

    def _clean_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not (k == "DISPLAY" or k.startswith("HYPRLAND_")
                       or k in ("SWAYSOCK", "WAYLAND_DISPLAY",
                                "XDG_ACTIVATION_TOKEN"))}
        env.update(GDK_BACKEND="wayland", QT_QPA_PLATFORM="wayland",
                   SDL_VIDEODRIVER="wayland", ELECTRON_OZONE_PLATFORM_HINT="wayland",
                   GDK_SCALE="1", GDK_DPI_SCALE="1", QT_SCALE_FACTOR="1")
        return env

    def spawn_tracked(self, argv: list[str], env: dict[str, str], logname: str,
                      cwd: str | None = None) -> subprocess.Popen:
        fh = _log_fh(self.sdir, logname)
        proc = subprocess.Popen(argv, env=env, cwd=cwd or str((paths.read_json(self.sdir / "state.json")).get("project") or self.wdir),
                                stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT,
                                close_fds=True)
        self.children[proc.pid] = proc
        self.say(f"spawned {' '.join(argv[:2])} pid={proc.pid} log={logname}")
        return proc

    def reap(self) -> None:
        for pid, proc in list(self.children.items()):
            if proc.poll() is not None:
                del self.children[pid]

    # -- boot stages ------------------------------------------------------

    def boot_dbus(self) -> str:
        bus_path = self.ns_rt / "bus"
        if bus_path.exists():
            bus_path.unlink()
        addr = f"unix:path={bus_path}"
        env = dict(self.base_env)
        env["DBUS_SESSION_BUS_ADDRESS"] = addr
        env["XDG_RUNTIME_DIR"] = str(self.ns_rt)
        self.spawn_tracked(["dbus-daemon", "--session", "--nofork",
                            f"--address={addr}"], env, "dbus.log")
        deadline = time.time() + 10
        while time.time() < deadline:
            if bus_path.exists():
                self.say(f"dbus ready at {addr}")
                return addr
            time.sleep(0.1)
        raise RuntimeError("dbus-daemon did not create its socket")

    def write_outer_config(self) -> Path:
        cfg = self.sdir / "outer-sway.conf"
        cfg.write_text(
            f"output HEADLESS-1 resolution {self.width}x{self.height} "
            f"position 0,0 bg #101018 solid_color\n"
            "for_window [all] fullscreen enable\n"
            "xwayland disable\n"
            "bar {\n"
            "  mode hide\n"
            "}\n",
            encoding="utf-8")
        return cfg

    def boot_sway(self, dbus_addr: str, cfg: Path) -> None:
        env = dict(self.base_env)
        env.update({
            "WLR_BACKENDS": "headless",
            "WLR_LIBINPUT_NO_DEVICES": "1",
            "XDG_RUNTIME_DIR": str(self.ns_rt),
            "DBUS_SESSION_BUS_ADDRESS": dbus_addr,
        })
        sway = self.spawn_tracked(["sway", "-c", str(cfg)], env, "sway.log")
        sock = self.ns_rt / f"sway-ipc.{os.getuid()}.{sway.pid}.sock"
        deadline = time.time() + 20
        while time.time() < deadline:
            if sock.exists():
                break
            if sway.poll() is not None:
                raise RuntimeError(f"sway exited early code={sway.returncode}")
            time.sleep(0.1)
        else:
            raise RuntimeError("sway IPC socket did not appear")
        self.swaysock = str(sock)
        self.say(f"sway ipc at {self.swaysock}")
        # Sway auto-names its socket; the private runtime holds exactly
        # this one outer socket.
        deadline = time.time() + 10
        while time.time() < deadline:
            socks = sorted(p.name for p in self.ns_rt.glob("wayland-*")
                           if not p.name.endswith(".lock"))
            if len(socks) == 1:
                self.outer_display = socks[0]
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("outer Wayland socket not found/ambiguous")
        self.say(f"outer display {self.outer_display}")
        # Sway 1.12 auto-creates a headless output at startup; create one
        # only if none exists (sway 1.12 has no destroy_output, so extras
        # from create_output cannot be removed again).
        outputs = json.loads(self._swaymsg(["-t", "get_outputs"]))
        if not outputs:
            out = self._swaymsg(["create_output"])
            self.say(f"create_output: {out.strip()[:200]}")
            outputs = json.loads(self._swaymsg(["-t", "get_outputs"]))
        if not outputs:
            raise RuntimeError("outer sway has no outputs")
        names = [str(o.get("name")) for o in outputs]
        target = "HEADLESS-1" if "HEADLESS-1" in names else names[0]
        set_out = self._swaymsg(
            ["output", target, "resolution",
             f"{self.width}x{self.height}", "position", "0", "0"])
        self.say(f"output set: {set_out.strip()[:200]}")
        outputs = json.loads(self._swaymsg(["-t", "get_outputs"]))
        infos = [(o.get("name"), o.get("current_mode")) for o in outputs]
        self.say(f"outer outputs: {infos}")
        match = [o for o in outputs if o.get("name") == target
                 and (o.get("current_mode") or {}).get("width") == self.width
                 and (o.get("current_mode") or {}).get("height") == self.height]
        if not match:
            raise RuntimeError(
                f"outer output {target} is not {self.width}x{self.height}: {infos}")
        self.outer_output = target
        if len(outputs) > 1:
            self.say(f"note: {len(outputs)} outer outputs; using {target}")

    def _swaymsg(self, args: list[str]) -> str:
        env = dict(self.base_env)
        env["SWAYSOCK"] = self.swaysock
        r = subprocess.run(["swaymsg", "-s", self.swaysock, *args],
                           env=env, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError(f"swaymsg {args} failed: {r.stderr.strip()[:300]}")
        return r.stdout

    def boot_hyprland(self, dbus_addr: str) -> None:
        env = dict(self.base_env)
        env.update({
            "WAYLAND_DISPLAY": self.outer_display,
            "XDG_RUNTIME_DIR": str(self.ns_rt),
            "DBUS_SESSION_BUS_ADDRESS": dbus_addr,
            "HYPRLAND_NO_SD_VARS": "1",
            "HYPRLAND_NO_SD_NOTIFY": "1",
            "XDG_CURRENT_DESKTOP": "Hyprland",
            "XDG_SESSION_TYPE": "wayland",
            "XDG_SESSION_DESKTOP": "Hyprland",
        })
        hypr_cfg = self.wdir / ".config" / "hypr" / "hyprland.lua"
        stdout_log = self.sdir / "hyprland.log"
        try:
            stdout_mark = stdout_log.stat().st_size
        except OSError:
            stdout_mark = 0
        hypr = self.spawn_tracked(["Hyprland", "--config", str(hypr_cfg)],
                                  env, "hyprland.log")
        # Hyprland 0.56.2 always generates its own instance signature
        # (Compositor.cpp overwrites the env var), so discover the single
        # instance dir in this owned runtime.
        deadline = time.time() + 60
        while time.time() < deadline:
            if hypr.poll() is not None:
                raise RuntimeError(
                    f"Hyprland exited early code={hypr.returncode}")
            hypr_dir = self.ns_rt / "hypr"
            if hypr_dir.is_dir():
                kids = [p for p in hypr_dir.iterdir() if p.is_dir()]
                if len(kids) == 1:
                    self.inner_sig = kids[0].name
                    break
            time.sleep(0.2)
        else:
            raise RuntimeError("inner Hyprland instance dir did not appear")
        self.say(f"inner signature {self.inner_sig}")
        # Inner Wayland socket: the wayland-* socket that is not the outer one.
        deadline = time.time() + 30
        while time.time() < deadline:
            socks = sorted(p.name for p in self.ns_rt.glob("wayland-*")
                           if not p.name.endswith(".lock")
                           and p.name != self.outer_display)
            if len(socks) == 1:
                self.inner_display = socks[0]
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("inner Wayland socket not found/ambiguous")
        self.say(f"inner display {self.inner_display}")
        # Traffic kick (upstream Aquamarine nested-startup flush stall):
        # Aquamarine 0.15.0 queues its initial toplevel commit but only
        # flushes when outer traffic arrives; a silent headless outer
        # therefore never configures the inner window and no inner output
        # appears. Creating a second outer output emits wl_output traffic
        # that unsticks the flush exactly once; the inner maps fullscreen
        # on the focused first output and its own frame loop sustains
        # traffic afterwards. The extra idle output is inert and invisible.
        kick = self._swaymsg(["create_output"])
        self.say(f"traffic kick: {kick.strip()[:200]}")
        # The nested output boots disabled (see desktop monitor override)
        # so no buffer can precede the first configure ack. The kick above
        # guarantees the flushed commit is processed and configured; allow
        # a generous settle (local roundtrips are milliseconds), then
        # confirm the connection survived (a pre-ack buffer would have
        # killed it with "xdg_surface has never been configured").
        # Neither the ack log line (file buffering) nor the outer tree
        # (null-committed toplevel may stay unmapped) is a reliable live
        # signal, so they only fast-path the wait, never gate it.
        inst_log = self.ns_rt / "hypr" / self.inner_sig / "hyprland.log"
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                if "configure surface with" in inst_log.read_text(
                        encoding="utf-8", errors="replace"):
                    break
            except OSError:
                pass
            if self._outer_has_inner_window():
                break
            time.sleep(0.5)
        try:
            with open(stdout_log, "rb") as fh:
                fh.seek(stdout_mark)
                fresh = fh.read().decode("utf-8", errors="replace")
            complained = "never been configured" in fresh
        except OSError:
            complained = False
        if complained:
            raise RuntimeError("outer killed the inner connection pre-ack")
        self.say("inner connection alive past configure")
        from . import desktop as _desktop
        if not _desktop.clear_monitor_override(self.wdir):
            raise RuntimeError("session monitor override missing")
        self._hyprctl(["reload"])
        self.say("config reloaded without override")
        deadline = time.time() + 30
        infos: list[tuple[object, object, object, object]] = []
        while time.time() < deadline:
            try:
                mons = self._hyprctl(["monitors", "-j"])
            except RuntimeError:
                time.sleep(0.5)
                continue
            infos = [(m.get("name"), m.get("width"), m.get("height"),
                      m.get("scale")) for m in json.loads(mons)]
            if infos:
                break
            time.sleep(0.5)
        self.say(f"inner monitors: {infos}")
        if not infos:
            raise RuntimeError("inner Hyprland reports no monitors")
        self.inner_output = str(infos[0][0])

    def _outer_has_inner_window(self) -> bool:
        try:
            tree = json.loads(self._swaymsg(["-t", "get_tree"]))
        except (RuntimeError, ValueError):
            return False
        stack = [tree]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            if node.get("app_id") == "aquamarine":
                return True
            stack.extend(node.get("nodes", []) +
                         node.get("floating_nodes", []))
        return False

    def _hyprctl(self, args: list[str]) -> str:
        env = dict(self.base_env)
        env.update({
            "XDG_RUNTIME_DIR": str(self.ns_rt),
            "HYPRLAND_INSTANCE_SIGNATURE": self.inner_sig,
        })
        r = subprocess.run(["hyprctl", "-i", self.inner_sig, *args],
                           env=env, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError(f"hyprctl {args} failed: {r.stderr.strip()[:300]}")
        return r.stdout

    def boot_shell(self, dbus_addr: str) -> None:
        omarchy = os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")
        env = dict(self.base_env)
        env.update({
            "WAYLAND_DISPLAY": self.inner_display,
            "XDG_RUNTIME_DIR": str(self.ns_rt),
            "DBUS_SESSION_BUS_ADDRESS": dbus_addr,
            "HYPRLAND_INSTANCE_SIGNATURE": self.inner_sig,
            "OMARCHY_PATH": omarchy,
            "QS_NO_RELOAD_POPUP": "1",
        })
        path = env.get("PATH", "/usr/local/bin:/usr/bin")
        if f"{omarchy}/bin" not in path.split(":"):
            env["PATH"] = f"{omarchy}/bin:{path}"
        shell = self.spawn_tracked(["quickshell", "-n", "-p", f"{omarchy}/shell"],
                                   env, "shell.log")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if shell.poll() is not None:
                raise RuntimeError("Omarchy shell exited; see shell.log")
            if 'omarchy-bar' in self._hyprctl(["layers", "-j"]):
                return
            time.sleep(0.2)
        raise RuntimeError("Omarchy shell did not display its bar; see shell.log")

    # -- persistent input helper --------------------------------------------

    def helper_running(self) -> bool:
        proc = self.helper
        if proc is None or proc.returncode is not None:
            return False
        try:
            done, status = os.waitpid(proc.pid, os.WNOHANG)
        except ChildProcessError:
            # Already reaped by the SIGCHLD handler: dead.
            proc.returncode = 0
            return False
        if done == proc.pid:
            proc.returncode = os.waitstatus_to_exitcode(status)
            return False
        return True

    def start_helper(self, base: dict[str, str]) -> str | None:
        """Start the owned helper; None means it answered READY."""
        self.stop_helper()  # Close streams from a previously exited helper.
        if not HELPER_BIN.is_file():
            return f"helper binary missing: {HELPER_BIN}"
        env = dict(base)
        env["AW_EXTENT_W"] = str(self.width)
        env["AW_EXTENT_H"] = str(self.height)
        try:
            proc = subprocess.Popen(
                [str(HELPER_BIN)], env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=_log_fh(self.sdir,
                                                       "helper.log"),
                text=True, bufsize=1, close_fds=True)
        except OSError as exc:
            return f"helper spawn failed: {exc}"
        self.helper = proc
        ready, _, _ = select.select([proc.stdout], [], [], 15)
        if not ready:
            self.stop_helper()
            return "helper not ready: timeout"
        line = proc.stdout.readline().strip()
        if line != "READY":
            self.stop_helper()
            return f"helper not ready: {line[:80]!r}"
        self.say(f"input helper ready pid={proc.pid}")
        return None

    def stop_helper(self) -> None:
        """EOF releases held input; discard the stream before accepting another batch."""
        proc = self.helper
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()
        self.helper = None

    def helper_cmd(self, line: str, timeout: float = 15.0) -> str:
        proc = self.helper
        assert proc is not None and proc.stdin is not None
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"helper write failed: {exc}")
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            raise RuntimeError("helper ack timeout")
        ack = proc.stdout.readline().strip()
        if ack == "" and not self.helper_running():
            raise RuntimeError("helper exited mid-batch")
        return ack

    def op_input(self, req: dict, base: dict[str, str]) -> dict:
        actions = req.get("actions")
        gen = req.get("generation")
        if (not isinstance(actions, list) or len(actions) > INPUT_BATCH_CAP
                or not all(isinstance(a, str) for a in actions)):
            return {"ok": False,
                    "error": f"bad_batch: up to {INPUT_BATCH_CAP} actions"}
        if not isinstance(gen, int):
            return {"ok": False, "error": "bad_batch: generation must be int"}
        for action in actions:
            err = check_action(action)
            if err is not None:
                return {"ok": False, "error": f"bad_action: {err}"}
        cur = current_generation(self.sdir)
        if gen != cur:
            return {"ok": False,
                    "error": f"stale_generation: have {cur}, want {gen}",
                    "generation": cur}
        if not self.helper_running():
            err = self.start_helper(base)
            if err is not None:
                return {"ok": False, "error": f"unavailable: {err}"}
        # A dead helper is restarted lazily by the next batch; this one
        # fails explicitly rather than silently dropping input.
        for action in actions:
            state = paths.read_json(self.sdir / "state.json")
            if state.get("owner", "agent") != "agent" or gen != int(state.get("generation", 0)):
                self.op_cancel()
                return {"ok": False, "error": "stale_generation: control changed"}
            try:
                ack = self.helper_cmd(action)
            except RuntimeError as exc:
                # A timed-out command can still reply later. Sending C and reusing
                # this stream could mistake that late reply for cancellation.
                self.stop_helper()
                return {"ok": False, "error": f"unavailable: {exc}"}
            if ack != "OK":
                self.op_cancel()
                return {"ok": False,
                        "error": f"unavailable: helper {ack[:80]!r}"}
        return {"ok": True, "acked": len(actions), "generation": cur}

    def op_cancel(self) -> dict:
        if not self.helper_running():
            return {"ok": True, "note": "helper not started"}
        try:
            ack = self.helper_cmd("C")
        except RuntimeError as exc:
            self.stop_helper()
            return {"ok": False, "error": f"unavailable: {exc}"}
        if ack != "OK":
            self.stop_helper()
            return {"ok": False, "error": f"unavailable: cancel -> {ack[:80]!r}"}
        return {"ok": True}

    # -- socket API ---------------------------------------------------------

    def spawn_env(self, dbus_addr: str) -> dict[str, str]:
        omarchy = os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")
        env = dict(self.base_env)
        env.update({
            "WAYLAND_DISPLAY": self.inner_display,
            "XDG_RUNTIME_DIR": str(self.ns_rt),
            "DBUS_SESSION_BUS_ADDRESS": dbus_addr,
            "HYPRLAND_INSTANCE_SIGNATURE": self.inner_sig,
            "OMARCHY_PATH": omarchy,
        })
        return env

    def serve(self, dbus_addr: str) -> int:
        sock_path = self.sdir / "ns.sock"
        if sock_path.exists():
            sock_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        server.listen(8)
        server.settimeout(0.5)
        self.say("spawn socket ready")
        base = self.spawn_env(dbus_addr)
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                self.reap()
                if not (self.ns_rt / "hypr" / self.inner_sig / ".socket.sock").exists():
                    raise RuntimeError("workspace compositor exited")
                continue
            with conn:
                conn.settimeout(30)
                fh = conn.makefile("r", encoding="utf-8")
                for line in fh:
                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError:
                        conn.sendall(b'{"ok":false,"error":"bad_json"}\n')
                        continue
                    if not isinstance(req, dict):
                        conn.sendall(b'{"ok":false,"error":"bad_json"}\n')
                        continue
                    resp = self.handle(req, base)
                    conn.sendall((json.dumps(resp) + "\n").encode())
                    if req.get("op") == "shutdown" and resp.get("ok"):
                        server.close()
                        return 0

    def handle(self, req: dict, base: dict[str, str]) -> dict:
        op = req.get("op")
        if op == "spawn":
            argv = req.get("argv")
            if not isinstance(argv, list) or not argv or not all(
                    isinstance(a, str) for a in argv):
                return {"ok": False, "error": "bad_argv"}
            env = dict(base)
            extra = req.get("env") or {}
            if not isinstance(extra, dict):
                return {"ok": False, "error": "bad_env"}
            for key, val in extra.items():
                if key in ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                           "HYPRLAND_INSTANCE_SIGNATURE",
                           "DBUS_SESSION_BUS_ADDRESS"):
                    continue  # routing is owned by the session
                env[str(key)] = str(val)
            logname = req.get("log")
            if logname is None:
                self.spawn_seq += 1
                logname = f"app-{self.spawn_seq}.log"
            else:
                logname = str(logname)
            if "/" in logname or logname.startswith("."):
                return {"ok": False, "error": "bad_log"}
            try:
                proc = self.spawn_tracked([str(a) for a in argv], env,
                                          logname,
                                          cwd=req.get("cwd"))
            except OSError as exc:
                return {"ok": False, "error": f"spawn_failed: {exc}"}
            return {"ok": True, "pid": proc.pid, "log": logname}
        if op == "hyprctl":
            args = req.get("args")
            if not isinstance(args, list) or not all(
                    isinstance(a, str) for a in args):
                return {"ok": False, "error": "bad_args"}
            try:
                return {"ok": True, "stdout": self._hyprctl(
                    [str(a) for a in args])}
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)[:300]}
        if op == "input":
            return self.op_input(req, base)
        if op == "cancel":
            return self.op_cancel()
        if op == "shutdown":
            return {"ok": True}
        return {"ok": False, "error": "unknown_op"}

    def shutdown(self) -> None:
        self.say("shutdown requested")
        try:
            (self.sdir / "ns.sock").unlink()
        except OSError:
            pass
        if self.helper is not None and self.helper_running():
            try:
                assert self.helper.stdin is not None
                self.helper.stdin.write("Q\n")
                self.helper.stdin.flush()
                self.helper.wait(timeout=5)
            except (OSError, ValueError, subprocess.SubprocessError):
                try:
                    self.helper.kill()
                except OSError:
                    pass
        for pid, proc in sorted(self.children.items(), reverse=True):
            if proc.poll() is None:
                proc.terminate()
        deadline = time.time() + 8
        while time.time() < deadline and any(
                p.poll() is None for p in self.children.values()):
            time.sleep(0.1)
        for pid, proc in self.children.items():
            if proc.poll() is None:
                self.say(f"killing pid={pid}")
                proc.kill()
        self.reap()


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: nsinit <session-id> <width> <height>", file=sys.stderr)
        return 2
    session_id, width, height = argv[1], int(argv[2]), int(argv[3])
    sess = Session(session_id, width, height)
    def terminated(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminated)
    try:
        dbus_addr = sess.boot_dbus()
        cfg = sess.write_outer_config()
        sess.boot_sway(dbus_addr, cfg)
        sess.boot_hyprland(dbus_addr)
        sess.boot_shell(dbus_addr)
        ready = {
            "session_id": session_id,
            "geometry": {"width": width, "height": height},
            "swaysock": sess.swaysock,
            "outer_display": sess.outer_display,
            "outer_output": sess.outer_output,
            "dbus_address": dbus_addr,
            "host_dbus_address": f"unix:path={sess.host_rt}/bus",
            "inner_signature": sess.inner_sig,
            "inner_display": sess.inner_display,
            "inner_output": sess.inner_output,
            "host_runtime_dir": str(sess.host_rt),
            "ns_runtime_dir": str(sess.ns_rt),
            "pids": sorted(sess.children),
        }
        paths.atomic_write_json(sess.sdir / "ready.json", ready)
        sess.say(f"ready: {ready}")
        return sess.serve(dbus_addr)
    except Exception as exc:  # boot failure: record and exit
        sess.say(f"BOOT FAILED: {exc}")
        try:
            paths.atomic_write_json(sess.sdir / "boot-error.json",
                                    {"error": f"{type(exc).__name__}: {exc}"})
        except OSError:
            pass
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        sess.shutdown()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
