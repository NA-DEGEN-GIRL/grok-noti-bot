# grok-noti-bot

Telegram completion notifications for [Grok Build](https://x.ai/cli) using Grok's native `Stop` hook.

This repository contains only the reusable notifier code, install helper, hook template, and example config. It does **not** include Telegram tokens, chat IDs, local session data, or machine-specific hook files.

## What it does

When a Grok Build turn finishes, the global Grok `Stop` hook runs `telegram_notify.py`. The script sends a Telegram message with:

- a short completion summary
- the current working directory
- the Grok session id, when available
- a `Grok 작업 완료` label so it is distinguishable from Codex or Claude notifications

The notifier is fail-open: hook failures are logged locally but do not block Grok.

## Files

| Path | Purpose |
| --- | --- |
| `src/grok_telegram_notify.py` | Main hook script. |
| `install.sh` | Installs the script and writes a user-local Grok hook config. |
| `hooks/telegram-notify.json.template` | Template for the Grok `Stop` hook. |
| `examples/telegram_notify.env.example` | Safe placeholder config; copy locally and fill in secrets. |
| `scripts/smoke_test.sh` | Dry-run test that does not send Telegram messages. |

## Requirements

- Linux/macOS shell environment
- Python 3.10+
- Grok Build CLI installed and authenticated
- Telegram bot token and chat/account id

## Install

```bash
git clone https://github.com/NA-DEGEN-GIRL/grok-noti-bot.git
cd grok-noti-bot
./install.sh
```

The installer writes:

- `~/.grok/hooks/telegram_notify.py`
- `~/.grok/hooks/telegram-notify.json`

Global Grok hooks under `~/.grok/hooks/` are personal hooks. Project hooks still require project trust in Grok.

## Configure Telegram secrets locally

Create a local config file from the example:

```bash
cp examples/telegram_notify.env.example ~/.grok/telegram_notify.env
chmod 600 ~/.grok/telegram_notify.env
nano ~/.grok/telegram_notify.env
```

Fill in:

```dotenv
LLM_NOTI_ENABLED=true
TELEGRAM_LLM_NOTI_BOT_TOKEN=<YOUR_TELEGRAM_BOT_TOKEN>
LLM_NOTI_OWNER_ACCOUNT_ID=<YOUR_TELEGRAM_CHAT_ID>
```

Supported token variable names:

- `TELEGRAM_LLM_NOTI_BOT_TOKEN`
- `LLM_NOTI_BOT_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `BOT_TOKEN`

Supported destination variable names:

- `OWNER_ACCOUNT_ID`
- `TELEGRAM_OWNER_ACCOUNT_ID`
- `LLM_NOTI_OWNER_ACCOUNT_ID`
- `TELEGRAM_CHAT_ID`
- `CHAT_ID`

Never commit your real `telegram_notify.env` or `.env` file.

## Verify without sending a Telegram message

```bash
./scripts/smoke_test.sh
```

Or run Grok with dry-run enabled:

```bash
LLM_NOTI_DRY_RUN=1 grok -p "Reply exactly: hook dry run ok" --no-memory --disable-web-search --output-format json
cat ~/.local/state/grok-telegram-notify/state.json
```

Expected state includes:

- `mode: stop-hook`
- `last_send_ok: true`
- `dry_run: true`
- `session_id: ...`

## Verify with a real Telegram message

After configuring real secrets:

```bash
grok -p "Reply exactly: grok notify real test ok" --no-memory --disable-web-search --output-format json
cat ~/.local/state/grok-telegram-notify/state.json
```

`last_send_ok: true` means the Telegram API request succeeded.

## Disable temporarily

Set either value in your local env file:

```dotenv
LLM_NOTI_ENABLED=false
# or
LLM_NOTI_DISABLED=true
```

## Security notes

- The repository intentionally contains no real credentials.
- Runtime state is written under `~/.local/state/grok-telegram-notify/`.
- The script logs failure class names, not Telegram secrets.
- Keep `~/.grok/telegram_notify.env` mode `600` when possible.
