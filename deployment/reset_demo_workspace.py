#!/usr/bin/env python3
"""Reset Blink's Firestore workspaces to a clean demo slate.

    python3 deployment/reset_demo_workspace.py                  # ALL workspaces
    python3 deployment/reset_demo_workspace.py <workspace_id>   # just that one

What it does, in order:
  1. Backs up every targeted workspace's state to ~/blink-backups/<stamp>/.
  2. Signed-in workspaces (u_*) are reset but keep their plumbing: profile
     (timezone), google_tokens (calendar stays connected), devices (push still
     reaches the phone), onboarded. Everything else about them is cleared.
  3. Guest and probe workspaces (g_*, ws_*, smoke_*, anything not u_*) are
     deleted outright; a guest identity is minted by the browser and a fresh
     one appears on the next visit.
  4. Bounces the Cloud Run service ONCE at the end. Not optional: the server
     hydrates a workspace from Firestore once and then serves it from memory,
     so without a restart the live instance writes the old state back on the
     next turn.

Needs: gcloud authenticated with Firestore + Cloud Run access.
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


def list_workspaces():
    """Every workspace id, including implicit parents whose only content is
    the state subcollection (showMissing surfaces those)."""
    ids, page = [], ""
    while True:
        url = f"{BASE}?pageSize=300&showMissing=true&mask.fieldPaths=__name__"
        if page:
            url += f"&pageToken={page}"
        d = req(url)
        if "__error__" in d:
            sys.exit(f"could not list workspaces: {d['__error__']}")
        ids += [x["name"].split("/")[-1] for x in d.get("documents", [])]
        page = d.get("nextPageToken")
        if not page:
            return ids


def sections_of(ws):
    d = req(f"{BASE}/{ws}/state")
    if "__error__" in d:
        return None
    return [x["name"].split("/")[-1] for x in d.get("documents", [])]


def backup(ws, out_dir):
    snap = req(f"{BASE}/{ws}/state")
    if "__error__" in snap:
        sys.exit(f"backup of {ws} failed, aborting before any delete: {snap['__error__']}")
    with open(os.path.join(out_dir, f"{ws}.json"), "w") as f:
        json.dump(snap, f)


def reset_keep_plumbing(ws):
    doc = req(f"{BASE}/{ws}/state/meta")
    if "__error__" in doc:
        print(f"  {ws}: meta read failed, SKIPPED ({doc['__error__']})")
        return
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
        print(f"  {ws}: meta write failed, sections left alone ({out['__error__']})")
        return
    for s in SECTIONS:
        req(f"{BASE}/{ws}/state/{s}", "DELETE")
    print(f"  {ws}: reset, plumbing kept")


def delete_whole(ws, secs):
    for s in secs:
        req(f"{BASE}/{ws}/state/{s}", "DELETE")
    print(f"  {ws}: deleted ({len(secs)} sections)")


def bounce():
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    print("bouncing Cloud Run so no in-memory copy can write back...")
    subprocess.run(
        ["gcloud", "run", "services", "update", SERVICE,
         "--region", REGION, "--project", PROJECT,
         "--update-env-vars", f"BLINK_STATE_RESET_AT={stamp}"],
        check=True, capture_output=True, text=True)
    print("  new revision serving")


if __name__ == "__main__":
    targets = sys.argv[1:] or list_workspaces()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.expanduser(f"~/blink-backups/{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    print(f"Resetting {len(targets)} workspace(s); backups -> {out_dir}")

    for ws in targets:
        secs = sections_of(ws)
        if secs is None:
            print(f"  {ws}: unreadable, SKIPPED")
            continue
        if not secs:
            print(f"  {ws}: already empty")
            continue
        backup(ws, out_dir)
        if ws.startswith("u_"):
            reset_keep_plumbing(ws)
        else:
            delete_whole(ws, secs)

    bounce()

    leftovers = [w for w in list_workspaces() if sections_of(w)]
    print(f"verify: {len(leftovers)} workspace(s) still hold state "
          f"(u_* keeping plumbing is expected): {leftovers}")
