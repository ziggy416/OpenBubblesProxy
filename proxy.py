#!/usr/bin/env python3
"""
OpenBubbles relay profile harmonizer — deployable edition.

A transparent shim between the OpenBubbles Android client and the
registration relay (Beeper's by default). Exists because Apple (Oct 2026)
enforces device checks at the Albert activation layer:

  - iPhone-class activations from software are refused outright
    ("Device Unknown"), even with real serials.
  - Mac-class activations pass with any correctly-formatted iMac serial
    ("C02...", exactly 12 chars) + a UUID — even with iOS version strings.

The client's activation is Mac-shaped (hard-coded in the app), while
Apple's iMessage layer sees the provider's real iPhone identity. This proxy
rewrites ONLY the two fields Apple checks at activation:

    serial_number     -> per-device synthetic iMac-format serial
    unique_device_id  -> per-device UUID

Hardware model, OS name, iOS version/build, and validation data pass through
untouched. sign-data returns 401 locally to trigger the client's fallback
without forwarding a command that JJTech's ValidationRelay does not handle.

DYNAMIC IDENTITIES (no hardcoded devices):
Each provider device is identified by its real UDID. The first time a new
device's identity request passes through, a fresh serial+UUID pair is
generated and persisted in a registry file, then reused forever for that
device (Apple activates each serial exactly once — a registration's
identity pair must remain stable and can never be reused by another
device). Swap in a new relay iPhone and it transparently gets its own
identity; nothing to reconfigure. Never delete the registry file.

The app makes get-version-info from two HTTP clients: Dart/dio (sends
User-Agent + Content-Type; used only to pick the setup branch) and
Rust/reqwest (sends neither; this response IS the registration identity).
Only the Rust one is rewritten.

Config: environment variables, with proxy_config.json as fallback:
  UPSTREAM      relay the provider is on (default https://registration-relay.beeper.com)
  BIND / PORT   listen address   (default 0.0.0.0:8080)
  REGISTRY_PATH identity registry (default ./data/registry.json)
  STAGE         hybrid (normal) | passthrough (debug)
  ALLOWED_CODES optional comma-separated list of relay codes this proxy
               serves. If set, any other code gets 403. Strongly recommended
               for public deployments: without it, anyone who finds your
               hostname can route their registration through you.

Python 3.8+ standard library only.
"""
import json
import os
import secrets
import sys
import threading
import urllib.request
import urllib.error
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "proxy.log")

FORWARD_HEADERS = ("authorization", "x-beeper-access-token", "content-type",
                   "accept-language")

# Valid 4-char serial model codes for iMac13,1 (OpenCore macserial data)
IMAC131_SUFFIXES = ("DNCT", "DNCR", "DNML", "DNMM", "F8QM", "F8QN", "F9RN",
                    "FC6M", "FC6N", "FC6P", "FD55", "FD5Y", "FF7L")
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"   # no I/O, like Apple serials

_log_lock = threading.Lock()
_registry_lock = threading.Lock()

def log(msg):
    with _log_lock:
        line = msg + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)

def cfg(key, default):
    if os.environ.get(key):
        return os.environ[key]
    try:
        with open(os.path.join(HERE, "proxy_config.json"), encoding="utf-8") as f:
            c = json.load(f)
        return c.get(key.lower(), default)
    except Exception:
        return default

def registry_path():
    return os.environ.get("REGISTRY_PATH",
                          os.path.join(HERE, "data", "registry.json"))

def load_registry():
    with _registry_lock:
        try:
            with open(registry_path(), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            log(f"[proxy] registry unreadable ({e}); treating as empty")
            return {}

def save_registry(reg):
    with _registry_lock:
        path = registry_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=1, sort_keys=True)
        os.replace(tmp, path)

def new_identity():
    # iMac13,1 serial: C02 + year-char (C..X = 2010..2019) + week-char
    # + 3 random + model code = EXACTLY 12 chars (13 -> Apple
    # DEVICE_INFO_INTEGRITY_FAILED, verified empirically).
    year = secrets.choice("CDFGHJKLMNPQRSTUVWX")
    week = secrets.choice("123456789CDFGHJKLMNPQRSTVW")
    uniq = "".join(secrets.choice(ALPHABET) for _ in range(3))
    return {
        "hybrid_serial": "C02" + year + week + uniq + secrets.choice(IMAC131_SUFFIXES),
        "hybrid_udid": str(uuidlib.uuid4()).upper(),
    }

