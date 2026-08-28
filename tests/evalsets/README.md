# adk eval — the ADK-native evalset

This runs the REAL `root_agent` (`src/agent/agent.py`) with its real tools in
Google's own eval harness: **trajectory evaluation** (`tool_trajectory_avg_score`,
strict 1.0 — the exact tool calls and args must match) plus **final-response
evaluation** (`response_match_score`, ROUGE-based against a reference reply).

Every case pins a fresh `eval-*` workspace id. Workspaces are in-memory
(Firestore stays off unless `BLINK_FIRESTORE` is set), so every tool response is
deterministic. The one non-offline piece is the model itself: `adk eval` drives
the live Gemini model, so it needs Vertex credentials (the repo's `.env` /
service account, same as running the app). There is no mocked-model mode in the
ADK harness — the pytest suite (`tests/`, fully offline, LLM mocked at the
`llm.set_client` seam) is the zero-token counterpart.

## Run it

```bash
pip install "google-adk[eval]"   # one-time: the eval extras (rouge, pandas, ...)

PYTHONPATH=. .venv/bin/adk eval src/agent tests/evalsets/blink.evalset.json \
  --config_file_path tests/evalsets/test_config.json \
  --print_detailed_results
```

Run from the repo root (`PYTHONPATH=.` because the agent imports `src.*`; `adk`
loads the repo's `.env` for the Vertex credentials).

## The cases

| eval_id | Proves |
| --- | --- |
| `capacity_query` | A capacity question routes to `get_capacity`, never to invented numbers. |
| `schedule_proposal` | Scheduling goes through `propose_schedule_for_workspace`; the model reports what the scheduler placed (here: nothing — no ready tasks). |
| `validate_before_planning` | A plan-health question calls `validate_plan` and reports the typed findings. |
| `open_questions` | Clarification state is read via `list_open_questions`, not guessed. |
| `calendar_write_requires_confirm` | A calendar write triggers `propose_create_event` ONLY — the human-in-the-loop gate; `create_event_confirmed` must not appear in the trajectory. |
| `capacity_before_claiming_room` | "Do I have room?" checks `get_capacity` before claiming anything. |

## Thresholds (test_config.json)

- `tool_trajectory_avg_score: 1.0` — EXACT match (tool name + args, no extra or
  missing calls). Exactness is load-bearing for `calendar_write_requires_confirm`:
  an unauthorized `create_event_confirmed` call would fail the case.
- `response_match_score: 0.25` — the final reply is LLM-phrased in Blink's terse
  voice, so ROUGE against a fixed reference is a coarse similarity check, not a
  transcript match. The truth of the reply is enforced elsewhere: by the strict
  trajectory score here and by the grounded-reply invariant tests in pytest.
