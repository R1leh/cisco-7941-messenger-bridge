#!/usr/bin/env python3
"""Small, token-authenticated HTTP status endpoint for a second host (e.g. a
VPN server), polled by the /status screen in phone-idle/idle.py. Bind it to
a private interface and firewall it to the phone-idle host's IP only -- this
is a convenience endpoint, not a hardened API.

Required: set STATUS_TOKEN to a long random value (e.g. `openssl rand -hex 32`)
and set the exact same value as UKNOW_TOKEN on the phone-idle side."""
import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("STATUS_TOKEN", "")
PORT = int(os.environ.get("STATUS_PORT", "8097"))
WG_INTERFACE = os.environ.get("WG_INTERFACE", "wg0")

if not TOKEN:
    raise SystemExit("STATUS_TOKEN is not set -- refusing to start with no auth token")


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def gather():
    uptime_s = float(open("/proc/uptime").read().split()[0])

    # xray: how many clients are configured, and how many have an open TCP session now
    stat = sh("xray api statsquery --server=127.0.0.1:10085 -pattern '' 2>/dev/null")
    users = set()
    for line in stat.splitlines():
        line = line.strip()
        if line.startswith('"name"') and "user>>>" in line:
            try:
                users.add(line.split("user>>>", 1)[1].split(">>>", 1)[0])
            except Exception:
                pass
    active_tcp = sh(
        "ss -tn state established "
        "'( dport = :443 or dport = :8443 or sport = :443 or sport = :8443 )' "
        "| tail -n +1 | wc -l"
    )

    # wireguard
    wg_total = sh(f"wg show {WG_INTERFACE} dump 2>/dev/null | tail -n +2 | wc -l")
    wg_online = 0
    now = int(time.time())
    for line in sh(f"wg show {WG_INTERFACE} dump 2>/dev/null | tail -n +2").splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            try:
                hs = int(parts[4])
                if hs and now - hs < 180:
                    wg_online += 1
            except Exception:
                pass

    hysteria_active = sh("systemctl is-active hysteria-server").strip() == "active"
    xray_active = sh("systemctl is-active xray").strip() == "active"

    return {
        "uptime_s": uptime_s,
        "xray_active": xray_active,
        "xray_clients": len(users),
        "xray_active_tcp": int(active_tcp or 0),
        "hysteria_active": hysteria_active,
        "wg_peers_total": int(wg_total or 0),
        "wg_peers_online": wg_online,
    }


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
            body = json.dumps({"error": str(e)}).encode()
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
