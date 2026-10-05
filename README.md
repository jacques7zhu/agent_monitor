# agentmon

A terminal dashboard for live **Claude Code** and **Codex** sessions. For each session it shows the status (processing / waiting for approval / idle / exited), how long it has been in that status, and token usage: input, cached, output, and context %. For Codex it also shows the rate-limit windows.

It's a single Python file that uses only the standard library (Python 3.8+).

```sh
python3 agentmon.py              # interactive TUI
python3 agentmon.py once         # print the table once
python3 agentmon.py install-hooks    # recommended: exact "waiting for approval" detection
python3 agentmon.py uninstall-hooks
```

Keys: `↑/↓` or `j/k` select · `Enter` details (last prompt and reply) · `s` cycle sort · `a` show or hide exited sessions · `b` bell on approval requests · `q` quit.

## Where the data comes from

| | Live sessions | Status | Usage |
|---|---|---|---|
| Claude Code | `~/.claude/sessions/<pid>.json` (pid must be alive) | `status` busy/idle in the same file | `message.usage` in `~/.claude/projects/*/<session>.jsonl`, deduplicated per request |
| Codex | rollout files held open by a `codex` process or written in the last 10 minutes | `task_started` / `task_complete` | latest `token_count` event, including `rate_limits` |

Session files alone can't show whether an agent is waiting for approval. Without hooks, agentmon shows `waiting?` when a tool call has no result and the transcript hasn't changed for 5 seconds. A long-running command looks the same, so this is only a guess.

`install-hooks` adds `agentmon.py hook --agent …` entries to `~/.claude/settings.json` and `~/.codex/hooks.json`. Existing hooks are kept, the original files are backed up to `*.agentmon.bak`, and running it again is safe. Each hook appends one line to `~/.local/state/agentmon/events.jsonl`. `PermissionRequest`, or a `Notification` of type `permission_prompt`, turns the row red as **waiting**. Already-running sessions pick up the hooks only after a restart.

## Tests

```sh
python3 -m unittest test_agentmon.py
```
