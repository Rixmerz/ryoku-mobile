"""A virtual pointer pinned to one Wayland output, speaking the wire protocol directly.

No `wlrctl`, `ydotool` or uinput access is needed: Hyprland implements
zwlr_virtual_pointer_manager_v1, and `create_virtual_pointer_with_output` makes absolute
motion land on that output only. That is what lets the phone drive the headless output
without the physical monitor's coordinates ever coming into play.

Only the handful of requests this needs are encoded; there is no event loop and no file
descriptor passing, so the standard library is enough.
"""

import os
import socket
import struct
import time

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

    def _send(self, opcode: int, *fields: int, signed: bool = False) -> None:
        fmt = "<" + ("i" if signed else "I") * len(fields)
        self.sock.sendall(pack(self._id, opcode, struct.pack(fmt, *fields)))

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
        """Smooth finger scrolling, in wheel lines (positive dy scrolls content down)."""
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
