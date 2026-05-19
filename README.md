# grok-noti-bot

Grok Build `Stop` hook that records each completed turn as an **AI Worklog** entry in a Telegram forum topic.

This replaces the old direct-message completion ping. The hook now writes the same project/session/nonce worklog format used by the Codex workflow:

```text
AI Worklog <agent> <project>
user: <last user request summary>
answer: <assistant result summary>
changes: <hook/action summary>
nonce: <per-turn nonce>
session: <Grok session id>
```

Git state and changed files are stored in the local JSONL ledger by default, but are not shown in Telegram unless explicitly enabled.

## What it does

When a Grok Build turn finishes, the global Grok `Stop` hook runs `telegram_notify.py`. The script:

- derives user/assistant summaries from the Grok hook event or session files,
- detects the git project from the hook CWD,
- creates or reuses one Telegram forum topic per project,
- sends a compact AI Worklog message to that topic,
- appends the full record to a local JSONL ledger.

Runtime files are shared with the Codex AI Worklog setup:

```text
~/.local/state/codex-ai-worklog/worklog.jsonl
~/.local/state/codex-ai-worklog/topics.json
~/.local/state/codex-ai-worklog/grok_state.json
```

## Files

| Path | Purpose |
| --- | --- |
| `src/grok_telegram_notify.py` | Main Grok Stop-hook script. |
| `install.sh` | Installs the script and writes the user-local Grok hook config. |
| `hooks/telegram-notify.json.template` | Template for the Grok `Stop` hook. |
| `examples/telegram_notify.env.example` | Safe placeholder config; copy locally and fill in secrets. |
| `scripts/smoke_test.sh` | Dry-run test that does not send Telegram messages. |

## Requirements

- Linux/macOS shell environment
- Python 3.10+ using only the standard library
- Grok Build CLI installed and authenticated
- Telegram bot added to a forum-enabled supergroup
- Bot permission to create/manage topics if `AI_WORKLOG_AUTO_CREATE_TOPICS=true`

## Install

```bash
git clone https://github.com/NA-DEGEN-GIRL/grok-noti-bot.git
cd grok-noti-bot
./install.sh
```

The installer writes:

```text
~/.grok/hooks/telegram_notify.py
~/.grok/hooks/telegram-notify.json
```

Restart Grok Build, or reload hooks in the TUI, after changing hook files.

## Configure

Create a private env file:

```bash
cp examples/telegram_notify.env.example ~/.grok/telegram_notify.env
chmod 600 ~/.grok/telegram_notify.env
nano ~/.grok/telegram_notify.env
```

Minimal config:

```text
AI_WORKLOG_BOT_TOKEN=<YOUR_TELEGRAM_BOT_TOKEN>
AI_WORKLOG_CHAT_ID=<YOUR_FORUM_SUPERGROUP_CHAT_ID>
AI_WORKLOG_AUTO_CREATE_TOPICS=true
AI_WORKLOG_ENABLED=true
```

The script also falls back to the shared Codex config path `~/.codex/telegram_notify.env`, so you can keep one shared AI Worklog bot/group config for Codex, Claude, and Grok.

## Options

| Key | Default | Effect |
| --- | --- | --- |
| `AI_WORKLOG_BOT_TOKEN` | fallback token aliases | Telegram bot token. |
| `AI_WORKLOG_CHAT_ID` | — | Forum supergroup chat id. |
| `AI_WORKLOG_AUTO_CREATE_TOPICS` | `true` | Create missing project topics automatically. |
| `AI_WORKLOG_TOPIC_ID` | — | Force all messages into one known topic. |
| `AI_WORKLOG_ENABLED` | `true` | Set `false` to disable the hook without removing it. |
| `AI_WORKLOG_DRY_RUN` / `LLM_NOTI_DRY_RUN` | `false` | Write local state without Telegram send. |
| `AI_WORKLOG_SHOW_AGENT` | `true` | Show `Grok` in the Telegram header. |
| `AI_WORKLOG_SHOW_GIT` | `false` | Also show git branch/head in Telegram. |
| `AI_WORKLOG_SHOW_FILES` | `false` | Also show changed files in Telegram. |
| `AI_WORKLOG_TOPIC_MAP` | shared local path | Override project-to-topic cache path. |

Token fallback aliases are supported for compatibility: `TELEGRAM_LLM_NOTI_BOT_TOKEN`, `LLM_NOTI_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`, `BOT_TOKEN`.

## Verify without sending a Telegram message

```bash
./scripts/smoke_test.sh
```

Expected state:

```bash
cat ~/.local/state/codex-ai-worklog/grok_state.json
```

The dry-run state should include `mode: stop-hook`, `last_send_ok: true`, and `telegram_status: dry_run`.

## Verify with a real Telegram message

After configuring the bot/group:

```bash
python3 ~/.grok/hooks/telegram_notify.py --test --summary "Grok AI Worklog real test"
cat ~/.local/state/codex-ai-worklog/grok_state.json
```

`last_send_ok: true` means the Telegram API request succeeded.

## Security notes

- The repository intentionally contains no real credentials.
- Never commit real env files, bot tokens, chat ids, account ids, or Telegram state.
- Anyone who can read the Telegram forum topic can read the summaries.
- Runtime state is written under `~/.local/state/codex-ai-worklog/`.
- The script logs failure class names and sanitized messages only.
- Hook failures exit 0 in normal Stop-hook mode so Grok is not blocked.
