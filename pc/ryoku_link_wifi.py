#!/usr/bin/env python3
"""ryoku-link-wifi: serves ryoku-link to the phone over the local network, no cable needed.

ryoku_link.py only listens on private unix sockets; this bridge puts them on the network behind
TLS and relays each connection to them byte for byte, so ryoku_link.py is unaware of Wi-Fi.

  discovery  The phone broadcasts "ryoku-link?" to UDP DISCOVERY_PORT; this answers with the
             host name. The answer holds nothing secret: finding the PC is not reaching it.
  pairing    Whenever the phone is on the USB cable, its credentials are written into the app's
             private storage through adb: a random token and the SHA-256 of this PC's certificate.
             The secrets never travel over the air, and plugging the cable in once is the pairing.
  link       TLS on PORT (control) and VIDEO_PORT (video), the same port numbers the phone uses
             over USB, so the `view` message's port is right on either path. The phone pins the
             certificate, then sends {"auth": token} as its first line; anything else is dropped.
             Pinning plus the token matters: a connected phone moves the pointer and types.
"""

import hmac
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import hashlib

PORT = int(os.environ.get("RYOKU_LINK_PORT", "7466"))
VIDEO_PORT = int(os.environ.get("RYOKU_LINK_VIDEO_PORT", str(PORT + 1)))
DISCOVERY_PORT = int(os.environ.get("RYOKU_LINK_DISCOVERY_PORT", "7470"))
SOCKET_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "ryoku-link")
STATE = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "ryoku-link")
APP = "com.rixmerz.ryokumobile"
ADB = os.environ.get("RYOKU_ADB") or (
    os.path.expanduser("~/Android/platform-tools/adb")
    if os.path.exists(os.path.expanduser("~/Android/platform-tools/adb"))
    else shutil.which("adb") or "adb"
)


def log(msg: str) -> None:
    print(f"ryoku-link-wifi: {msg}", file=sys.stderr, flush=True)


def credentials() -> tuple[str, str, str, str]:
    """(cert path, key path, token, cert sha256 hex), created on first run and kept."""
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    cert, key, tok = (os.path.join(STATE, n) for n in ("cert.pem", "key.pem", "token"))
    if not (os.path.exists(cert) and os.path.exists(key)):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-nodes", "-days", "3650", "-subj", "/CN=ryoku-link", "-keyout", key, "-out", cert],
                       check=True, capture_output=True)
        os.chmod(key, 0o600)
    if not os.path.exists(tok):
        with open(os.open(tok, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(secrets.token_hex(32))
    with open(tok) as f:
        token = f.read().strip()
    with open(cert) as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    return cert, key, token, hashlib.sha256(der).hexdigest()


def pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def read_line(s: socket.socket, limit: int = 512) -> bytes:
    buf = b""
    while not buf.endswith(b"\n") and len(buf) < limit:
        b = s.recv(1)
        if not b:
            break
        buf += b
    return buf


def serve(port: int, tls: ssl.SSLContext, token: str) -> None:
    lsock = socket.create_server(("0.0.0.0", port), reuse_port=False)
    target = os.path.join(SOCKET_DIR, f"{port}.sock")
    log(f"TLS on :{port} -> {target}")

    def handle(raw: socket.socket, addr) -> None:
        try:
            raw.settimeout(5)
            s = tls.wrap_socket(raw, server_side=True)
            try:
                got = json.loads(read_line(s)).get("auth", "")
            except (ValueError, AttributeError):
                got = ""
            if not isinstance(got, str) or not hmac.compare_digest(got, token):
                log(f"{addr[0]}: bad token, dropped")
                s.close()
                return
            s.settimeout(None)
            desk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            desk.connect(target)
        except (OSError, ssl.SSLError) as e:
            log(f"{addr[0]}: {e}")
            raw.close()
            return
        if port == PORT:
            log(f"{addr[0]} connected")
        threading.Thread(target=pipe, args=(desk, s), daemon=True).start()
        pipe(s, desk)
        desk.close()
        s.close()
        if port == PORT:
            log(f"{addr[0]} disconnected")

    while True:
        conn, addr = lsock.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


def discovery() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", DISCOVERY_PORT))
    reply = json.dumps({"ryoku": 1, "host": socket.gethostname(), "port": PORT}).encode()
    while True:
        data, addr = s.recvfrom(64)
        if data.strip() == b"ryoku-link?":
            log(f"discovery from {addr[0]}")
            s.sendto(reply, addr)


def local_addresses() -> list[str]:
    """This PC's IPv4 addresses on real networks, so the phone can dial them if discovery is lost."""
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True).stdout
    return [f.split()[3].split("/")[0] for f in out.splitlines()
            if not f.split()[1].startswith(("docker", "virbr", "veth", "br-"))]


def provision(token: str, fingerprint: str) -> None:
    """Hands the credentials to every phone that shows up on the cable, once per connection."""
    done: set[str] = set()
    while True:
        try:
            out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=5).stdout
            present = {l.split()[0] for l in out.splitlines()[1:] if l.endswith("\tdevice") and not l.startswith("emulator-")}
            for serial in present - done:
                payload = json.dumps({"token": token, "fingerprint": fingerprint, "hosts": local_addresses()})
                # run-as reaches the app's private files (debug builds); nothing else on the phone can read them.
                r = subprocess.run([ADB, "-s", serial, "shell", "run-as", APP, "sh", "-c", "'cat > files/link.json'"],
                                   input=payload, capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    log(f"paired {serial}")
                    done.add(serial)
            done &= present
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"adb unavailable ({e})")
        time.sleep(5)


def main() -> None:
    cert, key, token, fingerprint = credentials()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert, key)
    for target in (lambda: serve(PORT, tls, token), lambda: serve(VIDEO_PORT, tls, token), discovery,
                   lambda: provision(token, fingerprint)):
        threading.Thread(target=target, daemon=True).start()
    log(f"discovery on udp :{DISCOVERY_PORT}, certificate {fingerprint[:16]}…")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
