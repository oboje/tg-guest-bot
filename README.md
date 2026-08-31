---
title: Telegram Agent Relay
description: Single-owner Telegram bot that forwards messages to a local claude -p (or codex) CLI and streams live progress back.
date: 2026-08-31
tags: [telegram, claude-code, codex, bot, python]
status: active
---

# Telegram Agent Relay

Plain Python 3, standard library only. Long-polls Telegram, ignores every user
except one id, shells out to a local agent CLI (`claude -p` by default), and
edits one status message in place while the run proceeds.

```
Telegram --getUpdates--> bot.py --[owner id?]--> claude -p --output-format stream-json
   ^                        |                              |
   |            editMessageText (live steps) <---- JSONL events
   +--------------- sendMessage (final answer, split at 4096)
```

## Setup

```sh
cp .env.example .env   # fill in token + your numeric user id
python3 bot.py
```

Find your numeric id by messaging the bot, then reading
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

## What streaming looks like

One message, edited about every 1.5s; the `>` marks the current step:

```
- started claude-opus-5
- thinking
- Bash: git status --short
- got result
> Read: bot.py
```

Past 8 steps the head collapses to `(6 earlier steps)`. The final answer arrives
as a separate message so it stays copyable.

## Config

| Var | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | - | BotFather token (required) |
| `TELEGRAM_OWNER_ID` | - | Only id served (required) |
| `AGENT_BIN` | `claude` | Agent CLI to run |
| `AGENT_ARGS` | `-p` | Flags before the prompt |
| `AGENT_STREAM` | `1` | Live status; `0` for one blocking reply |
| `AGENT_STREAM_ARGS` | `--output-format stream-json --verbose` | Added when streaming |
| `AGENT_WORKDIR` | `.` | Directory the agent runs in |
| `AGENT_TIMEOUT` | `600` | Kill a run after N seconds |
| `POLL_TIMEOUT` | `30` | Long-poll window |

Switching to codex: see the preset at the bottom of `.env.example`
(`AGENT_STREAM=0` — its event format is not parsed).

## Commands

`/ping` - liveness. `/help` - usage. Any other text goes to the agent.
