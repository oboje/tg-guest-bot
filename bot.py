#!/usr/bin/env python3
"""Telegram -> local agent CLI relay. Single-owner, long polling, stdlib only."""
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 4096
EDIT_INTERVAL = 1.5
MAX_STEPS_SHOWN = 8
ALLOWED_UPDATES = json.dumps(["message", "edited_message", "guest_message"])


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class Config:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.owner_id = os.environ.get("TELEGRAM_OWNER_ID", "")
        self.agent_bin = os.environ.get("AGENT_BIN", "claude")
        self.agent_args = shlex.split(os.environ.get("AGENT_ARGS", "-p"))
        self.workdir = os.path.expanduser(os.environ.get("AGENT_WORKDIR", "."))
        self.timeout = int(os.environ.get("AGENT_TIMEOUT", "600"))
        self.stream = os.environ.get("AGENT_STREAM", "1") not in ("0", "false", "")
        self.stream_args = shlex.split(os.environ.get(
            "AGENT_STREAM_ARGS", "--output-format stream-json --verbose"))
        self.poll_timeout = int(os.environ.get("POLL_TIMEOUT", "30"))
        self.guest_timeout = int(os.environ.get("GUEST_TIMEOUT", "120"))
        allowed = os.environ.get("GUEST_ALLOWED_IDS", "")
        self.guest_ids = {i.strip() for i in allowed.split(",") if i.strip()}

    def validate(self):
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", self.token),
                                  ("TELEGRAM_OWNER_ID", self.owner_id)) if not v]
        if missing:
            sys.exit(f"Missing env: {', '.join(missing)}")
        if not self.owner_id.lstrip("-").isdigit():
            sys.exit("TELEGRAM_OWNER_ID must be numeric")
        if not self.guest_ids:
            self.guest_ids = {self.owner_id}
        if not os.path.isdir(self.workdir):
            sys.exit(f"AGENT_WORKDIR is not a directory: {self.workdir}")


def api(cfg, method, params, timeout=None):
    url = API.format(token=cfg.token, method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout or cfg.poll_timeout + 10) as resp:
        return json.loads(resp.read())


def send(cfg, chat_id, text):
    for i in range(0, len(text), TG_LIMIT):
        chunk = text[i:i + TG_LIMIT]
        try:
            api(cfg, "sendMessage", {"chat_id": chat_id, "text": chunk,
                                     "disable_web_page_preview": "true"}, timeout=30)
        except Exception as exc:
            print(f"send failed: {exc}", file=sys.stderr)
            return


