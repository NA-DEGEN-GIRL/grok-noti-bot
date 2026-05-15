#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m py_compile "$repo_root/src/grok_telegram_notify.py"

state_dir="$HOME/.local/state/grok-telegram-notify"
rm -f "$state_dir/state.json"

cat <<'JSON' | LLM_NOTI_DRY_RUN=1 python3 "$repo_root/src/grok_telegram_notify.py"
{
  "hookEventName": "Stop",
  "sessionId": "smoke-session",
  "cwd": "/tmp/grok-noti-bot-smoke"
}
JSON

python3 - <<'PY'
import json
import pathlib
import sys
p = pathlib.Path.home() / ".local" / "state" / "grok-telegram-notify" / "state.json"
if not p.exists():
    raise SystemExit("state.json was not written")
data = json.loads(p.read_text(encoding="utf-8"))
assert data.get("mode") == "stop-hook", data
assert data.get("last_send_ok") is True, data
assert data.get("dry_run") is True, data
assert data.get("session_id") == "smoke-session", data
print("smoke test ok")
PY
