#!/usr/bin/env bash
set -euo pipefail

GROK_HOME="${GROK_HOME:-$HOME/.grok}"
HOOK_DIR="$GROK_HOME/hooks"
SCRIPT_DEST="$HOOK_DIR/telegram_notify.py"
HOOK_DEST="$HOOK_DIR/telegram-notify.json"

mkdir -p "$HOOK_DIR"
install -m 700 "$(dirname "$0")/src/grok_telegram_notify.py" "$SCRIPT_DEST"

python3 - "$SCRIPT_DEST" "$HOOK_DEST" <<'PY'
import json
import pathlib
import sys

script_dest = pathlib.Path(sys.argv[1]).expanduser().resolve()
hook_dest = pathlib.Path(sys.argv[2]).expanduser().resolve()
config = {
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"python3 {script_dest}",
                        "timeout": 15,
                    }
                ]
            }
        ]
    }
}
hook_dest.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
hook_dest.chmod(0o600)
PY

cat <<EOF
Installed Grok Telegram notifier:
- script: $SCRIPT_DEST
- hook:   $HOOK_DEST

Next steps:
1. Copy examples/telegram_notify.env.example to ~/.grok/telegram_notify.env.
2. Fill in your Telegram bot token and chat/account id locally.
3. Restart Grok Build or reload hooks in the TUI.
4. Verify with: LLM_NOTI_DRY_RUN=1 grok -p "Reply exactly: hook dry run ok" --no-memory --disable-web-search --output-format json
EOF
