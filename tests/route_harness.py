"""Drive StoneSage's real HTTP handler in-process (no sockets): call(method, path, body) -> (status, content_type, body).

Used by the route golden tests: the layer split moves route code between files, and a route must answer exactly as before.
Runs under tests/run_tests.py's network guard, so anything a route tries to reach (Home Assistant, Frigate, VM 102) fails fast.
"""

import http.client
import io
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from stonesage import server  # noqa: E402


class _Handler(server.StoneSageHandler):
    def __init__(self, method, path, body, headers):
        raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
        msg = http.client.HTTPMessage()
        msg["Host"] = "localhost:8888"
        msg["Content-Length"] = str(len(raw))
        if body is not None and not isinstance(body, bytes):
            msg["Content-Type"] = "application/json"
        for k, v in (headers or {}).items():
            msg[k] = v
        self.command, self.path, self.headers = method, path, msg
        self.request_version, self.requestline = "HTTP/1.1", f"{method} {path} HTTP/1.1"
        self.client_address = ("127.0.0.1", 50000)
        self.server = types.SimpleNamespace(server_name="localhost", server_port=8888)
        self.rfile, self.wfile = io.BytesIO(raw), io.BytesIO()
        self.close_connection = True

    def log_message(self, *args):          # quiet
        pass


def call(method, path, body=None, headers=None):
    h = _Handler(method, path, body, headers)
    getattr(h, "do_" + method)()
    out = h.wfile.getvalue()
    head, _, payload = out.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and lines[0].startswith("HTTP/") else 0
    ctype = next((l.split(":", 1)[1].strip() for l in lines[1:] if l.lower().startswith("content-type:")), "")
    return status, ctype, payload


def shape(method, path, body=None):
    """What a route's answer looks like, stable across runs: status, content type, and top-level JSON keys if any."""
    status, ctype, payload = call(method, path, body)
    out = {"status": status, "type": ctype.split(";")[0]}
    if "json" in ctype:
        try:
            data = json.loads(payload.decode("utf-8") or "null")
            out["keys"] = sorted(data) if isinstance(data, dict) else type(data).__name__
        except ValueError:
            out["keys"] = "not json"
    return out
