#!/usr/bin/env python3
"""Generic, token-authenticated status agent for ANY server you want the
phone's "Server status" screen to report on -- not tied to any particular
stack. It runs a small list of shell commands you configure and serves
their output as plain text lines over HTTP.

Configure STATUS_CHECKS as a list of "Label=shell command" pairs, separated
by ";". Each command's stdout is captured, trimmed, and shown as one line
("Label: output"). Examples:

  STATUS_CHECKS="Disk=df -h / | awk 'NR==2{print $4\" free\"}';Docker=docker ps -q | wc -l"

Point phone-idle's REMOTE_STATUS_URLS at this agent (see
phone-idle/phone-idle.service.example) to show its output on the phone --
you can point it at as many servers running this agent as you like, each
with its own label.

Bind this to a private interface and firewall it to the phone-idle host's
IP only -- this is a convenience endpoint, not a hardened API."""
import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("STATUS_TOKEN", "")
PORT = int(os.environ.get("STATUS_PORT", "8097"))
CHECKS = os.environ.get("STATUS_CHECKS", "")

if not TOKEN:
    raise SystemExit("STATUS_TOKEN is not set -- refusing to start with no auth token")


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception as e:
        return f"error: {e}"


def parse_checks(spec):
    checks = []
    for part in spec.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        label, cmd = part.split("=", 1)
        checks.append((label.strip(), cmd.strip()))
    return checks


def gather():
    uptime_s = float(open("/proc/uptime").read().split()[0])
    d, rem = divmod(int(uptime_s), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    lines = [f"uptime {d}d {h}h {m}m"]
    for label, cmd in parse_checks(CHECKS):
        out = sh(cmd)
        first_line = out.splitlines()[0] if out else "(no output)"
        lines.append(f"{label}: {first_line}")
    return {"lines": lines}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/status"):
            self.send_error(404)
            return
        query = self.path.partition("?")[2]
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if params.get("token") != TOKEN:
            self.send_error(403)
            return
        try:
            body = json.dumps(gather()).encode()
            code = 200
        except Exception as e:
            body = json.dumps({"lines": [f"error: {e}"]}).encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
