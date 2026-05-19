#!/usr/bin/env python3
"""
Grok AI Worklog Telegram topic hook.

This script is installed as the existing telegram_notify.py hook entrypoint, but
its default behavior is no longer a legacy direct-message completion ping. It
records a compact AI Worklog turn and sends it to the project Telegram forum
topic using the same user/answer/changes/nonce/session order as Codex.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any


AGENT_NAME = "Grok"
STATE_DIR = pathlib.Path.home() / ".local" / "state" / "codex-ai-worklog"
WORKLOG_FILE = STATE_DIR / "worklog.jsonl"
TOPIC_MAP_FILE = STATE_DIR / "topics.json"
STATE_FILE = STATE_DIR / "grok_state.json"
LOG_FILE = STATE_DIR / "grok_notify.log"
DOTENV_NAME = "." + "env"
SUMMARY_MAX_CHARS = 700
TRANSCRIPT_LINE_MAX_BYTES = 1 * 1024 * 1024

BOT_TOKEN_KEYS = [
    "AI_WORKLOG_BOT_TOKEN",
    "TELEGRAM_WORKLOG_BOT_TOKEN",
    "TELEGRAM_LLM_NOTI_BOT_TOKEN",
    "LLM_NOTI_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "BOT_TOKEN",
]

WORKLOG_CHAT_ID_KEYS = [
    "AI_WORKLOG_CHAT_ID",
    "TELEGRAM_WORKLOG_CHAT_ID",
    "WORKLOG_CHAT_ID",
    "TELEGRAM_GROUP_CHAT_ID",
]

WORKLOG_TOPIC_ID_KEYS = [
    "AI_WORKLOG_TOPIC_ID",
    "AI_WORKLOG_MESSAGE_THREAD_ID",
    "TELEGRAM_WORKLOG_TOPIC_ID",
    "TELEGRAM_WORKLOG_MESSAGE_THREAD_ID",
]

SECRET_PATTERNS = [
    (
        re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
        "<redacted:telegram-bot-token>",
    ),
    (
        re.compile(r"(?i)\b(token|secret|password|passwd|api[_-]?key)\s*=\s*([^\s,;]+)"),
        lambda match: f"{match.group(1)}=<redacted>",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{AGENT_NAME} AI Worklog Telegram topic hook")
    parser.add_argument("--test", action="store_true", help="send one manual AI Worklog test")
    parser.add_argument("--send-now", action="store_true", help="alias for --test")
    parser.add_argument("--summary", help="manual test answer summary")
    parser.add_argument("--message", help="backward-compatible alias for --summary")
    parser.add_argument("--dry-run", action="store_true", help="build local record without Telegram send")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    event = read_hook_event()
    config = load_config()

    if args.test or args.send_now:
        summary = (args.summary or args.message or f"{AGENT_NAME} AI Worklog test").strip()
        cwd = os.getcwd()
        record = build_record(
            cwd=cwd,
            session_id=manual_session_id(),
            user_summary=f"{AGENT_NAME} manual test",
            answer_summary=summary,
            changes=f"{AGENT_NAME} manual AI Worklog test",
        )
        ok = deliver_record(config, record, dry_run=args.dry_run)
        write_state("manual-test", record, ok)
        return 0 if ok else 1

    if should_ignore_event(event):
        return 0

    cwd = extract_cwd(event)
    session_id = extract_session_id(event)
    user_summary, answer_summary = derive_turn_summaries(event, session_id, cwd)
    record = build_record(
        cwd=cwd,
        session_id=session_id,
        user_summary=user_summary,
        answer_summary=answer_summary,
        changes=f"{AGENT_NAME} Stop hook 자동 AI Worklog 기록",
    )
    ok = deliver_record(config, record, dry_run=args.dry_run)
    write_state("stop-hook", record, ok)
    return 0


def should_ignore_event(event: dict[str, Any]) -> bool:
    hook_name = str(event.get("hookEventName") or event.get("hook_event_name") or os.environ.get("GROK_HOOK_EVENT") or "").lower()
    if hook_name and hook_name not in {"stop", "sessionend", "session_end"}:
        return True
    return False


def derive_turn_summaries(event: dict[str, Any], session_id: str, cwd: str) -> tuple[str, str]:
    for user_key in ("prompt", "userMessage", "user_message"):
        value = event.get(user_key)
        if isinstance(value, str) and value.strip():
            user_summary = value
            break
    else:
        user_summary = ""
    for answer_key in ("summary", "message", "lastAssistantMessage", "assistantText"):
        value = event.get(answer_key)
        if isinstance(value, str) and value.strip():
            answer_summary = value
            break
    else:
        answer_summary = ""

    session_dir = find_grok_session_dir(session_id)
    if session_dir:
        hist_user, hist_answer = last_grok_turn(session_dir / "chat_history.jsonl")
        user_summary = user_summary or hist_user
        answer_summary = answer_summary or hist_answer
        if not answer_summary:
            answer_summary = grok_session_title(session_dir / "summary.json")
    if not answer_summary and cwd:
        answer_summary = f"{pathlib.Path(cwd).name or cwd} 작업 완료"
    return shorten(user_summary or "Grok 사용자 요청 요약 없음"), shorten(answer_summary or "Grok 작업 완료")


def build_record(cwd: str, session_id: str, user_summary: str, answer_summary: str, changes: str) -> dict[str, Any]:
    project_ctx = project_context(cwd)
    git = git_info(project_ctx.get("git_root") or cwd)
    return {
        "timestamp": now_iso(),
        "agent": AGENT_NAME,
        "nonce": generate_nonce(),
        "project": project_ctx["project"],
        "topic_name": sanitize_topic_name(project_ctx["project"]),
        "topic_id": None,
        "session_id": session_id or f"missing-session-{AGENT_NAME.lower()}-{os.getppid()}",
        "cwd": cwd,
        "git_root": project_ctx.get("git_root"),
        "git": git,
        "user_summary": clean_text(shorten(user_summary or f"{AGENT_NAME} 사용자 요청 요약 없음", SUMMARY_MAX_CHARS)),
        "answer_summary": clean_text(shorten(answer_summary or "작업 완료", SUMMARY_MAX_CHARS)),
        "changes": clean_text(shorten(changes or f"{AGENT_NAME} Stop hook 자동 기록", 900)),
    }


def deliver_record(config: dict[str, str], record: dict[str, Any], dry_run: bool = False) -> bool:
    if not bool_config(config, "AI_WORKLOG_ENABLED", default=True):
        record["telegram_status"] = "disabled"
        record["telegram_send_ok"] = None
        append_worklog(record)
        log("AI Worklog disabled by AI_WORKLOG_ENABLED")
        return True

    if dry_run or bool_config(config, "AI_WORKLOG_DRY_RUN", default=False) or bool_config(config, "LLM_NOTI_DRY_RUN", default=False) or bool_config(config, f"{AGENT_NAME.upper()}_NOTI_DRY_RUN", default=False):
        record["dry_run"] = True
        record["telegram_status"] = "dry_run"
        record["telegram_send_ok"] = True
        append_worklog(record)
        return True

    token = first_value(config, BOT_TOKEN_KEYS)
    chat_id = first_value(config, WORKLOG_CHAT_ID_KEYS)
    if not token or not chat_id:
        record["telegram_status"] = "not_configured"
        record["telegram_send_ok"] = None
        append_worklog(record)
        log("AI Worklog Telegram is not configured; stored local record only")
        return False

    topic_id = first_value(config, WORKLOG_TOPIC_ID_KEYS)
    if topic_id:
        record["topic_id"] = topic_id
    else:
        ensured = ensure_topic(token, chat_id, record["topic_name"], record, config)
        record["topic_id"] = ensured.get("message_thread_id")
        record["topic_status"] = ensured.get("status")

    text = format_telegram_html(
        record,
        show_files=bool_config(config, "AI_WORKLOG_SHOW_FILES", default=False),
        show_git=bool_config(config, "AI_WORKLOG_SHOW_GIT", default=False),
        show_agent=bool_config(config, "AI_WORKLOG_SHOW_AGENT", default=True),
    )
    ok = send_telegram(token, chat_id, text, record.get("topic_id"))
    record["telegram_status"] = "sent" if ok else "send_failed"
    record["telegram_send_ok"] = ok
    record["telegram_sent_at"] = now_iso()
    append_worklog(record)
    return ok


def ensure_topic(token: str, chat_id: str, topic_name: str, record: dict[str, Any], config: dict[str, str]) -> dict[str, Any]:
    topic_map = read_topic_map(config)
    chat_key = str(chat_id)
    topics = topic_map.setdefault("chats", {}).setdefault(chat_key, {})
    existing = topics.get(topic_name)
    if existing and existing.get("message_thread_id"):
        return {"status": "mapped", "topic_name": topic_name, "message_thread_id": str(existing["message_thread_id"])}

    if not bool_config(config, "AI_WORKLOG_AUTO_CREATE_TOPICS", default=True):
        return {"status": "missing", "topic_name": topic_name, "message_thread_id": None}

    response = telegram_api(token, "createForumTopic", {"chat_id": chat_id, "name": topic_name})
    result = response.get("result") if response.get("ok") else None
    if not isinstance(result, dict) or not result.get("message_thread_id"):
        log(f"createForumTopic failed: {safe_json(response)}")
        if bool_config(config, "AI_WORKLOG_FALLBACK_TO_GENERAL", default=True):
            return {"status": "create_failed_general_fallback", "topic_name": topic_name, "message_thread_id": None}
        return {"status": "create_failed", "topic_name": topic_name, "message_thread_id": None}

    message_thread_id = str(result["message_thread_id"])
    topics[topic_name] = {
        "message_thread_id": message_thread_id,
        "project": record.get("project"),
        "git_root_hash": short_hash(record.get("git_root") or ""),
        "created_at": now_iso(),
    }
    write_topic_map(config, topic_map)
    return {"status": "created", "topic_name": topic_name, "message_thread_id": message_thread_id}


def format_telegram_html(record: dict[str, Any], show_files: bool = False, show_git: bool = False, show_agent: bool = True) -> str:
    git = record.get("git") or {}
    changed = git.get("changed_files") or []
    labels = [f"<code>{h(record['project'])}</code>"]
    if show_agent:
        labels.insert(0, f"<code>{h(record.get('agent') or AGENT_NAME)}</code>")
    changed_line = ""
    if show_files and changed:
        changed_line = "\n<b>files</b>: " + h(", ".join(changed[:6]))
        if len(changed) > 6:
            changed_line += h(f" 외 {len(changed) - 6}개")
    git_line = ""
    if show_git:
        dirty = "dirty" if git.get("dirty") else "clean"
        git_line = f"\n<b>git</b>: <code>{h(git.get('branch') or '?')}@{h(git.get('head') or '?')}</code> <code>{dirty}</code>"
    text = (
        f"<b>AI Worklog</b> {' '.join(labels)}\n"
        f"<b>user</b>: {h(limit(record.get('user_summary') or '', 700))}\n"
        f"<b>answer</b>: {h(limit(record.get('answer_summary') or '', 900))}\n"
        f"<b>changes</b>: {h(limit(record.get('changes') or '', 900))}\n"
        f"<b>nonce</b>: <code>{h(record['nonce'])}</code>\n"
        f"<b>session</b>: <code>{h(record['session_id'])}</code>"
        f"{git_line}{changed_line}"
    )
    return limit(text, 3900)


def send_telegram(token: str, chat_id: str, text: str, message_thread_id: str | int | None = None) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if message_thread_id:
        payload["message_thread_id"] = str(message_thread_id)
    response = telegram_api(token, "sendMessage", payload)
    if not response.get("ok"):
        log(f"telegram send failed: {safe_json(response)}")
        return False
    return True


def telegram_api(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"ok": False, "result": parsed}
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__}


def project_context(cwd: str) -> dict[str, Any]:
    git_root = run_text(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    root_path = pathlib.Path(git_root).resolve() if git_root else pathlib.Path(cwd).resolve()
    return {"project": root_path.name or pathlib.Path(cwd).name, "git_root": str(root_path)}


def git_info(cwd: str) -> dict[str, Any]:
    branch = run_text(["git", "branch", "--show-current"], cwd=cwd) or "detached"
    head = run_text(["git", "rev-parse", "--short", "HEAD"], cwd=cwd)
    status_raw = run_text(["git", "status", "--short"], cwd=cwd)
    status_lines = status_raw.splitlines() if status_raw else []
    return {"branch": branch, "head": head, "dirty": bool(status_lines), "changed_count": len(status_lines), "changed_files": status_lines[:20]}


def extract_text_for_role(entry: Any, role: str) -> str:
    if not isinstance(entry, dict):
        return ""
    entry_type = entry.get("type")
    if entry_type is not None and entry_type != role:
        return ""
    message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
    if isinstance(message, dict) and message.get("role") and message.get("role") != role:
        return ""
    content = message.get("content") if isinstance(message, dict) else None
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
    for key in ("summary", "message", "lastAssistantMessage", "assistantText", "prompt"):
        value = entry.get(key) if isinstance(entry, dict) else None
        if role == "assistant" and key in {"lastAssistantMessage", "assistantText", "summary", "message"} and isinstance(value, str):
            return value
        if role == "user" and key in {"prompt", "message"} and isinstance(value, str):
            return value
    return ""


def clean_summary(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("GROK_TMUX_STARTED:") or line.startswith("GROK_TMUX_DONE:"):
            continue
        lines.append(line)
    return " ".join(" ".join(lines).split()) or "작업 완료"


def shorten(text: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    text = clean_summary(text or "")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def find_grok_session_dir(session_id: str) -> pathlib.Path | None:
    if not session_id:
        return None
    sessions_root = pathlib.Path.home() / ".grok" / "sessions"
    if not sessions_root.exists():
        return None
    for candidate in sessions_root.glob(f"*/*{session_id}*"):
        if candidate.is_dir() and candidate.name == session_id:
            return candidate
    return None


def last_grok_turn(path: pathlib.Path) -> tuple[str, str]:
    if not path.exists() or not path.is_file():
        return "", ""
    last_user = ""
    last_assistant = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                if len(raw) > TRANSCRIPT_LINE_MAX_BYTES:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                user_text = extract_text_for_role(entry, "user")
                if user_text:
                    last_user = user_text
                assistant_text = extract_text_for_role(entry, "assistant")
                if assistant_text:
                    last_assistant = assistant_text
    except OSError as exc:
        log(f"could not read Grok chat history: {exc.__class__.__name__}")
    return last_user, last_assistant


def grok_session_title(path: pathlib.Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"could not read Grok summary: {exc.__class__.__name__}")
        return ""
    for key in ("generated_title", "session_summary"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_session_id(event: dict[str, Any]) -> str:
    for key in ("sessionId", "session_id", "sessionID"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return os.environ.get("GROK_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or ""

def extract_cwd(event: dict[str, Any]) -> str:
    for key in ("cwd", "workspaceRoot", "workspace_root"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    env = os.environ.get("GROK_WORKSPACE_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR")
    return env or os.getcwd()


def manual_session_id() -> str:
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("GROK_SESSION_ID") or f"manual-{AGENT_NAME.lower()}"


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
        log(f"could not parse hook JSON: {exc.__class__.__name__}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_config() -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in os.environ.items():
        values[key] = value
        values[key.upper()] = value
    file_path = values.get("AI_WORKLOG_ENV_FILE") or values.get("LLM_NOTI_FILE") or values.get("LLM_NOTI_ENV_FILE")
    candidates: list[pathlib.Path] = []
    if file_path:
        candidates.append(pathlib.Path(file_path).expanduser())
    candidates.extend(config_candidates())
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        values.update(parse_secret_file(candidate))
    return values


def config_candidates() -> list[pathlib.Path]:
    return [
        pathlib.Path.home() / ".grok" / ("telegram_notify." + "env"),
        pathlib.Path.home() / ".grok" / DOTENV_NAME,
        pathlib.Path.home() / ".claude" / ("telegram_notify." + "env"),
        pathlib.Path.home() / ".claude" / DOTENV_NAME,
        pathlib.Path.home() / ".codex" / ("telegram_notify." + "env"),
        pathlib.Path.home() / ".codex" / DOTENV_NAME,
        pathlib.Path.home() / DOTENV_NAME,
        pathlib.Path.cwd() / DOTENV_NAME,
    ]


def parse_secret_file(path: pathlib.Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log(f"could not read config file: {exc.__class__.__name__}")
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
            parts = shlex.split(value, comments=False, posix=True)
            value = parts[0] if parts else ""
        except Exception:
            value = value.strip("'\"")
        parsed[key] = value
        parsed[key.upper()] = value
    return parsed


def read_topic_map(config: dict[str, str]) -> dict[str, Any]:
    path = topic_map_path(config)
    if not path.exists():
        return {"version": 1, "chats": {}}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {"version": 1, "chats": {}}
    except Exception as exc:
        log(f"could not read topic map: {exc.__class__.__name__}")
        return {"version": 1, "chats": {}}


def write_topic_map(config: dict[str, str], data: dict[str, Any]) -> None:
    path = topic_map_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def topic_map_path(config: dict[str, str]) -> pathlib.Path:
    raw = config.get("AI_WORKLOG_TOPIC_MAP") or config.get("AI_WORKLOG_TOPIC_MAP_FILE")
    return pathlib.Path(raw).expanduser() if raw else TOPIC_MAP_FILE


def append_worklog(record: dict[str, Any]) -> None:
    WORKLOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with WORKLOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_state(mode: str, record: dict[str, Any], ok: bool) -> None:
    data = {
        "mode": mode,
        "agent": AGENT_NAME,
        "project": record.get("project"),
        "cwd": record.get("cwd"),
        "summary": record.get("answer_summary"),
        "session_id": record.get("session_id"),
        "nonce": record.get("nonce"),
        "last_send_ok": ok,
        "telegram_status": record.get("telegram_status"),
        "last_sent_at": time.time(),
    }
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log(f"could not write state: {exc.__class__.__name__}")


def first_value(config: dict[str, str], keys: list[str]) -> str | None:
    for key in keys:
        value = config.get(key)
        if value:
            return str(value)
    return None


def bool_config(config: dict[str, str], key: str, default: bool = False) -> bool:
    value = config.get(key)
    if value is None or value == "":
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def generate_nonce() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def sanitize_topic_name(value: str) -> str:
    cleaned = re.sub(r"[\r\n\t]+", " ", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or "unknown-project"


def clean_text(value: str) -> str:
    cleaned = value.replace("\r", " ").strip()
    for pattern, replacement in SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def h(value: Any) -> str:
    return html.escape(str(value), quote=False)


def limit(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


def safe_json(value: Any) -> str:
    return clean_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run_text(cmd: list[str], cwd: str | None = None) -> str | None:
    try:
        completed = subprocess.run(cmd, cwd=cwd, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def log(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{now_iso()} {clean_text(message)}\n")
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
