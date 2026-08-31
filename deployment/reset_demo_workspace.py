#!/usr/bin/env python3
"""Reset a Blink workspace to a clean demo slate, keeping the plumbing.

    python3 deployment/reset_demo_workspace.py                # the demo account
    python3 deployment/reset_demo_workspace.py <workspace_id> # any workspace

What it does, in order:
  1. Backs up the workspace's full Firestore state to ~/blink-backups/.
  2. Clears the plan (tasks, commitments, blocks, constraints, zones) and the
     history (conversation, memory, milestones, insight decisions, the
     notification ledger).
  3. KEEPS the plumbing: profile (timezone), google_tokens (calendar stays
     connected), devices (push still reaches the phone), onboarded.
  4. Bounces the Cloud Run service. This step is not optional: the server
     hydrates a workspace from Firestore ONCE and then serves it from memory,
     so without a restart the live instance would simply write the old state
     back on the next turn.

Needs: gcloud authenticated as an account with Firestore + Cloud Run access.
Does NOT touch Google Calendar; events Blink created stay on the calendar.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROJECT = "focus-agent-506601"
REGION = "us-central1"
SERVICE = "focus-agent"
DB = "blink"
DEFAULT_WS = "u_3fbfc72440377177cb1f7387"  # the demo account's workspace
BASE = (f"https://firestore.googleapis.com/v1/projects/{PROJECT}"
        f"/databases/{DB}/documents/blink_workspaces")

KEEP = ("profile", "google_tokens", "devices", "onboarded")
SECTIONS = ("constraints", "blocks", "tasks", "commitments", "zones")

TOKEN = subprocess.check_output(
    ["gcloud", "auth", "print-access-token"], text=True).strip()


def req(url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, method=method, data=data)
    r.add_header("Authorization", "Bearer " + TOKEN)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r) as resp:
            body = resp.read().decode()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        return {"__error__": f"{e.code} {e.reason}: {e.read().decode()[:200]}"}


def backup(ws):
    out_dir = os.path.expanduser("~/blink-backups")
    os.makedirs(out_dir, exist_ok=True)
    snap = req(f"{BASE}/{ws}/state")
    if "__error__" in snap:
        sys.exit(f"backup failed, aborting before any delete: {snap['__error__']}")
    path = os.path.join(out_dir, f"{ws}_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w") as f:
        json.dump(snap, f)
    print(f"1. backed up -> {path} ({os.path.getsize(path)} bytes)")


def reset(ws):
    doc = req(f"{BASE}/{ws}/state/meta")
    if "__error__" in doc:
        sys.exit(f"meta read failed: {doc['__error__']}")
    meta = json.loads(doc["fields"]["json"]["stringValue"])
    for key in list(meta):
        if key in KEEP:
            continue
        v = meta[key]
        meta[key] = [] if isinstance(v, list) else ({} if isinstance(v, dict) else None)
    fields = dict(doc["fields"])
    fields["json"] = {"stringValue": json.dumps(meta)}
    out = req(f"{BASE}/{ws}/state/meta", "PATCH", {"fields": fields})
    if "__error__" in out:
        sys.exit(f"meta write failed: {out['__error__']}")
    for s in SECTIONS:
        req(f"{BASE}/{ws}/state/{s}", "DELETE")
    print(f"2. cleared plan + history; kept {list(KEEP)}")


def bounce():
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    print("3. bouncing Cloud Run so the in-memory copy cannot write back...")
    subprocess.run(
        ["gcloud", "run", "services", "update", SERVICE,
         "--region", REGION, "--project", PROJECT,
         "--update-env-vars", f"BLINK_STATE_RESET_AT={stamp}"],
        check=True, capture_output=True, text=True)
    print("   new revision serving")


def verify(ws):
    snap = req(f"{BASE}/{ws}/state")
    names = [d["name"].split("/")[-1] for d in snap.get("documents", [])]
    ok = names == ["meta"]
    print(f"4. verify: sections now {names} -> {'CLEAN' if ok else 'NOT CLEAN'}")
    for d in snap.get("documents", []):
        o = json.loads(d["fields"]["json"]["stringValue"])
        p = o.get("profile") or {}
        print(f"   tz={p.get('timezone')} tokens={bool(o.get('google_tokens'))} "
              f"devices={len(o.get('devices') or {})} "
              f"conversation={len(o.get('conversation') or [])}")


if __name__ == "__main__":
    ws = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WS
    print(f"Resetting {ws} on {PROJECT}/{DB}")
    backup(ws)
    reset(ws)
    bounce()
    verify(ws)
