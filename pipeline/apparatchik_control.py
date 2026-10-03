#!/usr/bin/env python3
"""
Pause / resume the Apparatchik guard around a capture, using its authenticated
remote-control API (the same one the Android companion uses):

  POST /v1/pair  {code, displayName}        one-time pairing, returns a bearer credential
  GET  /v1/state                             snapshot: isRunning, pauseResumesAt, ...
  POST /v1/loop  {running:false, minutes:N}  timed pause (auto-resumes after N minutes)
  POST /v1/loop  {running:true}              resume now

The server listens with TLS on 127.0.0.1:57567 using a self-signed certificate; the
pipeline pins its SHA-256 fingerprint (captured at pairing and cross-checked with the
fingerprint the server reports). The credential is stored in the macOS login Keychain,
never in the repo.

One-time setup (pairing code from Apparatchik > Settings > Remote / Pairing):
  python3 pipeline/apparatchik_control.py pair 123456
Check:   python3 pipeline/apparatchik_control.py status
"""
import hashlib
import http.client
import json
import socket
import ssl
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

HOST = "127.0.0.1"
PORT = 57567
KEYCHAIN_SERVICE = "s117-zroute-pipeline.apparatchik"
KEYCHAIN_ACCOUNT = "remote-control"
DISPLAY_NAME = "S117 Capitol pipeline (Mac)"


class ApparatchikError(RuntimeError):
    pass


# ---------- credential storage (macOS Keychain, else a private file) ----------
# The login Keychain is not reachable from non-GUI sessions (tmux/ssh: `security` exits 36),
# so fall back to a 0600 file outside the repo. Secrets are never put in error messages.
import os
CRED_FILE = os.path.expanduser("~/.config/s117-zroute-pipeline/apparatchik.json")


def _load_credentials():
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                              "-a", KEYCHAIN_ACCOUNT, "-w"],
                             check=True, capture_output=True, text=True).stdout.strip()
        return json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_credentials(data):
    """Returns where the credential was stored."""
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                        "-a", KEYCHAIN_ACCOUNT, "-w", json.dumps(data)], capture_output=True)
    if r.returncode == 0:
        return "the login Keychain"
    os.makedirs(os.path.dirname(CRED_FILE), mode=0o700, exist_ok=True)
    fd = os.open(CRED_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(CRED_FILE, 0o600)
    return f"{CRED_FILE} (mode 600; Keychain not available in this session, security exit {r.returncode})"


# ---------- TLS transport with certificate pinning ----------

def _connect(expected_fingerprint=None, timeout=15):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # self-signed; authenticity comes from fingerprint pinning
    conn = http.client.HTTPSConnection(HOST, PORT, timeout=timeout, context=ctx)
    try:
        conn.connect()
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        raise ApparatchikError(f"Apparatchik remote control is not reachable on {HOST}:{PORT} ({e})")
    der = conn.sock.getpeercert(binary_form=True)
    fingerprint = hashlib.sha256(der).hexdigest()
    if expected_fingerprint and fingerprint != expected_fingerprint:
        conn.close()
        raise ApparatchikError("Apparatchik TLS certificate fingerprint changed; re-pair the pipeline")
    return conn, fingerprint


def _request(method, path, body=None, creds=None):
    creds = creds or _load_credentials()
    if not creds:
        raise ApparatchikError("Pipeline is not paired with Apparatchik. Run: "
                               "python3 pipeline/apparatchik_control.py pair <code>")
    conn, _ = _connect(creds["fingerprint"])
    headers = {"Authorization": f"Bearer {creds['credential']}", "Content-Type": "application/json"}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    if resp.status != 200:
        raise ApparatchikError(f"{method} {path} -> HTTP {resp.status}: {data[:200]!r}")
    return json.loads(data) if data else {}


def pair(code):
    conn, observed = _connect()
    conn.request("POST", "/v1/pair", body=json.dumps({"code": code, "displayName": DISPLAY_NAME}),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    if resp.status != 200:
        raise ApparatchikError(f"Pairing failed: {data.get('error', resp.status)}")
    if data.get("fingerprint") and data["fingerprint"].lower() != observed:
        raise ApparatchikError("Server-reported fingerprint does not match the TLS certificate; not saving")
    where = _save_credentials({"credential": data["credential"], "clientId": data.get("clientId"),
                               "fingerprint": observed})
    return data.get("clientId"), where


# ---------- state & control ----------

def _parse_date(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_installed_and_running():
    """True when the Apparatchik remote-control port accepts connections."""
    try:
        with socket.create_connection((HOST, PORT), timeout=2):
            return True
    except OSError:
        return False


def state():
    snap = _request("GET", "/v1/state")
    return {"isRunning": bool(snap.get("isRunning")),
            "pauseResumesAt": _parse_date(snap.get("pauseResumesAt"))}


def pause(minutes):
    _request("POST", "/v1/loop", {"running": False, "minutes": int(minutes)})
    return state()


def resume():
    _request("POST", "/v1/loop", {"running": True})
    return state()


@contextmanager
def monitoring_paused(minutes, log=print):
    """Pause Apparatchik monitoring for the capture and resume it as soon as the block exits.

    - Apparatchik not running on this Mac: nothing to do.
    - Already paused by the operator: left paused and NOT resumed afterwards (operator pauses are respected).
    - Monitoring: timed pause of `minutes` (safety net: Apparatchik resumes on its own if the
      pipeline dies), then resumed immediately when the block exits, success or failure,
      unless someone changed the pause in the meantime.
    """
    if not is_installed_and_running():
        log("Apparatchik is not running; no pause needed.")
        yield None
        return
    before = state()
    if not before["isRunning"]:
        log("Apparatchik is already paused by the operator; leaving it paused (will not resume).")
        yield before
        return
    after = pause(minutes)
    if after["isRunning"]:
        raise ApparatchikError("Apparatchik did not pause; aborting capture to avoid fighting over the emulator")
    our_resume_at = after["pauseResumesAt"]
    log(f"Apparatchik monitoring paused (safety auto-resume at "
        f"{our_resume_at.astimezone().strftime('%H:%M:%S') if our_resume_at else 'unknown'}).")
    try:
        yield after
    finally:
        try:
            now = state()
            if now["isRunning"]:
                log("Apparatchik already resumed.")
            elif our_resume_at and now["pauseResumesAt"] and \
                    abs((now["pauseResumesAt"] - our_resume_at).total_seconds()) > 5:
                log("Apparatchik pause was changed by someone else; leaving it as is.")
            elif our_resume_at and not now["pauseResumesAt"]:
                log("Apparatchik was switched to an indefinite pause by the operator; leaving it paused.")
            else:
                resume()
                log("Apparatchik monitoring resumed.")
        except ApparatchikError as e:
            log(f"WARNING: could not resume Apparatchik ({e}); its timed pause will resume it at "
                f"{our_resume_at.astimezone().strftime('%H:%M:%S') if our_resume_at else 'the timer'}.")


def main(argv):
    if len(argv) >= 2 and argv[0] == "pair":
        client, where = pair(argv[1])
        print(f"Paired with Apparatchik (client {client}); credential stored in {where}.")
    elif argv[:1] == ["status"]:
        if not is_installed_and_running():
            print("Apparatchik remote control not reachable.")
            return 1
        s = state()
        print(f"monitoring: {'running' if s['isRunning'] else 'paused'}"
              + (f", resumes at {s['pauseResumesAt'].astimezone():%H:%M:%S}" if s["pauseResumesAt"] else ""))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ApparatchikError as e:
        print(f"Error: {e}")
        sys.exit(1)
