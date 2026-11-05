#!/usr/bin/env python3
"""ryoku-link: shows and drives the Ryoku desktop's Hyprland workspaces from the phone, over USB.

The phone app connects to 127.0.0.1:7466 on itself; `adb reverse` tunnels that port over the
USB cable to a unix socket in this user's runtime directory (mode 0700, socket 0600), so
nothing is exposed to the network and no other local user can reach it. That matters since
the phone's input is injected into the desktop. The control protocol is newline-delimited
JSON, the same for any future transport:

  server -> phone  {"type": "state", "host": "...", "workspaces": [{"id", "name", "active",
                    "busy", "windows": [{"class", "title"}]}], "viewing": <id or null>}
  server -> phone  {"type": "view", "workspace": 3, "width": 1920, "height": 864, "port": 7467}
                   (sent again, unasked, whenever what the phone should show changes: width/height
                   and the stream's source follow what the PC does),
                   or {"type": "view", "workspace": 3, "error": "..."} when it could not be opened
  phone -> server  {"cmd": "workspace", "id": 3}            switch the PC monitor's workspace
  phone -> server  {"cmd": "view", "id": 3, "width": 1080, "height": 2400}
                   {"cmd": "unview"}
  phone -> server  {"cmd": "pointer", "action": "move"|"down"|"up", "x": 0..1, "y": 0..1,
                    "button": "left"|"right"|"middle"}
  phone -> server  {"cmd": "scroll", "dx": lines, "dy": lines, "end": bool}
  phone -> server  {"cmd": "zoom", "factor": 1..8, "x": 0..1, "y": 0..1}
                   zoom the PC's screen about that point of the viewed output (factor 1 = off)
  phone -> server  {"cmd": "key", "text": "abc"} or {"cmd": "key", "key": "Return", "mods": ["ctrl"]}
  phone -> server  {"cmd": "cast", "package": "com.google.android.youtube"}
                   send the app in front on the phone to a new PC workspace (see "Casting");
                   answered to that phone alone with {"type": "cast", "package": ..., "workspace": 7}
                   or {"type": "cast", "package": ..., "error": "..."}

State is pushed on every Hyprland event (window opened, workspace switched, ...), read from
Hyprland's event socket, so the phone updates as the desktop changes.

Live view. `view` creates a landscape headless Hyprland output (the phone's aspect, or 1920x1080 where
the desktop forces it, see PhoneView). A workspace no PC monitor is displaying is shown on it; one a
PC monitor is displaying is never moved: that monitor is mirrored instead. `VIDEO_PORT` serves an
H.264 Annex-B stream of whichever output the phone looks at (wf-recorder, one access unit per AUD
NAL). The phone's input goes to a virtual pointer bound to that same output, so a workspace never
disappears from the PC because the phone looks at it.
Casting. `cast` moves a running phone app onto an Android virtual display that scrcpy mirrors in a window
on the first PC workspace nothing uses; once the app is there, the PC monitor switches to that workspace
and focuses the window, and closing the window sends it back to the workspace it was on. A cast first ends
any live view and resets the screen zoom. The app keeps its state (`am display move-stack` moves the
existing task; launching it on the display would start a second task instead). Needs scrcpy and adb on the PC.
Requires Hyprland configured through its Lua API (`hl.dsp`, `hl.monitor`), wf-recorder and wtype.
"""

import asyncio
import hashlib
import itertools
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time

PORT = int(os.environ.get("RYOKU_LINK_PORT", "7466"))
VIDEO_PORT = int(os.environ.get("RYOKU_LINK_VIDEO_PORT", str(PORT + 1)))
# PORT and VIDEO_PORT are the phone's side of the tunnel; this side is a unix socket per port.
SOCKET_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "ryoku-link")
# The distro adb package is broken on this machine; prefer the SDK's copy when present.
ADB = os.environ.get("RYOKU_ADB") or (
    os.path.expanduser("~/Android/platform-tools/adb")
    if os.path.exists(os.path.expanduser("~/Android/platform-tools/adb"))
    else shutil.which("adb") or "adb"
)

# scrcpy mirrors a phone app on the PC (see "Casting"); None while it is not installed.
SCRCPY = os.environ.get("RYOKU_SCRCPY") or shutil.which("scrcpy")

# The headless output the phone looks through. Fixed name: a stale one left by a crash is
# found and removed at startup.
OUTPUT = "RYOKU-PHONE"
FPS = 30
# Encoding big frames on the CPU at 30 fps is the latency bottleneck, so the stream's long side
# is capped here and the phone letterboxes nothing: the output keeps the phone's aspect.
MAX_SIDE = 1920
# After this many seconds without phone input the PC's cursor and focus are handed back.
REST_AFTER = 2.0
# x264 for a live view: no B-frames or lookahead (latency), one thread and one slice per frame
# (a frame is then a single NAL after its AUD), headers repeated on every keyframe.
X264 = f"repeat-headers=1:keyint={FPS * 2}:aud=1:bframes=0:threads=1:sliced-threads=0:crf=26:vbv-maxrate=6000:vbv-bufsize=1500"

# What the phone may type besides plain text. Names are XKB keysyms, validated here because they
# reach wtype's argv; a single letter or digit is allowed for shortcuts such as Ctrl+C.
KEY_NAMES = frozenset(
    ["Return", "BackSpace", "Tab", "Escape", "Delete", "Home", "End", "Page_Up", "Page_Down",
     "Left", "Right", "Up", "Down", "space"] + [f"F{n}" for n in range(1, 13)]
)
MODS = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "super": "logo"}

clients: set[asyncio.StreamWriter] = set()


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} ryoku-link: {msg}", file=sys.stderr, flush=True)


def hyprctl(*args: str):
    out = subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True, timeout=3)
    return json.loads(out.stdout or "null")


def dispatch(*lua: str) -> bool:
    """Runs Lua dispatchers (`hl.dsp...`) as one hyprctl batch, so no frame is drawn between them."""
    if not lua:
        return True
    batch = " ; ".join(f"dispatch {expr}" for expr in lua)
    out = subprocess.run(["hyprctl", "--batch", batch], capture_output=True, text=True, timeout=5).stdout
    problems = [l for l in out.splitlines() if l.startswith(("error", "warning"))]
    for l in problems:
        log(f"hyprctl: {l}")
    return not problems


MAX_ZOOM = 8.0


def clamp_zoom(factor: float) -> float:
    return min(max(float(factor), 1.0), MAX_ZOOM)


def set_zoom(factor: float) -> bool:
    """Hyprland's own screen zoom (`cursor:zoom_factor`, 1.0 = off). It is global and follows the
    cursor, so it magnifies whichever output the cursor is on. This Hyprland is configured through
    Lua, where `hyprctl keyword` is refused: the option goes through `hl.config`."""
    out = subprocess.run(["hyprctl", "eval", f"hl.config({{ cursor = {{ zoom_factor = {clamp_zoom(factor):.3f} }} }})"],
                         capture_output=True, text=True, timeout=3).stdout.strip()
    if out != "ok":
        log(f"hyprctl: could not set the zoom: {out}")
    return out == "ok"


def focus_monitor(name: str) -> str:
    return f"hl.dsp.focus({{ monitor = {json.dumps(name)} }})"


def focus_workspace(ws: int, on_current_monitor: bool = False) -> str:
    return f"hl.dsp.focus({{ workspace = {int(ws)}{', on_current_monitor = true' if on_current_monitor else ''} }})"


def move_current_workspace(monitor: str) -> str:
    # hl.dsp.workspace.move ignores any id: it always moves the focused monitor's workspace.
    return f"hl.dsp.workspace.move({{ monitor = {json.dumps(monitor)} }})"


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def fit_size(width: int, height: int) -> tuple[int, int]:
    """The size scaled so the long side is at most MAX_SIDE, with even sides because yuv420p needs them."""
    k = min(1.0, MAX_SIDE / max(width, height))
    return max(2, round(width * k / 2) * 2), max(2, round(height * k / 2) * 2)


def output_size(width: int, height: int) -> tuple[int, int]:
    """The phone's screen as a landscape output (the app rotates to match, desktops are wide)."""
    return fit_size(max(width, height), min(width, height))