def identity_for(real_udid, real_serial, hardware_version):
    """Stable per-device hybrid identity; generated on first sight."""
    reg = load_registry()
    entry = reg.get(real_udid)
    if entry is None:
        entry = new_identity()
        entry["real_serial"] = real_serial
        entry["hardware_version"] = hardware_version
        entry["created"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
        reg[real_udid] = entry
        save_registry(reg)
        log(f"[proxy] new device {real_udid[:4]}***: issued identity (serial <len {len(entry['hybrid_serial'])}>)")
    return entry

def describe(versions):
    # redacted: field presence + length only, never values
    return json.dumps({k: (("<len %d>" % len(str(v))) if v is not None else "<null>")
                       for k, v in versions.items()}, sort_keys=True)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _is_rust_client(self):
        ua = self.headers.get("User-Agent")
        ct = self.headers.get("Content-Type")
        return (ua is None) and (ct is None)

    def do_POST(self):
        upstream = cfg("UPSTREAM", "https://registration-relay.beeper.com").rstrip("/")
        stage = cfg("STAGE", "hybrid")

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if not self.path.startswith("/api/v1/bridge/"):
            log(f"[proxy] unexpected path {self.path} -> 404")
            self.send_error(404)
            return

        command = self.path.rsplit("/", 1)[-1]
        allowed = cfg("ALLOWED_CODES", "")
        if allowed:
            bearer = self.headers.get("Authorization", "")
            code = bearer[7:].strip() if bearer.startswith("Bearer ") else ""
            if code not in [c.strip() for c in allowed.split(",") if c.strip()]:
                log(f"[proxy] rejected code not in allowlist")
                self.send_error(403, "not permitted")
                return
        rust = self._is_rust_client()
        client = "rust" if rust else "dart"
        log(f"[proxy] {client} client -> {command} (stage {stage})")

        # JJTech's ValidationRelay does not handle sign-data. Forwarding it
        # can time out and disconnect the provider; use the client's fallback.
        if command == "sign-data":
            log("[proxy] sign-data: returning 401 for client fallback")
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        url = upstream + self.path
        req = urllib.request.Request(url, data=(body or None), method="POST")
        for h in FORWARD_HEADERS:
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        if not body:
            req.add_header("Content-Length", "0")

        try:
            with urllib.request.urlopen(req, timeout=70) as resp:
                status = resp.status
                rbody = resp.read()
                ctype = resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            status = e.code
            rbody = e.read()
            ctype = e.headers.get("Content-Type", "application/json")
        except Exception as e:
            log(f"[proxy] upstream error: {e}")
            self.send_error(502, "upstream error")
            return

        log(f"[proxy] {command}: upstream {status}, {len(rbody)} bytes")

        if command == "get-version-info" and status == 200 and rust and stage == "hybrid":
            try:
                data = json.loads(rbody)
                versions = data.get("versions", {})
                log(f"[proxy] upstream versions: {describe(versions)}")
                real_udid = str(versions.get("unique_device_id", ""))
                real_serial = str(versions.get("serial_number", ""))
                hw = str(versions.get("hardware_version", ""))
                entry = identity_for(real_udid, real_serial, hw)
                versions["serial_number"] = entry["hybrid_serial"]
                versions["unique_device_id"] = entry["hybrid_udid"]
                data["versions"] = versions
                log(f"[proxy] served versions: {describe(versions)}")
                rbody = json.dumps(data).encode()
            except Exception as e:
                log(f"[proxy] rewrite FAILED, serving passthrough: {e}")

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(rbody)))
        self.end_headers()
        self.wfile.write(rbody)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def log_message(self, *args):
        pass

def main():
    bind = cfg("BIND", "0.0.0.0")
    port = int(cfg("PORT", "8080"))
    srv = ThreadingHTTPServer((bind, port), Handler)
    log(f"[proxy] listening {bind}:{port} | upstream={cfg('UPSTREAM','https://registration-relay.beeper.com')} "
        f"| stage={cfg('STAGE','hybrid')} | registry={registry_path()}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("[proxy] stopped")

if __name__ == "__main__":
    main()
