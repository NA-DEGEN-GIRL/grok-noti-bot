#!/usr/bin/env python3
"""
Grok Build completion notifier for Telegram.

Designed for the Grok `Stop` hook in ~/.grok/hooks/*.json. The Stop hook fires
when Grok finishes a turn, so this is the native Grok equivalent of the user's
Codex final-response notification rule and Claude Code Stop-hook notifier.

The script:
- accepts Grok hook JSON on stdin;
- derives a short summary from Grok session files when possible;
- reads Telegram credentials from env or local env files without printing them;
- supports LLM_NOTI_DRY_RUN=1 for safe verification;
- always exits 0 in hook mode so notifications never block Grok.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import sys
import time
import urllib.parse
import urllib.request
from typing import Any


STATE_DIR = pathlib.Path.home() / ".local" / "state" / "grok-telegram-notify"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "notify.log"
DOTENV_NAME = "." + "env"
SUMMARY_MAX_CHARS = 240

BOT_TOKEN_KEYS = [
    "TELEGRAM_LLM_NOTI_BOT_TOKEN",
    "LLM_NOTI_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "BOT_TOKEN",
]

OWNER_ID_KEYS = [
    "OWNER_ACCOUNT_ID",
    "TELEGRAM_OWNER_ACCOUNT_ID",
    "LLM_NOTI_OWNER_ACCOUNT_ID",
    "TELEGRAM_CHAT_ID",
    "CHAT_ID",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--send-now",
        action="store_true",
        help="send one Grok-labelled notification immediately",
    )
    parser.add_argument("--summary", help="summary for --send-now")
    parser.add_argument("--message", help="backward-compatible alias for --summary")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    event = read_hook_event()
    config = load_config()

    if is_disabled(config):
        write_state(event, "disabled", summary=args.summary or args.message)
        return 0

    if args.send_now:
        ok = send_now(config, args.summary or args.message)
        return 0 if ok else 1

    hook_name = str(
        event.get("hookEventName")
        or event.get("hook_event_name")
        or os.environ.get("GROK_HOOK_EVENT")
        or ""
    ).lower()
    if hook_name and hook_name not in {"stop", "sessionend", "session_end"}:
        write_state(event, "ignored", summary=f"non-stop hook: {hook_name}")
        return 0

    ok = send_hook_completion(config, event)
    return 0 if ok or not is_strict(config) else 1


def send_now(config: dict[str, str], summary: str | None) -> bool:
    event = {
        "hookEventName": "manual_send_now",
        "sessionId": os.environ.get("GROK_SESSION_ID") or "",
        "cwd": os.getcwd(),
    }
    return send_completion(config, event, (summary or "작업 완료").strip(), "manual-send-now")


def send_hook_completion(config: dict[str, str], event: dict[str, Any]) -> bool:
    session_id = extract_session_id(event)
    cwd = extract_cwd(event)
    summary = derive_summary(event, session_id, cwd)
    return send_completion(config, event, summary, "stop-hook")


def send_completion(
    config: dict[str, str], event: dict[str, Any], summary: str, mode: str
) -> bool:
    bot_token = first_value(config, BOT_TOKEN_KEYS)
    owner_id = first_value(config, OWNER_ID_KEYS)
    dry_run = is_truthy(config.get("LLM_NOTI_DRY_RUN") or config.get("GROK_NOTI_DRY_RUN"))
    if not dry_run and (not bot_token or not owner_id):
        log("telegram notifier is not configured; missing bot token or owner id")
        write_state(event, mode, summary=summary, ok=False, skipped="missing-config")
        return False

    session_id = extract_session_id(event)
    cwd = extract_cwd(event)

    session_line = (
        f"\\- 세션: `{escape_markdown_v2_code(session_id)}`\n" if session_id else ""
    )
    text = (
        "Grok 작업 완료\n"
        f"\\- 내용: {escape_markdown_v2(summary or '작업 완료')}\n"
        f"\\- 위치: {escape_markdown_v2(cwd)}\n"
        f"{session_line}"
        "\\- 상태: Stop hook 자동 알림"
    )

    if dry_run:
        ok = True
        log("dry-run: skipped Telegram send")
    else:
        ok = send_telegram(bot_token, owner_id, text)

    write_state(event, mode, summary=summary, ok=ok, dry_run=dry_run)
    return ok


def derive_summary(event: dict[str, Any], session_id: str, cwd: str) -> str:
    for key in ("summary", "message", "lastAssistantMessage", "assistantText"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return shorten(clean_summary(value))

    session_dir = find_session_dir(session_id)
    if session_dir:
        text = last_assistant_text(session_dir / "chat_history.jsonl")
        if text:
            return shorten(clean_summary(text))
        title = session_title(session_dir / "summary.json")
        if title:
            return shorten(clean_summary(title))

    if cwd:
        return f"{pathlib.Path(cwd).name or cwd} 작업 완료"
    return "작업 완료"


def clean_summary(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("GROK_TMUX_STARTED:") or line.startswith("GROK_TMUX_DONE:"):
            continue
        lines.append(line)
    cleaned = " ".join(" ".join(lines).split())
    return cleaned or "작업 완료"


def shorten(text: str) -> str:
    text = text.strip() or "작업 완료"
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    return text[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"


def last_assistant_text(path: pathlib.Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    last = ""
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = extract_assistant_text(entry)
                if text:
                    last = text
    except OSError as exc:
        log(f"could not read {path}: {exc}")
        return ""
    return last


def extract_assistant_text(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    if entry.get("type") != "assistant":
        return ""
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                value = block.get("text") or block.get("content")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    message = entry.get("message")
    if isinstance(message, dict):
        nested = dict(message)
        nested.setdefault("type", "assistant")
        return extract_assistant_text(nested)
    return ""


def session_title(path: pathlib.Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"could not read summary {path}: {exc}")
        return ""
    for key in ("generated_title", "session_summary"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def find_session_dir(session_id: str) -> pathlib.Path | None:
    if not session_id:
        return None
    sessions_root = pathlib.Path.home() / ".grok" / "sessions"
    if not sessions_root.exists():
        return None
    for candidate in sessions_root.glob(f"*/*{session_id}*"):
        if candidate.is_dir() and candidate.name == session_id:
            return candidate
    return None


def extract_session_id(event: dict[str, Any]) -> str:
    for key in ("sessionId", "session_id", "sessionID"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    env = os.environ.get("GROK_SESSION_ID")
    return env or ""


def extract_cwd(event: dict[str, Any]) -> str:
    for key in ("cwd", "workspaceRoot", "workspace_root"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    env = os.environ.get("GROK_WORKSPACE_ROOT")
    return env or os.getcwd()


def read_hook_event() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"could not parse hook JSON: {exc}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_config() -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in os.environ.items():
        values[key] = value
        values[key.upper()] = value

    file_path = values.get("LLM_NOTI_FILE") or values.get("LLM_NOTI_ENV_FILE")
    candidates: list[pathlib.Path] = []
    if file_path:
        candidates.append(pathlib.Path(file_path).expanduser())
    candidates.extend(
        [
            pathlib.Path.home() / ".grok" / ("telegram_notify." + "env"),
            pathlib.Path.home() / ".grok" / DOTENV_NAME,
            pathlib.Path.home() / ".claude" / ("telegram_notify." + "env"),
            pathlib.Path.home() / ".claude" / DOTENV_NAME,
            pathlib.Path.home() / ".codex" / ("telegram_notify." + "env"),
            pathlib.Path.home() / ".codex" / DOTENV_NAME,
            pathlib.Path.home() / DOTENV_NAME,
            pathlib.Path.cwd() / DOTENV_NAME,
        ]
    )

    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        values.update(parse_secret_file(candidate))
        if first_value(values, BOT_TOKEN_KEYS) and first_value(values, OWNER_ID_KEYS):
            break
    return values


def parse_secret_file(path: pathlib.Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log(f"could not read secret file {path}: {exc}")
        return parsed
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key:
            continue
        value = value.strip()
        try:
            split = shlex.split(value, comments=False, posix=True)
            value = split[0] if split else ""
        except Exception:
            value = value.strip("'\"")
        parsed[key] = value
        parsed[key.upper()] = value
    return parsed


def first_value(config: dict[str, str], keys: list[str]) -> str | None:
    for key in keys:
        value = config.get(key)
        if value:
            return str(value)
    return None


def is_disabled(config: dict[str, str]) -> bool:
    return is_truthy(config.get("LLM_NOTI_DISABLED")) or str(
        config.get("LLM_NOTI_ENABLED", "true")
    ).lower() in {"0", "false", "no", "off"}


def is_strict(config: dict[str, str]) -> bool:
    return is_truthy(config.get("LLM_NOTI_STRICT") or config.get("GROK_NOTI_STRICT"))


def is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def escape_markdown_v2(value: str) -> str:
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{char}" if char in special else char for char in value)


def escape_markdown_v2_code(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`")


def send_telegram(bot_token: str, owner_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": owner_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            ok = 200 <= response.status < 300
            if not ok:
                log(f"telegram send failed with status {response.status}")
            return ok
    except Exception as exc:
        log(f"telegram send failed: {exc.__class__.__name__}")
        return False


def write_state(
    event: dict[str, Any],
    mode: str,
    summary: str | None = None,
    ok: bool | None = None,
    dry_run: bool = False,
    skipped: str | None = None,
) -> None:
    data: dict[str, Any] = {
        "mode": mode,
        "cwd": extract_cwd(event),
        "summary": summary or "",
        "session_id": extract_session_id(event),
        "hook_event": event.get("hookEventName") or event.get("hook_event_name") or "",
        "last_sent_at": time.time(),
    }
    if ok is not None:
        data["last_send_ok"] = ok
    if dry_run:
        data["dry_run"] = True
    if skipped:
        data["skipped"] = skipped
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log(f"could not write state: {exc}")


def log(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
