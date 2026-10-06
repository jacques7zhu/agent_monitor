# agentmon

A terminal dashboard for live **Claude Code** and **Codex** sessions. For each session it shows the status (processing / waiting for approval / idle / exited), how long it has been in that status, and token usage: input, cached, output, and context %. For Codex it also shows the rate-limit windows.

It's a single Python file that uses only the standard library (Python 3.8+).

![agentmon in the terminal](docs/terminal.png)

```sh
python3 agentmon.py              # interactive TUI
python3 agentmon.py once         # print the table once
python3 agentmon.py install-hooks    # recommended: exact "waiting for approval" detection
python3 agentmon.py uninstall-hooks
```

Keys: `↑/↓` or `j/k` select · `Enter` details (last prompt and reply) · `s` cycle sort · `a` show or hide exited sessions · `b` bell on approval requests · `q` quit.

## Ubuntu top-bar indicator

`agentmon_tray.py` puts a dot in the top-right panel with a short label such as `1 waiting 2 busy`. The dot is red when an agent is waiting for approval, amber when one is working, green when all are idle, and grey when no sessions are running. It blinks while a session waits for approval, and for 20 seconds after a session finishes a turn. A desktop notification has **Show details** and **Go to pane** buttons. Click the indicator for a menu with one entry per session; clicking an entry opens a details window with the table, the last prompt and reply, and a *Go to tmux pane* button.

```sh
sudo apt install gir1.2-appindicator3-0.1    # one-time; Ubuntu ships the GNOME AppIndicator extension
python3 agentmon_tray.py install-desktop     # adds "agentmon" to the app menu and starts it on login
python3 agentmon_tray.py                     # run it now (a second launch opens the details window)
python3 agentmon_tray.py uninstall-desktop
```

<p>
  <img src="docs/topbar.png" alt="agentmon in the Ubuntu top bar" height="56"><br>
  <img src="docs/menu.png" alt="agentmon indicator menu" width="480">
</p>

![agentmon details window](docs/details.png)

Use the TUI's `install-hooks` as well, so "waiting for approval" is detected exactly rather than guessed.

## Where the data comes from

| | Live sessions | Status | Usage |
|---|---|---|---|
| Claude Code | `~/.claude/sessions/<pid>.json` (pid must be alive) | `status` busy/idle in the same file | `message.usage` in `~/.claude/projects/*/<session>.jsonl`, deduplicated per request |
| Codex | threads a running `codex` process holds in `~/.codex/thread-writer-locks/` (guardian sub-agents hidden), mapped to `~/.codex/sessions/**/rollout-*.jsonl` | `task_started` / `task_complete` | latest `token_count` event, including `rate_limits` |

Session files alone can't show whether an agent is waiting for approval. Without hooks, agentmon shows `waiting?` when a tool call has no result and the transcript hasn't changed for 5 seconds. A long-running command looks the same, so this is only a guess.

`install-hooks` adds `agentmon.py hook --agent …` entries to `~/.claude/settings.json` and `~/.codex/hooks.json`. Existing hooks are kept, the original files are backed up to `*.agentmon.bak`, and running it again is safe. Each hook appends one line to `~/.local/state/agentmon/events.jsonl`. `PermissionRequest`, or a `Notification` of type `permission_prompt`, turns the row red as **waiting**. Already-running sessions pick up the hooks only after a restart.

*The screenshots use made-up demo sessions.*

## Tests

```sh
python3 -m unittest test_agentmon.py
```