def edit(cfg, chat_id, message_id, text):
    try:
        api(cfg, "editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                     "text": text[:TG_LIMIT]}, timeout=30)
    except Exception:
        pass


def send_status(cfg, chat_id, text):
    try:
        res = api(cfg, "sendMessage", {"chat_id": chat_id, "text": text}, timeout=30)
        return res.get("result", {}).get("message_id")
    except Exception:
        return None


def typing(cfg, chat_id):
    try:
        api(cfg, "sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=15)
    except Exception:
        pass


def _clip(value, limit=60):
    text = " ".join(str(value).split())
    return text[:limit] + "..." if len(text) > limit else text


def _tool_label(block):
    name = block.get("name", "tool")
    args = block.get("input") or {}
    for key in ("command", "file_path", "pattern", "path", "url", "prompt"):
        if args.get(key):
            return f"{name}: {_clip(args[key])}"
    return name


def _steps_from(event):
    """Human-readable step labels for one stream-json event."""
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return [f"started {_clip(event.get('model', 'agent'), 30)}"]
    if kind == "assistant":
        steps = []
        for block in event.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "thinking":
                steps.append("thinking")
            elif btype == "tool_use":
                steps.append(_tool_label(block))
            elif btype == "text" and block.get("text", "").strip():
                steps.append("writing")
        return steps
    if kind == "user":
        for block in event.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return ["got result"]
    return []


def _render(steps, done=False):
    shown = steps[-MAX_STEPS_SHOWN:]
    lines = [f"- {step}" for step in shown[:-1]]
    if shown:
        lines.append(("- " if done else "> ") + shown[-1])
    if len(steps) > MAX_STEPS_SHOWN:
        lines.insert(0, f"({len(steps) - MAX_STEPS_SHOWN} earlier steps)")
    return "\n".join(lines) or "> starting"


def run_agent_stream(cfg, chat_id, prompt):
    cmd = [cfg.agent_bin, *cfg.agent_args, *cfg.stream_args, prompt]
    try:
        proc = subprocess.Popen(cmd, cwd=cfg.workdir, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, bufsize=1)
    except FileNotFoundError:
        return f"agent binary not found: {cfg.agent_bin}"

    killer = threading.Timer(cfg.timeout, proc.kill)
    killer.start()
    status_id = send_status(cfg, chat_id, "> starting")
    steps, texts, answer = [], [], None
    last_edit, last_text = 0.0, "> starting"

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        texts.append(block["text"].strip())
            if event.get("type") == "result":
                answer = event.get("result") or answer
            for step in _steps_from(event):
                if not steps or steps[-1] != step:
                    steps.append(step)
            now = time.monotonic()
            if status_id and steps and now - last_edit >= EDIT_INTERVAL:
                text = _render(steps)
                if text != last_text:
                    edit(cfg, chat_id, status_id, text)
                    last_edit, last_text = now, text
    finally:
        proc.wait()
        killer.cancel()

    err = (proc.stderr.read() or "").strip()
    if status_id:
        edit(cfg, chat_id, status_id, _render(steps + ["done"], done=True))
    if proc.returncode != 0 and not answer:
        return f"agent exited {proc.returncode}\n\n{err or '(no output)'}"
    return answer or "\n\n".join(texts) or err or "(no output)"


def run_agent(cfg, prompt, timeout=None):
    timeout = timeout or cfg.timeout
    cmd = [cfg.agent_bin, *cfg.agent_args, prompt]
    try:
        proc = subprocess.run(cmd, cwd=cfg.workdir, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=timeout)
    except FileNotFoundError:
        return f"agent binary not found: {cfg.agent_bin}"
    except subprocess.TimeoutExpired:
        return f"agent timed out after {timeout}s"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"agent exited {proc.returncode}\n\n{err or out or '(no output)'}"
    return out or err or "(no output)"


def answer_guest(cfg, query_id, text):
    result = {"type": "article", "id": "1", "title": "answer",
              "input_message_content": {"message_text": text[:TG_LIMIT]}}
    try:
        api(cfg, "answerGuestQuery", {"guest_query_id": query_id,
                                      "result": json.dumps(result)}, timeout=30)
    except Exception as exc:
        print(f"answerGuestQuery failed: {exc}", file=sys.stderr)


def handle_guest(cfg, message):
    query_id = message.get("guest_query_id")
    if not query_id:
        return
    user_id = str(message.get("from", {}).get("id", ""))
    if user_id not in cfg.guest_ids:
        print(f"ignored guest {user_id}", file=sys.stderr)
        return
    text = (message.get("text") or "").strip()
    if text.startswith("@"):
        text = text.partition(" ")[2].strip()
    quoted = ((message.get("reply_to_message") or {}).get("text") or "").strip()
    if quoted:
        text = f"Quoted message:\n{quoted}\n\nRequest:\n{text}"
    if not text:
        return
    answer_guest(cfg, query_id, run_agent(cfg, text, cfg.guest_timeout))


def handle(cfg, message):
    chat_id = message.get("chat", {}).get("id")
    user_id = str(message.get("from", {}).get("id", ""))
    if user_id != cfg.owner_id:
        print(f"ignored user {user_id}", file=sys.stderr)
        return
    text = (message.get("text") or "").strip()
    if not text:
        send(cfg, chat_id, "Text messages only.")
        return
    if text in ("/start", "/help"):
        send(cfg, chat_id, f"Send any text; it is forwarded to the local agent "
                           f"({cfg.agent_bin}) and the reply comes back here.\n"
                           f"/ping - liveness check")
        return
    if text == "/ping":
        send(cfg, chat_id, "pong")
        return
    if cfg.stream:
        reply = run_agent_stream(cfg, chat_id, text)
    else:
        typing(cfg, chat_id)
        reply = run_agent(cfg, text)
    send(cfg, chat_id, reply)


def main():
    load_env()
    cfg = Config()
    cfg.validate()
    try:
        me = api(cfg, "getMe", {}, timeout=15).get("result", {})
        guest = "on" if me.get("supports_guest_queries") else "off (BotFather)"
    except Exception:
        guest = "unknown"
    print(f"tg-guest-bot up: owner={cfg.owner_id} agent={cfg.agent_bin} "
          f"workdir={cfg.workdir} guest_mode={guest}", file=sys.stderr)
    offset = None
    while True:
        params = {"timeout": cfg.poll_timeout, "allowed_updates": ALLOWED_UPDATES}
        if offset is not None:
            params["offset"] = offset
        try:
            result = api(cfg, "getUpdates", params)
        except Exception as exc:
            print(f"poll failed: {exc}", file=sys.stderr)
            time.sleep(3)
            continue
        for update in result.get("result", []):
            offset = update["update_id"] + 1
            if update.get("guest_message"):
                handle_guest(cfg, update["guest_message"])
                continue
            message = update.get("message") or update.get("edited_message")
            if message:
                handle(cfg, message)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
