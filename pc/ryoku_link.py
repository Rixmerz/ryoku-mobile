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

State is pushed on every Hyprland event (window opened, workspace switched, ...), read from
Hyprland's event socket, so the phone updates as the desktop changes.

Live view. `view` creates a landscape headless Hyprland output (the phone's aspect, or 1920x1080 where
the desktop forces it, see PhoneView). A workspace no PC monitor is displaying is shown on it; one a
PC monitor is displaying is never moved: that monitor is mirrored instead. `VIDEO_PORT` serves an
H.264 Annex-B stream of whichever output the phone looks at (wf-recorder, one access unit per AUD
NAL). The phone's input goes to a virtual pointer bound to that same output, so a workspace never
disappears from the PC because the phone looks at it.
Requires Hyprland configured through its Lua API (`hl.dsp`, `hl.monitor`), wf-recorder and wtype.
"""

import asyncio
import json
import math
import os
import re
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
        self.reset_zoom()
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
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    work = asyncio.gather(server.serve_forever(), video.serve_forever(), watch_hyprland(), keep_reverse())
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
