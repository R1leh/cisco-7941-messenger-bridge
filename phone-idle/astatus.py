#!/usr/bin/env python3
"""Root-owned daemon: every 15s, writes the SIP phone's registration status
to a world-readable file so the unprivileged phone-idle.service can just
read it (instead of accessing the Asterisk control socket directly, which
needs root / the asterisk group -- and a service running with
DynamicUser=yes + NoNewPrivileges=yes can't get either)."""
import json
import os
import subprocess
import time

OUT = "/run/phone-idle/asterisk_status.json"

# Extension/endpoint to check -- must match your pjsip.conf endpoint name.
SIP_ENDPOINT = os.environ.get("SIP_ENDPOINT", "100")

os.makedirs("/run/phone-idle", exist_ok=True)
os.chmod("/run/phone-idle", 0o755)

while True:
    try:
        out = subprocess.run(
            ["asterisk", "-rx", "pjsip show endpoints"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        registered = (
            f"{SIP_ENDPOINT}/{SIP_ENDPOINT}" in out
            and "Contact:" in out
            and f"sip:{SIP_ENDPOINT}@" in out
        )
        data = {"registered": registered, "ts": time.time()}
    except Exception as e:
        data = {"registered": False, "error": str(e), "ts": time.time()}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)
    time.sleep(15)
