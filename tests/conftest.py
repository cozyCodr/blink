# tests/conftest.py
"""Suite-wide guards.

The suite is fully offline and token-free, and it stays that way. Firestore
persistence (P2-01) is opt-in via BLINK_FIRESTORE, and this belt-and-braces
switch pins it off for every test process, so no test can reach the network
even on a machine with live credentials sitting in the environment.
"""
import os

os.environ["BLINK_DISABLE_FIRESTORE"] = "1"
os.environ.pop("BLINK_FIRESTORE", None)
