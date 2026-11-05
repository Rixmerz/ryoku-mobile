#!/usr/bin/env python3
"""ryoku-link: shows the Ryoku desktop's Hyprland workspaces on the phone, over USB.

The phone app connects to 127.0.0.1:7466 on itself; `adb reverse` tunnels that port over the
USB cable to this server, which also listens on 127.0.0.1 only, so nothing is exposed to the
network. The protocol is newline-delimited JSON, the same for any future transport:

  server -> phone  {"type": "state", "host": "...", "workspaces": [{"id", "name", "active",
                    "windows": [{"class", "title"}]}]}
  phone -> server  {"cmd": "workspace", "id": 3}

State is pushed on every Hyprland event (window opened, workspace switched, ...), read from
Hyprland's event socket, so the phone updates as the desktop changes.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys

PORT = int(os.environ.get("RYOKU_LINK_PORT", "7466"))
# The distro adb package is broken on this machine; prefer the SDK's copy when present.
ADB = os.environ.get("RYOKU_ADB") or (
    os.path.expanduser("~/Android/platform-tools/adb")
    if os.path.exists(os.path.expanduser("~/Android/platform-tools/adb"))
    else shutil.which("adb") or "adb"
)

clients: set[asyncio.StreamWriter] = set()


def log(msg: str) -> None:
    print(f"ryoku-link: {msg}", file=sys.stderr, flush=True)


def hyprctl(*args: str):
    out = subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True, timeout=3)
    return json.loads(out.stdout or "null")


def snapshot() -> dict:
    workspaces = hyprctl("workspaces") or []
    active = (hyprctl("activeworkspace") or {}).get("id")
    by_ws: dict[int, list] = {}
    for c in hyprctl("clients") or []:
        if c.get("mapped", True) and not c.get("hidden", False):
            by_ws.setdefault(c["workspace"]["id"], []).append(
                {"class": c.get("class") or c.get("initialClass") or "?", "title": c.get("title", "")[:80]}
            )
    return {
        "type": "state",
        "host": socket.gethostname(),
        "workspaces": [
            {"id": w["id"], "name": w["name"], "active": w["id"] == active, "windows": by_ws.get(w["id"], [])}
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
            if msg.get("cmd") == "workspace" and isinstance(msg.get("id"), int):
                # Only plain numeric ids reach the dispatcher: the phone is not a shell.
                subprocess.run(["hyprctl", "dispatch", "workspace", str(msg["id"])], capture_output=True, timeout=3)
    except (ConnectionError, OSError):
        pass
    finally:
        clients.discard(writer)
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
        except (OSError, ConnectionError) as e:
            log(f"Hyprland event socket unavailable ({e}); retrying")
            await asyncio.sleep(3)


async def keep_reverse() -> None:
    """Re-creates the USB tunnel whenever a phone (re)appears; `adb reverse` is per connection."""
    while True:
        try:
            out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=5).stdout
            for serial in [l.split()[0] for l in out.splitlines()[1:] if l.endswith("\tdevice")]:
                if serial.startswith("emulator-") and os.environ.get("RYOKU_LINK_EMULATOR") != "1":
                    continue
                subprocess.run([ADB, "-s", serial, "reverse", f"tcp:{PORT}", f"tcp:{PORT}"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"adb unavailable ({e})")
        await asyncio.sleep(5)


async def main() -> None:
    server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    log(f"listening on 127.0.0.1:{PORT}, adb at {ADB}")
    async with server:
        await asyncio.gather(server.serve_forever(), watch_hyprland(), keep_reverse())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