def mirror_target(ws: int, visible: dict[str, int]) -> str | None:
    """The PC monitor displaying `ws`, or None when no PC monitor is. Hyprland shows a workspace
    on one monitor only, so a displayed workspace is mirrored, never moved: moving it would make
    it disappear from the PC. `visible` maps each PC monitor (not the headless output) to its workspace."""
    return next((name for name, shown in visible.items() if shown == ws), None)


def snapshot() -> dict:
    workspaces = hyprctl("workspaces") or []
    monitors = hyprctl("monitors") or []
    # The phone's own output is not "the desktop": `active` and `busy` describe the PC's screens.
    pc = [m for m in monitors if m["name"] != OUTPUT]
    shown = {m["activeWorkspace"]["id"] for m in pc}
    focused = next((m for m in pc if m.get("focused")), pc[0] if pc else None)
    active = focused["activeWorkspace"]["id"] if focused else None
    viewing = view.ws if view is not None else None
    by_ws: dict[int, list] = {}
    for c in hyprctl("clients") or []:
        if c.get("mapped", True) and not c.get("hidden", False):
            by_ws.setdefault(c["workspace"]["id"], []).append(
                {"class": c.get("class") or c.get("initialClass") or "?", "title": c.get("title", "")[:80]}
            )
    return {
        "type": "state",
        # What this bridge understands beyond the base protocol: a phone that wants one it lacks says so
        # instead of waiting for an answer an older bridge never gives.
        "features": ["cast"],
        "host": socket.gethostname(),
        "viewing": viewing,
        "workspaces": [
            {"id": w["id"], "name": w["name"], "active": w["id"] == active, "busy": w["id"] in shown,
             "windows": by_ws.get(w["id"], [])}
            # Special (scratchpad) workspaces have negative ids; they are not desktops.
            for w in sorted(workspaces, key=lambda w: w["id"]) if w["id"] > 0
        ],
    }


async def broadcast() -> None:
    if not clients:
        return
    line = (json.dumps(snapshot()) + "\n").encode()
    for w in list(clients):
        try:
            w.write(line)
            await w.drain()
        except (ConnectionError, OSError):
            clients.discard(w)


async def reply(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write((json.dumps(msg) + "\n").encode())
    await writer.drain()


# ---- Virtual pointer pinned to one Wayland output, speaking the wire protocol directly ----
#
# No `wlrctl`, `ydotool` or uinput access is needed: Hyprland implements
# zwlr_virtual_pointer_manager_v1, and `create_virtual_pointer_with_output` makes absolute
# motion land on that output only. That is what lets the phone drive the headless output
# without the physical monitor's coordinates ever coming into play. Only the handful of
# requests this needs are encoded; there is no event loop and no file descriptor passing.
# (Kept in this file on purpose: the bridge is started as a single script, `python3 -I`.)

BTN = {"left": 0x110, "right": 0x111, "middle": 0x112}
# Absolute motion is sent on a fixed grid; the compositor scales it to the bound output.
EXTENT = 0xFFFF
AXIS_VERTICAL, AXIS_HORIZONTAL = 0, 1
AXIS_SOURCE_FINGER = 1
# One wheel notch in the protocol's axis units.
AXIS_PER_LINE = 15.0

# Object ids the client allocates, in creation order.
DISPLAY, REGISTRY = 1, 2


def _string(s: str) -> bytes:
    raw = s.encode() + b"\0"
    return struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def pack(obj: int, opcode: int, body: bytes = b"") -> bytes:
    return struct.pack("<II", obj, ((8 + len(body)) << 16) | opcode) + body


def _fixed(v: float) -> int:
    return round(v * 256)


def unpack(buf: bytes) -> tuple[list[tuple[int, int, bytes]], bytes]:
    """Splits a byte stream into (object, opcode, body) messages plus the unfinished tail."""
    msgs = []
    while len(buf) >= 8:
        obj, word = struct.unpack_from("<II", buf)
        size = word >> 16
        if size < 8:
            raise ValueError(f"malformed Wayland message size {size}")
        if len(buf) < size:
            break
        msgs.append((obj, word & 0xFFFF, buf[8:size]))
        buf = buf[size:]
    return msgs, buf


def _read_string(body: bytes, at: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<I", body, at)
    text = body[at + 4:at + 4 + n - 1].decode(errors="replace")
    return text, at + 4 + n + (-n % 4)


class VirtualPointer:
    def __init__(self, output_name: str, timeout: float = 3.0):
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        path = os.path.join(runtime, os.environ.get("WAYLAND_DISPLAY", "wayland-0"))
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self._buf = b""
        self._next_id = REGISTRY + 1
        try:
            self._id = self._setup(output_name)
        except BaseException:
            self.sock.close()
            raise

    def _alloc(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def _roundtrip(self) -> list[tuple[int, int, bytes]]:
        """Sends wl_display.sync and returns every event up to its callback."""
        cb = self._alloc()
        self.sock.sendall(pack(DISPLAY, 0, struct.pack("<I", cb)))
        got = []
        while True:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("Wayland compositor closed the connection")
            self._buf += chunk
            msgs, self._buf = unpack(self._buf)
            for m in msgs:
                if m[0] == DISPLAY and m[1] == 0:
                    obj, code = struct.unpack_from("<II", m[2])
                    raise RuntimeError(f"Wayland protocol error on object {obj}, code {code}: {_read_string(m[2], 8)[0]}")
                got.append(m)
            if any(m[0] == cb for m in got):
                return got

    def _bind(self, name: int, interface: str, version: int) -> int:
        new = self._alloc()
        self.sock.sendall(pack(REGISTRY, 0, struct.pack("<I", name) + _string(interface) + struct.pack("<II", version, new)))
        return new

    def _setup(self, output_name: str) -> int:
        self.sock.sendall(pack(DISPLAY, 1, struct.pack("<I", REGISTRY)))
        globals_: dict[str, list[tuple[int, int]]] = {}
        for obj, op, body in self._roundtrip():
            if obj == REGISTRY and op == 0:
                name = struct.unpack_from("<I", body)[0]
                iface, at = _read_string(body, 4)
                globals_.setdefault(iface, []).append((name, struct.unpack_from("<I", body, at)[0]))
        manager = globals_.get("zwlr_virtual_pointer_manager_v1")
        if not manager or not globals_.get("wl_output"):
            raise RuntimeError("compositor lacks zwlr_virtual_pointer_manager_v1 or has no outputs")

        # wl_output v4 announces its connector name; bind them all and match ours.
        outputs = {self._bind(n, "wl_output", min(v, 4)): None for n, v in globals_["wl_output"]}
        for obj, op, body in self._roundtrip():
            if obj in outputs and op == 4:
                outputs[obj] = _read_string(body, 0)[0]
        target = next((o for o, n in outputs.items() if n == output_name), None)
        if target is None:
            raise RuntimeError(f"no Wayland output named {output_name!r}")

        mgr_name, mgr_ver = manager[0]
        mgr = self._bind(mgr_name, "zwlr_virtual_pointer_manager_v1", min(mgr_ver, 2))
        ptr = self._alloc()
        # create_virtual_pointer_with_output(seat=null, output, id): needs manager v2.
        self.sock.sendall(pack(mgr, 2, struct.pack("<III", 0, target, ptr)))
        self._roundtrip()
        return ptr

    def _send(self, opcode: int, *fields: int) -> None:
        self.sock.sendall(pack(self._id, opcode, struct.pack("<" + "I" * len(fields), *fields)))

    @staticmethod
    def _now() -> int:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def _frame(self) -> None:
        self._send(4)

    def move_to(self, x: float, y: float) -> None:
        """x, y in 0..1 across the bound output."""
        cx = round(min(max(x, 0.0), 1.0) * EXTENT)
        cy = round(min(max(y, 0.0), 1.0) * EXTENT)
        self._send(1, self._now(), cx, cy, EXTENT, EXTENT)
        self._frame()

    def button(self, name: str, pressed: bool) -> None:
        self._send(2, self._now(), BTN[name], 1 if pressed else 0)
        self._frame()

    def scroll(self, dx: float, dy: float) -> None:
        """Smooth finger scrolling, in wheel lines (positive dy is a wheel turned down: content moves up)."""
        self._send(5, AXIS_SOURCE_FINGER)
        for axis, lines in ((AXIS_HORIZONTAL, dx), (AXIS_VERTICAL, dy)):
            if lines:
                self.sock.sendall(pack(self._id, 3, struct.pack("<IIi", self._now(), axis, _fixed(lines * AXIS_PER_LINE))))
        self._frame()

    def scroll_end(self) -> None:
        """Tells clients the finger lifted, so they can start kinetic scrolling."""
        for axis in (AXIS_HORIZONTAL, AXIS_VERTICAL):
            self._send(6, self._now(), axis)
        self._frame()

    def close(self) -> None:
        try:
            self._send(8)
        except OSError:
            pass  # compositor already gone; the socket close below releases the pointer anyway
        self.sock.close()


class PhoneView:
    """The headless output standing in for the phone's screen, and everything to undo with it.

    Viewing a workspace must never make it disappear from the PC, and Hyprland cannot show one
    workspace on two outputs. So a workspace a PC monitor is displaying stays where it is and the
    phone *mirrors* that monitor (`mirror`): the stream records it and the phone's input is bound
    to it. Every other workspace is pulled onto the headless output and handed back to its home
    monitor when the phone moves on, so the PC's own workspace shortcuts never land on an output
    nobody can see. While it is displayed here a workspace is still the PC's: PC windows and
    keybinds can reach it. `apply` re-checks all of this and is also run when the PC moves.

    On the headless output, Hyprland has one seat, so phone input moves the PC's cursor and
    keyboard focus onto it. They are handed back after REST_AFTER seconds of silence, unless the
    PC's own mouse already took the cursor off the output. A mirrored monitor needs no borrowing.
    """

    def __init__(self, owner: asyncio.StreamWriter, width: int, height: int):
        self.owner = owner
        self.width, self.height = output_size(width, height)
        self.ws: int | None = None  # the workspace the phone asked for
        self.mirror: str | None = None  # the PC monitor being mirrored; None: the headless output
        self.mirror_size = (self.width, self.height)
        self.mirror_native = self.mirror_size  # the mirrored monitor's own pixels
        self.pointer: VirtualPointer | None = None
        self.pointer_on = OUTPUT
        self.stream: asyncio.Task | None = None
        self.watcher: asyncio.Task | None = None
        # workspace -> the monitor it was pulled from, for those shown on the headless output
        self.pulled: dict[int, str] = {}
        # (monitor focused, cursor x, y) before the phone took the seat; None when not borrowed
        self.rest: tuple[str, float | None, float | None] | None = None
        self.pressed: set[str] = set()
        self.last_input = 0.0
        self.zoomed_ws: int | None = None  # the workspace the PC screen is zoomed on; None: not zoomed

    # ---- output lifecycle (blocking: run in a thread) ----

    def open(self) -> None:
        subprocess.run(["hyprctl", "output", "create", "headless", OUTPUT], capture_output=True, timeout=3)
        try:
            spec = f'hl.monitor({{ output = "{OUTPUT}", mode = "{self.width}x{self.height}@{FPS}", position = "auto-right", scale = 1 }})'
            # The desktop's `ryoku-monitor autoscale` runs on every hotplug and, for about 12 s,
            # re-asserts a 1920x1080 mode every 2 s over any size set meanwhile. Setting ours
            # in that window just loses, so it is waited out first (about 13 s on such a desktop).
            if shutil.which("ryoku-monitor"):
                self._wait_out_autoscale()
            # Hyprland also ignores a monitor rule sent before it has registered the new output,
            # so the rule is re-sent until the output reports the size. The encoder only starts
            # once the size has held: a size change under a running encoder garbles the stream.
            held = 0
            for _ in range(60):
                m = self.phone_monitor()
                if m and (m["width"], m["height"]) == (self.width, self.height):
                    held += 1
                else:
                    held = 0
                    subprocess.run(["hyprctl", "eval", spec], capture_output=True, timeout=3)
                if held == 5:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError(f"headless output never held the size {self.width}x{self.height}")
            self.reclaim_strays()
            self.pointer = VirtualPointer(OUTPUT)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _wait_out_autoscale() -> None:
        start = time.monotonic()
        seen, idle = False, 0
        while time.monotonic() - start < 25:
            if subprocess.run(["pgrep", "-f", "ryoku-monitor autoscale"], capture_output=True).returncode == 0:
                seen, idle = True, 0
            else:
                idle += 1
                # Not running for 1 s: it finished, or it never started (3 s grace to start).
                if idle >= 4 and (seen or time.monotonic() - start > 3):
                    return
            time.sleep(0.25)
        log("ryoku-monitor autoscale still running after 25 s; sizing the output anyway")

    def close(self) -> None:
        # Unconditional: a zoom this view lost track of (a failed reset, an earlier run) must not outlive it.
        self.zoomed_ws = None
        set_zoom(1.0)
        if self.pointer:
            self.pointer.close()
            self.pointer = None
        try:
            self.release()
        finally:
            # Removing the output sends whatever it still shows back to the PC without
            # touching the PC monitor's workspace.
            subprocess.run(["hyprctl", "output", "remove", OUTPUT], capture_output=True, timeout=3)

    @staticmethod
    def phone_monitor() -> dict | None:
        return next((m for m in hyprctl("monitors") or [] if m["name"] == OUTPUT), None)

    # ---- borrowing the seat ----

    def remember_pc(self) -> None:
        if self.rest is not None:
            return
        mons = hyprctl("monitors") or []
        pc = [m["name"] for m in mons if m["name"] != OUTPUT]
        focused = next((m["name"] for m in mons if m.get("focused")), None)
        if focused and focused != OUTPUT:
            pos = hyprctl("cursorpos") or {}
            self.rest = (focused, pos.get("x"), pos.get("y"))
        elif pc:
            self.rest = (pc[0], None, None)

    def _give_back(self) -> list[str]:
        """Dispatchers that return the seat to the PC; also forgets the borrow."""
        if self.rest is None:
            return []
        monitor, x, y = self.rest
        self.rest = None
        steps = [focus_monitor(monitor)]
        if x is not None and y is not None:
            steps.append(f"hl.dsp.cursor.move({{ x = {int(x)}, y = {int(y)} }})")
        return steps

    def restore_pc(self) -> None:
        """Idle hand-back: skipped when the PC's mouse already left the headless output."""
        if self.rest is None:
            return
        phone, pos = self.phone_monitor(), hyprctl("cursorpos") or {}
        if phone and phone["x"] <= pos.get("x", -1) < phone["x"] + phone["width"] and phone["y"] <= pos.get("y", -1) < phone["y"] + phone["height"]:
            dispatch(*self._give_back())
        self.rest = None

    async def rest_watcher(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if self.rest and not self.pressed and time.monotonic() - self.last_input > REST_AFTER:
                await asyncio.to_thread(self.restore_pc)

    # ---- which workspace the phone sees (blocking) ----

    @staticmethod
    def _send_home(monitor: str, was: int) -> list[str]:
        """With a workspace showing on the headless output: put it on `monitor`, restore what
        `monitor` was showing, and come back to the headless output."""
        return [move_current_workspace(monitor), focus_monitor(monitor), focus_workspace(was), focus_monitor(OUTPUT)]

    def _hand_back_steps(self, current: int, visible: dict[str, int]) -> list[str]:
        # What the home monitor shows *now* is restored, not what it showed when the workspace was
        # pulled: the PC may have moved on since, and it must not be switched away again.
        home = self.pulled.pop(current, None)
        if home is None or home not in visible:
            return []
        return self._send_home(home, visible[home])

    def reclaim_strays(self) -> None:
        """Hyprland parks some hidden workspaces on a freshly created output. Left there they would swallow the PC's own
        workspace shortcuts, so each goes back to a PC monitor, one dispatch batch for all."""
        phone = self.phone_monitor()
        mons = hyprctl("monitors") or []
        visible = {m["name"]: m["activeWorkspace"]["id"] for m in mons if m["name"] != OUTPUT}
        if phone is None or not visible:
            return
        home = next(iter(visible))
        shown = phone["activeWorkspace"]["id"]
        strays = [w["id"] for w in hyprctl("workspaces") or [] if w["monitor"] == OUTPUT and w["id"] != shown and w["id"] > 0]
        if not strays:
            return
        self.remember_pc()
        steps = [focus_monitor(OUTPUT)]
        for ws in strays:
            steps += [focus_workspace(ws, on_current_monitor=True), *self._send_home(home, visible[home])]
        dispatch(*steps, *self._give_back())

    def look(self) -> tuple[str | None, int, int]:
        """What the phone should be shown: the mirrored monitor (None: headless) and the stream size."""
        return (self.mirror, *(self.mirror_size if self.mirror else (self.width, self.height)))

    @property
    def source(self) -> str:
        return self.mirror or OUTPUT

    def apply(self, from_pc: bool = False) -> bool:
        """Makes the desktop match what the phone asked for (`ws`); True when what the phone should
        be shown changed. Idempotent, so it can run after every Hyprland event without looping.
        `from_pc`: the PC moved by itself (not the phone), see the takeover below."""
        mons = hyprctl("monitors") or []
        phone = next((m for m in mons if m["name"] == OUTPUT), None)
        if phone is None:
            raise RuntimeError("the headless output is gone")
        real = {m["name"]: m for m in mons if m["name"] != OUTPUT}
        visible = {n: m["activeWorkspace"]["id"] for n, m in real.items()}
        if not visible or self.ws is None:
            return False
        before, ws, current = self.look(), self.ws, phone["activeWorkspace"]["id"]
        target = mirror_target(ws, visible)
        if target is None and ws == current:
            # Switching the PC to a workspace the headless output holds only focuses that output,
            # where nobody can see it (and the phone is not borrowing the seat: `rest` is None).
            # Hand it to its home monitor and mirror that.
            if from_pc and phone.get("focused") and self.rest is None:
                target = self.pulled.pop(ws, None)
                if target not in visible:
                    target = next(iter(visible))
                dispatch(focus_monitor(OUTPUT), move_current_workspace(target), focus_monitor(target))
        elif target is not None:
            # The PC is displaying it: leave it alone. Whatever the headless output held goes home.
            steps = self._hand_back_steps(current, visible)
            if steps:
                self.remember_pc()
                dispatch(focus_monitor(OUTPUT), *steps, *self._give_back())
            elif self.rest is not None:
                dispatch(*self._give_back())
        else:
            homes = {w["id"]: w["monitor"] for w in hyprctl("workspaces") or []}
            self.remember_pc()
            steps = [focus_monitor(OUTPUT), *self._hand_back_steps(current, visible)]
            if homes.get(ws) in visible:
                self.pulled[ws] = homes[ws]
            steps.append(focus_workspace(ws, on_current_monitor=True))
            if not dispatch(*steps, *self._give_back()):
                raise RuntimeError(f"could not show workspace {ws} on the headless output")
        self.mirror = target
        if target:
            self.mirror_native = (real[target]["width"], real[target]["height"])
            self.mirror_size = fit_size(*self.mirror_native)
        if self.pointer_on != self.source and self.pointer is not None:
            fresh = VirtualPointer(self.source)
            old, self.pointer, self.pointer_on = self.pointer, fresh, self.source
            self.pressed.clear()
            old.close()
        changed = self.look() != before
        if self.zoomed_ws is not None and (changed or self.zoomed_ws != self.ws):
            self.reset_zoom()  # the zoom belonged to another picture
        return changed

    def release(self) -> None:
        """Returns the workspace the phone is looking at to its home monitor."""
        phone = self.phone_monitor()
        if phone is None:
            return
        visible = {m["name"]: m["activeWorkspace"]["id"] for m in hyprctl("monitors") or [] if m["name"] != OUTPUT}
        steps = self._hand_back_steps(phone["activeWorkspace"]["id"], visible)
        self.remember_pc()
        if steps:
            dispatch(focus_monitor(OUTPUT), *steps, *self._give_back())
        else:
            dispatch(*self._give_back())

    # ---- input (cheap socket writes, except the first event of a burst) ----

    def _touch(self) -> None:
        self.last_input = time.monotonic()
        if self.mirror is None:  # a mirrored PC monitor already has the seat
            self.remember_pc()

    def pointer_event(self, msg: dict) -> None:
        action, x, y, button = msg.get("action"), msg.get("x"), msg.get("y"), msg.get("button", "left")
        if self.pointer is None or action not in ("move", "down", "up") or button not in BTN or not (is_number(x) and is_number(y)):
            return
        self._touch()
        self.pointer.move_to(x, y)
        if action == "down":
            self.pointer.button(button, True)
            self.pressed.add(button)
        elif action == "up":
            self.pointer.button(button, False)
            self.pressed.discard(button)

    def scroll_event(self, msg: dict) -> None:
        dx, dy = msg.get("dx", 0), msg.get("dy", 0)
        if self.pointer is None or not (is_number(dx) and is_number(dy)):
            return
        self._touch()
        if dx or dy:
            self.pointer.scroll(dx, dy)
        if msg.get("end") is True:
            self.pointer.scroll_end()

    def zoom_event(self, msg: dict) -> None:
        """Mirrors the phone's pinch on the PC screen, for an audience watching it. Hyprland zooms
        about the cursor, so the pointer is first put on the zoom centre."""
        factor, x, y = msg.get("factor"), msg.get("x"), msg.get("y")
        if not (is_number(factor) and is_number(x) and is_number(y)):
            return
        if clamp_zoom(factor) <= 1.005:
            self.reset_zoom()
            return
        if self.pointer is None:
            return
        self._touch()
        self.pointer.move_to(x, y)
        if set_zoom(factor):
            self.zoomed_ws = self.ws

    def reset_zoom(self) -> None:
        if self.zoomed_ws is not None:
            self.zoomed_ws = None
            set_zoom(1.0)

    def key_event(self, msg: dict) -> None:
        text, key = msg.get("text"), msg.get("key")
        mods = [MODS[m] for m in msg.get("mods", []) if m in MODS] if isinstance(msg.get("mods"), list) else []
        if isinstance(text, str) and text:
            argv = ["--", text[:256]]
        elif isinstance(key, str) and (key in KEY_NAMES or re.fullmatch(r"[A-Za-z0-9]", key)):
            argv = [a for m in mods for a in ("-M", m)] + ["-k", key] + [a for m in reversed(mods) for a in ("-m", m)]
        else:
            return
        self._touch()
        # The seat's keyboard focus may have gone back to the PC (or the PC's mouse took it).
        dispatch(focus_monitor(self.source))
        done = subprocess.run(["wtype", *argv], capture_output=True, text=True, timeout=3)
        if done.returncode != 0:
            log(f"wtype failed ({done.returncode}): {done.stderr.strip()}")

    # ---- video ----

    async def stream_to(self, writer: asyncio.StreamWriter) -> None:
        """Pipes wf-recorder's H.264 for the output the phone looks at (alone) to the phone's video socket."""
        source, (width, height) = self.source, self.look()[1:]
        # A mirrored monitor larger than MAX_SIDE is scaled down to the size the phone was told.
        scale = ["-F", f"scale={width}:{height},format=yuv420p"] if self.mirror and self.mirror_native != (width, height) else []
        proc = await asyncio.create_subprocess_exec(
            "wf-recorder", "-o", source, *scale, "-y", "-r", str(FPS),
            # Constant frame rate (no damage tracking): the phone finds the end of a frame when the
            # next one starts, so a still desktop must keep producing frames.
            "-D", "-c", "libx264", "-x", "yuv420p", "-p", "preset=ultrafast", "-p", "tune=zerolatency",
            "-p", f"x264-params={X264}", "-m", "h264", "-f", "/dev/stdout",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            first = True
            while chunk := await proc.stdout.read(65536):
                if first:
                    log(f"first video bytes ({len(chunk)})")
                    first = False
                writer.write(chunk)
                await writer.drain()
            log(f"wf-recorder ended (exit {await proc.wait()})")
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()


view: PhoneView | None = None
view_lock = asyncio.Lock()


async def announce(v: PhoneView, changed: bool) -> None:
    """Tells the phone what to show. A new source needs a new encoder: dropping the video
    connection makes the phone reconnect (it retries every second) and get the right one."""
    if changed and v.stream:
        v.stream.cancel()
    width, height = v.look()[1:]
    await reply(v.owner, {"type": "view", "workspace": v.ws, "width": width, "height": height, "port": VIDEO_PORT})


async def start_view(writer: asyncio.StreamWriter, ws: int, width: int, height: int) -> None:
    global view
    async with view_lock:
        if view is not None and view.owner is not writer:
            await _end_view()  # the newest phone wins
        if view is None:
            candidate = PhoneView(writer, width, height)
            try:
                await asyncio.to_thread(candidate.open)
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                log(f"live view could not open: {e}")
                await reply(writer, {"type": "view", "workspace": ws, "error": "could not open the view"})
                return
            candidate.watcher = asyncio.create_task(candidate.rest_watcher())
            view = candidate
            log(f"live view opened: {candidate.width}x{candidate.height}")
        view.ws = ws
        changed = await asyncio.to_thread(view.apply)
        log(f"viewing workspace {ws} via {view.source}")
        await announce(view, changed)
    await broadcast()


follow_pending = False
follow_task: asyncio.Task | None = None


def follow_pc_soon() -> None:
    """Re-checks the view after the PC moved by itself; at most one check runs, and events that
    arrive meanwhile only ask for another pass (the check reads the state afresh)."""
    global follow_pending, follow_task
    if view is None:
        return
    follow_pending = True
    if follow_task is None or follow_task.done():
        follow_task = asyncio.create_task(follow_pc())


async def follow_pc() -> None:
    global follow_pending
    while follow_pending:
        follow_pending = False
        async with view_lock:
            v = view
            if v is None:
                return
            try:
                changed = await asyncio.to_thread(v.apply, True)
                if changed:
                    log(f"PC moved: workspace {v.ws} now via {v.source}")
                    await announce(v, True)
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                log(f"could not follow the PC: {e}")


async def _end_view() -> None:
    global view
    v, view = view, None
    if v is None:
        return
    for task in (v.watcher, v.stream):
        if task:
            task.cancel()
    await asyncio.gather(*(t for t in (v.watcher, v.stream) if t), return_exceptions=True)
    await asyncio.to_thread(v.close)
    log("live view closed")


async def end_view() -> None:
    async with view_lock:
        await _end_view()
    await broadcast()


# ---- Casting a phone app to the PC ----
#
# scrcpy creates an Android virtual display and mirrors it in a window; the app is then moved onto
# that display with `am display move-stack`, which keeps its task and so what it was doing (a playing
# video keeps playing). Starting the app on the display instead, as scrcpy's --start-app does, makes
# a second task and loses that state. Audio: --audio-source=playback sends the apps' audio to the PC
# (Android 13+); it stays off the phone's speaker, and apps that opt out of capture stay silent.
# Closing the window ends the virtual display; --no-vd-destroy-content, when this scrcpy has it, makes
# Android move the app back to the phone instead of closing it.
#
# The app has one task, so opening it on the phone during a cast (its icon, recents, its notification)
# pulls that task back to the phone's display. With system decorations the virtual display then falls
# back to the phone launcher's SecondaryDisplayLauncher, and the PC streamed the phone's home screen;
# --no-vd-system-decorations leaves no launcher on the display, so it simply stays empty until the
# watcher ends the cast. scrcpy's --start-app is no way around it: it moves that same task.
#
# The display is 1920x1080 at the phone's own density. At scrcpy's old 240 dpi it is 1280 dp wide, so
# YouTube drew its tablet layout (a player pane beside a comments pane that spun forever); at the
# phone's 400 dpi the smallest width stays phone-sized and the player fills the window like a phone
# turned sideways.

CAST_SIZE = "1920x1080"
CAST_SETUP_TIMEOUT = 25.0
# What an installed Android package name looks like: dotted identifiers, never a shell string.
PACKAGE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")
OWN_PACKAGE = "com.rixmerz.ryokumobile"
# The only apps that may be cast for now; add a package here to allow another one.
CASTABLE_PACKAGES = frozenset({"com.google.android.youtube"})
KEEP_APPS_FLAG = "--no-vd-destroy-content"
BARE_DISPLAY_FLAG = "--no-vd-system-decorations"  # no launcher on the virtual display (see above)
GRIM = shutil.which("grim")  # screenshots of the cast window, to notice a frozen picture; None: not checked
FROZEN_AFTER = 15.0  # seconds of an unchanged picture, while the video plays, before the cast is restarted
MAX_RECOVERIES = 2  # restarts for one stuck picture before the bridge leaves the cast alone


class CastError(Exception):
    """A cast that could not start; the message is shown on the phone."""


class Cast:
    def __init__(self, package: str, workspace: int, address: str, display: int, previous: int | None = None,
                 serial: str | None = None):
        self.package, self.workspace, self.address, self.display = package, workspace, address, display
        self.previous = previous  # the workspace the PC monitor showed before the cast; None: unknown
        self.serial = serial  # the phone's adb serial, for the health checks
        self.misses = 0  # consecutive failed health checks (see watch_casts)
        self.recoveries = 0  # times a frozen picture made the bridge cast this app again
        self.picture: str | None = None  # digest of what the window showed at the last frozen-picture check
        self.picture_since = 0.0  # time.monotonic() from which the window has shown exactly that picture


def valid_package(value) -> bool:
    return isinstance(value, str) and len(value) <= 255 and value != OWN_PACKAGE and PACKAGE_PATTERN.fullmatch(value) is not None


def free_workspace(workspaces: list[dict], monitors: list[dict]) -> int:
    """The lowest workspace id that has no windows and is shown on no monitor (the headless phone
    output included), so a cast never lands on something in use or on what the PC monitor shows."""
    used = {w["id"] for w in workspaces if w.get("windows", 0) > 0}
    used |= {m["activeWorkspace"]["id"] for m in monitors}
    return next(i for i in itertools.count(1) if i not in used)


def parse_stack_list(text: str) -> list[tuple[int, int, str]]:
    """(root task id, display id, package) for every task in `am stack list`."""
    found, root = [], None
    for line in text.splitlines():
        if m := re.match(r"RootTask id=(\d+) .*\bdisplayId=(\d+)", line):
            root = (int(m[1]), int(m[2]))
        elif root and (m := re.match(r"\s+taskId=\d+: ([^/\s]+)/", line)):
            found.append((root[0], root[1], m[1]))
    return found


def find_root_task(stack_list: str, package: str, display: int = 0) -> int | None:
    """The root task of `package` on `display`; `am stack list` puts the front-most first."""
    return next((root for root, shown_on, pkg in parse_stack_list(stack_list) if pkg == package and shown_on == display), None)


def parse_displays(dumpsys_display: str) -> dict[int, str]:
    """Display id -> type (INTERNAL, VIRTUAL, OVERLAY, ...) from `dumpsys display`."""
    return {int(m[1]): m[2] for m in re.finditer(r'mBaseDisplayInfo=DisplayInfo\{"[^"]*", displayId (\d+),.*?\btype (\w+),', dumpsys_display)}


def new_display(before: dict[int, str], after: dict[int, str]) -> int | None:
    """The display that appeared between two listings, preferring a virtual one."""
    fresh = sorted(i for i in after if i not in before)
    return next((i for i in fresh if after[i] == "VIRTUAL"), fresh[0] if fresh else None)


def cast_title(package: str) -> str:
    return f"ryoku-cast {package}"


def phone_density(wm_density: str) -> int | None:
    """The density the phone really uses, from `wm density`: an override wins over the physical one."""
    found = dict(re.findall(r"(Physical|Override) density: (\d+)", wm_density))
    value = found.get("Override") or found.get("Physical")
    return int(value) if value else None


def cast_display(density: int | None) -> str:
    return f"{CAST_SIZE}/{density}" if density else CAST_SIZE  # no density: scrcpy picks one for the size


def scrcpy_argv(scrcpy: str, serial: str, title: str, keep_apps: bool, display: str = CAST_SIZE,
                bare: bool = False) -> list[str]:
    argv = [scrcpy, "-s", serial, f"--new-display={display}", "--audio-source=playback", f"--window-title={title}"]
    return argv + [KEEP_APPS_FLAG] * keep_apps + [BARE_DISPLAY_FLAG] * bare


def exec_in_workspace(argv: list[str], workspace: int, log_path: str) -> str:
    """Lua dispatcher that runs `argv` on `workspace` without switching to it: the window only
    becomes the PC's focus once the app is on it (see show_cast). Hyprland runs the command through
    a shell, so every word is quoted; its output goes to a log."""
    command = f"env ADB={shlex.quote(ADB)} {shlex.join(argv)} >{shlex.quote(log_path)} 2>&1"
    return f"hl.dsp.exec_cmd({json.dumps(command, ensure_ascii=False)}, {{ workspace = \"{int(workspace)} silent\" }})"


def focus_window(address: str) -> str:
    return f"hl.dsp.focus({{ window = {json.dumps('address:' + address)} }})"


def pc_workspace(monitors: list[dict]) -> int | None:
    """The workspace the focused PC monitor shows (the phone's headless output is not a PC monitor)."""
    pc = [m for m in monitors if m["name"] != OUTPUT]
    focused = next((m for m in pc if m.get("focused")), pc[0] if pc else None)
    return focused["activeWorkspace"]["id"] if focused else None


def show_cast(cast: Cast) -> list[str]:
    """Dispatchers that put the PC on the cast's workspace with its window focused."""
    return [focus_workspace(cast.workspace), focus_window(cast.address)]


def return_steps(cast: Cast, monitors: list[dict]) -> list[str]:
    """Dispatchers that send the PC back to where it was before `cast`. None when the PC has moved on
    by itself (no monitor shows the cast's now empty workspace any more): it is not pulled back."""
    shown_on = next((m["name"] for m in monitors if m["activeWorkspace"]["id"] == cast.workspace), None)
    if cast.previous is None or shown_on is None:
        return []
    return [focus_monitor(shown_on), focus_workspace(cast.previous)]


def adb(serial: str, *args: str, timeout: float = 8) -> str:
    # `adb shell` joins its words into one remote shell line: callers pass validated names and integers only.
    return subprocess.run([ADB, "-s", serial, *args], capture_output=True, text=True, timeout=timeout).stdout


def poll(read, timeout: float, interval: float = 0.4):
    """The first truthy `read()` before the deadline, else None. Runs on a worker thread."""
    deadline = time.monotonic() + timeout
    while True:
        if value := read():
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def window_titled(title: str) -> dict | None:
    return next((c for c in hyprctl("clients") or [] if c.get("title") == title), None)


def window_at(address: str) -> dict | None:
    return next((c for c in hyprctl("clients") or [] if c.get("address") == address), None)


def top_package(stack_list: str, display: int) -> str | None:
    """The package of the front-most task on `display` (`am stack list` puts it first), None when empty."""
    return next((pkg for _, shown_on, pkg in parse_stack_list(stack_list) if shown_on == display), None)


def parse_playback_state(media_session: str, package: str) -> str | None:
    """The playback state name (PLAYING, PAUSED, ...) of `package`'s media session in
    `dumpsys media_session`, None when it has no session or the session reports no state."""
    current = None
    for line in media_session.splitlines():
        if m := re.match(r"\s*package=(\S+)", line):
            current = m[1]
        elif current == package and (m := re.match(r"\s*state=PlaybackState \{state=(\w+)\(", line)):
            return m[1]
    return None


def playback_state(serial: str, package: str) -> str | None:
    return parse_playback_state(adb(serial, "shell", "dumpsys", "media_session"), package)


def cast_problem(cast: Cast) -> str | None:
    """Why `cast` is no longer a working cast, None while it is. Blocking; adb failures propagate and
    mean "could not tell", never "dead". A cast works while its window exists, its virtual display
    exists and the app is the front-most task on that display: anything else on the display (the phone's
    launcher, when the app was closed, finished or moved back) is what the PC window would show."""
    if window_at(cast.address) is None:
        return "its window is gone"
    displays = adb(cast.serial, "shell", "dumpsys", "display")
    if not displays.strip():
        raise OSError("adb returned no display list")
    if cast.display not in parse_displays(displays):
        return "its virtual display is gone"
    top = top_package(adb(cast.serial, "shell", "am", "stack", "list"), cast.display)
    if top != cast.package:
        return f"{top or 'nothing'} is in front on the virtual display, not the app"
    return None


def window_picture(cast: Cast) -> str | None:
    """A digest of what the cast window shows on the PC right now, None when it cannot be seen: not on
    the workspace a monitor displays (Hyprland draws nothing for it then) or no grim. Blocking."""
    window = window_at(cast.address)
    if window is None or not GRIM or window["workspace"]["id"] not in {m["activeWorkspace"]["id"] for m in hyprctl("monitors") or []}:
        return None
    (x, y), (w, h) = window["at"], window["size"]
    shot = subprocess.run([GRIM, "-g", f"{x},{y} {w}x{h}", "-t", "ppm", "-"], capture_output=True, timeout=5)
    return hashlib.md5(shot.stdout).hexdigest() if shot.returncode == 0 and shot.stdout else None


def picture_frozen(cast: Cast) -> bool:
    """True when the window has shown the very same picture for FROZEN_AFTER seconds while the app's
    media session says it is playing: the audio goes on, the picture does not. Blocking.
    ponytail: a video that really stands still for that long (a music track over one image) looks the same;
    the watcher recasts at most MAX_RECOVERIES times for it, then leaves the cast alone."""
    digest, now = window_picture(cast), time.monotonic()
    if digest is None or digest != cast.picture:
        cast.picture, cast.picture_since = digest, now  # hidden, or the picture moved: start counting again
        if digest is not None:
            cast.recoveries = 0
        return False
    return now - cast.picture_since >= FROZEN_AFTER and playback_state(cast.serial, cast.package) == "PLAYING"


def close_cast_window(address: str) -> None:
    """Ends scrcpy, and with it the virtual display, by signalling the process of the window at `address`.
    Waits for the window to go; a scrcpy that ignores SIGTERM is killed."""
    for sig, wait in ((signal.SIGTERM, 4.0), (signal.SIGKILL, 2.0)):
        window = window_at(address)
        if window is None:
            return
        if window.get("pid"):
            try:
                os.kill(window["pid"], sig)
            except ProcessLookupError:
                pass  # it exited between the lookup and the signal: the goal
        poll(lambda: window_at(address) is None, wait, interval=0.2)


def resume_playback(serial: str, display: int, package: str, timeout: float = 12.0) -> str:
    """Presses play until the app's media session says PLAYING. Blocking. Returns "playing", "left"
    (the app is no longer in front on `display`) or "not playing".
    KEYCODE_MEDIA_PLAY (126), never PLAY_PAUSE, so an already playing video is not paused. It is only
    pressed for a session that is paused or stopped: with no session Android would hand the key to
    whatever media app played last. YouTube pauses a moment after the move, often after a first
    PLAYING reading, so it only counts once STEADY_READS readings in a row said PLAYING.
    The key reaches the media session wherever the app is: once the app has gone back to the phone,
    pressing it would start the video on the phone, its sound still captured by the closing cast."""
    playing_reads = [0]

    def settle() -> str | None:
        if top_package(adb(serial, "shell", "am", "stack", "list"), display) != package:
            return "left"
        state = playback_state(serial, package)
        if state in ("PAUSED", "STOPPED"):
            playing_reads[0] = 0
            adb(serial, "shell", "input", "-d", str(display), "keyevent", "126")
            return False
        if state != "PLAYING":
            playing_reads[0] = 0
            return False
        playing_reads[0] += 1
        return "playing" if playing_reads[0] >= STEADY_READS else None
    return poll(settle, timeout, interval=0.5) or "not playing"


STEADY_READS = 8  # readings 0.5 s apart: PLAYING must hold about 4 s before autoplay trusts it


def launch_cast(package: str) -> Cast:
    """Blocking: opens the scrcpy window and moves the app onto its display."""
    if not SCRCPY:
        raise CastError("scrcpy is not installed on the computer")
    help_text = subprocess.run([SCRCPY, "--help"], capture_output=True, text=True, timeout=5)
    help_text = help_text.stdout + help_text.stderr
    if "--new-display" not in help_text:
        raise CastError("this scrcpy is too old: it has no virtual display")
    # USB serials first: a TCP one (Wi-Fi) only works when adb was already connected over the network.
    serials = sorted(reverse_targets(), key=lambda s: ":" in s or s.startswith("adb-"))
    if not serials:
        raise CastError("no phone on adb: plug the cable in, or adb connect over Wi-Fi")
    serial = serials[0]
    if not adb(serial, "shell", "pm", "path", package).startswith("package:"):
        raise CastError("that app is not installed on the phone")
    root = find_root_task(adb(serial, "shell", "am", "stack", "list"), package)
    if root is None:
        raise CastError("that app is not running on the phone")
    before = parse_displays(adb(serial, "shell", "dumpsys", "display"))
    pc_monitors = hyprctl("monitors") or []
    previous = pc_workspace(pc_monitors)
    workspace = free_workspace(hyprctl("workspaces") or [], pc_monitors)
    title = cast_title(package)
    # Whatever still wears this title is a leftover (a bridge restart forgets its casts): adopting its
    # window would mistake it for the new one, and it keeps a dead virtual display open.
    for leftover in [c for c in hyprctl("clients") or [] if c.get("title") == title]:
        log(f"closing a leftover cast window ({leftover.get('address')})")
        close_cast_window(leftover["address"])
    log_path = os.path.join(SOCKET_DIR, f"cast-{package}.log")
    display_spec = cast_display(phone_density(adb(serial, "shell", "wm", "density")))
    argv = scrcpy_argv(SCRCPY, serial, title, KEEP_APPS_FLAG in help_text, display_spec, BARE_DISPLAY_FLAG in help_text)
    if KEEP_APPS_FLAG not in help_text:
        log(f"this scrcpy lacks {KEEP_APPS_FLAG}: closing the window will close {package} instead of returning it to the phone")
    if BARE_DISPLAY_FLAG not in help_text:
        log(f"this scrcpy lacks {BARE_DISPLAY_FLAG}: the phone launcher may show on the PC when {package} leaves the cast")
    if not dispatch(exec_in_workspace(argv, workspace, log_path)):
        raise CastError("the desktop refused to start scrcpy")
    window = None
    try:
        display = poll(lambda: new_display(before, parse_displays(adb(serial, "shell", "dumpsys", "display"))), CAST_SETUP_TIMEOUT)
        if display is None:
            raise CastError("scrcpy did not create a virtual display")
        adb(serial, "shell", "am", "display", "move-stack", str(root), str(display))
        if find_root_task(adb(serial, "shell", "am", "stack", "list"), package, display) is None:
            raise CastError("Android would not move the app to the virtual display")
        window = poll(lambda: window_titled(title), CAST_SETUP_TIMEOUT)
        if window is None:
            raise CastError("scrcpy opened no window")
    except CastError:
        try:
            with open(log_path) as f:
                log(f"scrcpy output: {f.read()[-400:].strip()}")
        except OSError:
            pass  # no log: scrcpy never started, the error above says why
        if (w := window_titled(title)) and w.get("pid"):
            os.kill(w["pid"], signal.SIGTERM)  # tears the virtual display down with it
        raise
    cast = Cast(package, window["workspace"]["id"], window["address"], display, previous, serial)
    if not dispatch(*show_cast(cast)):
        log(f"cast {package}: could not switch the PC to workspace {cast.workspace}")
    return cast


casts: dict[str, Cast] = {}
cast_lock = asyncio.Lock()  # the new display is found by diffing, so one cast starts at a time
CAST_POLL = 2.0  # seconds between health checks of the live casts
CAST_MISSES = 2  # consecutive failed checks before a cast is ended: a task switch passes through odd states


async def return_pc(ended: list[Cast]) -> None:
    """Sends the PC back to where it was before the cast (once: it cannot return to two places) and
    resets the screen zoom, which would otherwise be left on whatever the PC shows next."""
    monitors = await asyncio.to_thread(hyprctl, "monitors") or []
    for cast in ended:
        if steps := return_steps(cast, monitors):
            await asyncio.to_thread(dispatch, *steps)
            break
    await asyncio.to_thread(set_zoom, 1.0)


async def end_cast(cast: Cast, reason: str, return_to_previous: bool = True) -> None:
    """Ends a cast: closes its window (which tears the virtual display down), forgets it and returns the PC.
    The caller holds cast_lock."""
    if casts.get(cast.package) is cast:
        del casts[cast.package]
    log(f"cast {cast.package} ended: {reason}")
    await asyncio.to_thread(close_cast_window, cast.address)
    if return_to_previous:
        await return_pc([cast])


async def recast(cast: Cast) -> None:
    """Restarts a cast whose picture froze: a new scrcpy (a new video stream and virtual display) with
    the same app, shown where the old one was. The PC is not sent back in between, and it goes back to
    where it was before the first cast. The caller holds cast_lock."""
    await end_cast(cast, "the picture froze while the video played; casting it again", return_to_previous=False)
    for attempt in range(3):  # the app takes a moment to come back to the phone's own display
        try:
            fresh = await asyncio.to_thread(launch_cast, cast.package)
            break
        except (CastError, OSError, subprocess.SubprocessError) as e:
            log(f"cast {cast.package}: restart attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(2)
    else:
        await return_pc([cast])
        return
    fresh.previous, fresh.recoveries = cast.previous, cast.recoveries + 1
    casts[cast.package] = fresh
    log(f"cast {cast.package}: restarted on virtual display {fresh.display}, PC workspace {fresh.workspace}")
    await asyncio.to_thread(autoplay, fresh)


async def start_cast(writer: asyncio.StreamWriter, package: str) -> None:
    if package not in CASTABLE_PACKAGES:
        await reply(writer, {"type": "cast", "package": package, "error": "only YouTube can be sent to the PC for now"})
        return
    cast = None
    async with cast_lock:
        if existing := casts.get(package):
            # A cast the bridge still remembers is reused when it works and cleaned up when it does not:
            # a dead one must never answer "already busy" to the next Send to PC.
            try:
                problem = await asyncio.to_thread(cast_problem, existing)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                problem = f"the phone could not be reached ({e})"
            if problem is None:
                window = await asyncio.to_thread(window_at, existing.address)
                if window:
                    existing.workspace = window["workspace"]["id"]
                await asyncio.to_thread(dispatch, *show_cast(existing))
                log(f"cast {package}: already live on workspace {existing.workspace}, focused it")
                await reply(writer, {"type": "cast", "package": package, "workspace": existing.workspace})
                cast = existing
            else:
                await end_cast(existing, problem)
        if cast is None:
            cast = await launch_new_cast(writer, package)
    # Outside the lock: pressing play takes seconds, and the watcher must be free meanwhile to notice
    # the app leaving the display (opened again on the phone) and end the cast.
    if cast is not None:
        await asyncio.to_thread(autoplay, cast)


async def launch_new_cast(writer: asyncio.StreamWriter, package: str) -> Cast | None:
    """Opens a new cast of `package` and answers the phone; None when it failed. The caller holds cast_lock."""
    # The phone is leaving for the app: a live view (its headless output, a pulled workspace, a
    # screen zoom) must not be left on the PC next to the cast, nor shift which workspace is free.
    if view is not None:
        await end_view()
    await asyncio.to_thread(set_zoom, 1.0)
    try:
        cast = await asyncio.to_thread(launch_cast, package)
    except (CastError, OSError, subprocess.SubprocessError) as e:
        log(f"cast of {package} failed: {e}")
        await reply(writer, {"type": "cast", "package": package, "error": str(e) if isinstance(e, CastError) else "could not reach adb"})
        return None
    casts[package] = cast
    log(f"cast {package}: virtual display {cast.display}, PC workspace {cast.workspace}")
    await reply(writer, {"type": "cast", "package": package, "workspace": cast.workspace})
    return cast


def autoplay(cast: Cast) -> None:
    """Moving the task pauses the video: resume it and say whether it is playing. Blocking."""
    try:
        outcome = resume_playback(cast.serial, cast.display, cast.package)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"cast {cast.package}: could not resume playback ({e})")
        return
    said = {"playing": "playing", "left": "left the virtual display before it played; play is not pressed"}
    log(f"cast {cast.package}: {said.get(outcome, 'not playing (paused, or no playback to resume)')}")


async def reap_casts() -> None:
    """Forgets casts whose window is gone: closing it ends scrcpy and with it the virtual display."""
    if not casts:
        return
    open_windows = {c["address"] for c in await asyncio.to_thread(hyprctl, "clients") or []}
    ended = [cast for cast in casts.values() if cast.address not in open_windows]
    for cast in ended:
        del casts[cast.package]
        log(f"cast {cast.package} ended: its window was closed")
    if ended:
        await return_pc(ended)


async def watch_casts() -> None:
    """Ends a cast that stopped working, so the PC never keeps showing the phone's launcher and the
    next Send to PC is never refused by a cast nobody uses. Needs CAST_MISSES bad checks in a row."""
    while True:
        await asyncio.sleep(CAST_POLL)
        for cast in list(casts.values()):
            async with cast_lock:
                if casts.get(cast.package) is not cast:
                    continue
                try:
                    problem, frozen = await asyncio.to_thread(check_cast, cast)
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
                    log(f"cast {cast.package}: health check failed ({e})")
                    continue  # could not tell: the cast stays until adb answers
                cast.misses = cast.misses + 1 if problem else 0
                if problem and cast.misses >= CAST_MISSES:
                    await end_cast(cast, problem)
                elif frozen and cast.recoveries < MAX_RECOVERIES:
                    await recast(cast)
                elif frozen:
                    cast.picture_since = time.monotonic()  # said once per window of FROZEN_AFTER
                    log(f"cast {cast.package}: the picture looks frozen but was restarted {cast.recoveries} times already; leaving it")


def check_cast(cast: Cast) -> tuple[str | None, bool]:
    """(why the cast is dead or None, whether its picture is frozen). Blocking."""
    problem = cast_problem(cast)
    return problem, problem is None and picture_frozen(cast)


async def handle_video(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    v = view
    if v is None or v.pointer is None:
        writer.close()
        return
    if v.stream:
        v.stream.cancel()
    v.stream = asyncio.current_task()
    log("video connected")
    try:
        await v.stream_to(writer)
    except (ConnectionError, OSError):
        pass  # the phone closed the video socket: leaving the screen is the normal way this ends
    finally:
        writer.close()


async def on_command(writer: asyncio.StreamWriter, msg: dict) -> None:
    cmd = msg.get("cmd")
    if cmd in ("view", "unview"):
        log(f"phone asked: {cmd} {msg.get('id', '')}")
    if cmd == "workspace" and is_int(msg.get("id")):
        # Only plain numeric ids reach the dispatcher: the phone is not a shell.
        if view is not None:
            await asyncio.to_thread(view.restore_pc)
        await asyncio.to_thread(dispatch, focus_workspace(msg["id"]))
    elif cmd == "view" and is_int(msg.get("id")) and is_int(msg.get("width")) and is_int(msg.get("height")):
        if msg["id"] > 0 and 100 <= msg["width"] <= 10000 and 100 <= msg["height"] <= 10000:
            await start_view(writer, msg["id"], msg["width"], msg["height"])
    elif cmd == "unview":
        if view is not None and view.owner is writer:
            await end_view()
    elif cmd == "cast":
        if valid_package(msg.get("package")):
            await start_cast(writer, msg["package"])
        else:
            await reply(writer, {"type": "cast", "error": "not a package name"})
    elif view is not None and view.owner is writer:
        if cmd == "pointer":
            view.pointer_event(msg)
        elif cmd == "scroll":
            view.scroll_event(msg)
        elif cmd == "zoom":
            await asyncio.to_thread(view.zoom_event, msg)
        elif cmd == "key":
            await asyncio.to_thread(view.key_event, msg)


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    clients.add(writer)
    log(f"phone connected ({len(clients)})")
    await broadcast()
    try:
        while line := await reader.readline():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            try:
                await on_command(writer, msg)
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                log(f"command {msg.get('cmd')!r} failed: {e}")
    except (ConnectionError, OSError):
        pass
    finally:
        clients.discard(writer)
        if view is not None and view.owner is writer:
            await end_view()
        log(f"phone disconnected ({len(clients)})")


async def watch_hyprland() -> None:
    """Pushes a fresh snapshot after each burst of Hyprland events."""
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    path = f"{runtime}/hypr/{sig}/.socket2.sock"
    while True:
        try:
            reader, _ = await asyncio.open_unix_connection(path)
            while await reader.readline():
                # Coalesce: one window move can emit several events within a few milliseconds.
                await asyncio.sleep(0.05)
                await broadcast()
                follow_pc_soon()
                await reap_casts()
        except (OSError, ConnectionError) as e:
            log(f"Hyprland event socket unavailable ({e}); retrying")
            await asyncio.sleep(3)


def socket_path(port: int) -> str:
    return os.path.join(SOCKET_DIR, f"{port}.sock")


async def serve_private(handler, port: int) -> asyncio.Server:
    """Listens on this user's unix socket for `port`; refuses to take over a live instance's."""
    os.makedirs(SOCKET_DIR, mode=0o700, exist_ok=True)
    os.chmod(SOCKET_DIR, 0o700)
    path = socket_path(port)
    try:
        _, probe = await asyncio.open_unix_connection(path)
    except OSError:
        pass  # nobody is listening: a missing path or a stale socket left by a crash
    else:
        probe.close()
        await probe.wait_closed()
        raise SystemExit(f"ryoku-link: already running ({path} answers)")
    server = await asyncio.start_unix_server(handler, path)
    os.chmod(path, 0o600)
    return server


def reverse_targets() -> list[str]:
    """Serials to tunnel to: ANDROID_SERIAL alone when set (an emulator included), otherwise every
    attached USB phone. Emulators are skipped unless RYOKU_LINK_EMULATOR=1."""
    out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=5).stdout
    serials = [l.split()[0] for l in out.splitlines()[1:] if l.endswith("\tdevice")]
    wanted = os.environ.get("ANDROID_SERIAL")
    if wanted:
        return [s for s in serials if s == wanted]
    return [s for s in serials if not s.startswith("emulator-") or os.environ.get("RYOKU_LINK_EMULATOR") == "1"]


tunnelled: set[str] = set()


def clear_reverse() -> None:
    """Removes the tunnels this run created, so no port keeps pointing at a socket that is gone."""
    for serial in sorted(tunnelled):
        for port in (PORT, VIDEO_PORT):
            subprocess.run([ADB, "-s", serial, "reverse", "--remove", f"tcp:{port}"], capture_output=True, timeout=5)
    tunnelled.clear()


async def keep_reverse() -> None:
    """Re-creates the tunnels whenever a phone (re)appears; `adb reverse` is per connection."""
    while True:
        try:
            for serial in reverse_targets():
                for port in (PORT, VIDEO_PORT):
                    subprocess.run([ADB, "-s", serial, "reverse", f"tcp:{port}", f"localfilesystem:{socket_path(port)}"], capture_output=True, timeout=5)
                tunnelled.add(serial)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"adb unavailable ({e})")
        await asyncio.sleep(5)


async def main() -> None:
    server = await serve_private(handle, PORT)
    video = await serve_private(handle_video, VIDEO_PORT)
    log(f"listening on {socket_path(PORT)} (video {socket_path(VIDEO_PORT)}), adb at {ADB}")
    if any(m["name"] == OUTPUT for m in hyprctl("monitors") or []):
        log(f"removing stale output {OUTPUT} from a previous run")
        subprocess.run(["hyprctl", "output", "remove", OUTPUT], capture_output=True, timeout=3)
    set_zoom(1.0)  # a zoom left by a run that died while the phone was pinching
    # A cast window outlives a restart of this bridge, which no longer tracks it: left open it ends up
    # showing the phone's launcher and blocks the next cast.
    for client in hyprctl("clients") or []:
        if client.get("title", "").startswith(cast_title("")):
            log(f"closing cast window left by a previous run: {client['title']}")
            close_cast_window(client["address"])
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    work = asyncio.gather(server.serve_forever(), video.serve_forever(), watch_hyprland(), keep_reverse(), watch_casts())
    waiter = asyncio.ensure_future(stop.wait())
    try:
        done, _ = await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            work.result()
    finally:
        work.cancel()
        waiter.cancel()
        await asyncio.gather(work, waiter, return_exceptions=True)  # retrieves the cancellation so asyncio does not print it
        await end_view()  # the headless output must not outlive the bridge
        await asyncio.to_thread(clear_reverse)


if __name__ == "__main__":
    asyncio.run(main())
