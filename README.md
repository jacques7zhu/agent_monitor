# agentmon

A terminal dashboard for live **Claude Code** and **Codex** sessions. It can
combine agents running locally, in one or more **WSL** distributions, and on
**SSH** servers in the same view. For each session it shows its source target,
status (processing / waiting for approval / idle / exited), time in that state,
and token usage. For Codex it also shows the rate-limit windows.

The core monitor and native Windows tray companion use only the standard
library (Python 3.8+). On Windows, the interactive TUI optionally uses
`windows-curses`; the `watch` dashboard and tray icon need no extra package.

![agentmon in the terminal](docs/terminal.png)

```sh
python3 agentmon.py              # interactive TUI
python3 agentmon.py once         # print the table once
python3 agentmon.py watch        # portable live view; Ctrl-C to quit
python3 agentmon.py install-hooks    # recommended: exact "waiting for approval" detection
python3 agentmon.py uninstall-hooks
```

Keys: `↑/↓` or `j/k` select · `Enter` details (last prompt and reply) · `s` cycle sort · `a` show or hide exited sessions · `b` bell on approval requests · `q` quit.

## Windows, WSL, and SSH targets

Pass `--target` more than once to aggregate sources. It can go before the
subcommand or after `once`, `watch`, and `tui`:

```powershell
# Windows Terminal / PowerShell: native Windows + default WSL distribution
py agentmon.py watch --target local --target wsl

# A named WSL distribution and two servers from ~/.ssh/config
py agentmon.py once --target wsl:Ubuntu-24.04 --target ssh:build --target ssh:user@example.com

# Optional: enable the full keyboard-controlled TUI on Windows
py -m pip install windows-curses
py agentmon.py --target wsl:Ubuntu-24.04 --target ssh:build
```

`wsl:DISTRO` uses `wsl.exe -d DISTRO`. `ssh:HOST` uses the system OpenSSH
client in non-interactive (`BatchMode`) mode, so set up key authentication and
put ports, identity files, jump hosts, and other options in `~/.ssh/config`.
Python 3 must be available as `python3` inside WSL and on SSH hosts.

For a persistent setup, create `%APPDATA%\agentmon\targets.json` on Windows or
`~/.config/agentmon/targets.json` on Linux:

```json
{
  "targets": ["local", "wsl:Ubuntu-24.04", "ssh:build"]
}
```

`AGENTMON_TARGETS=local,wsl:Ubuntu-24.04,ssh:build` is also supported. Explicit
CLI targets take precedence over the environment variable and config file.

There is no server to install or expose. agentmon opens one persistent
`wsl.exe`/`ssh` process per target, sends the current collector source over
stdin, and receives versioned JSON snapshots. Connections are isolated and
automatically retried; an unavailable host is reported without stopping the
other targets. The `TARGET` column keeps identical session IDs on different
machines separate.

Native Windows monitoring supports Claude's session files directly. Native
Codex monitoring uses `codex.exe` plus recently active rollout files because
Windows does not expose Linux-style `/proc/<pid>/fd` links. Agents running
inside WSL get the same full process/file correlation as Linux.

## Windows notification-area icon

`agentmon_tray_windows.py` is a native, dependency-free Windows system-tray
application. It shows a red icon when approval is required, amber while an
agent is working, green when all agents are idle, and grey when none are live.
The icon blinks for attention and sends Windows notifications when approval is
required or a turn finishes. Left-click for a summary; right-click for a menu
with every session, details, stop-blinking, and quit actions.

Launch it without a terminal window using `pyw.exe`:

```powershell
pyw agentmon_tray_windows.py --target wsl:Ubuntu-24.04
```

Install it for the current Windows user so it starts automatically at sign-in:

```powershell
py agentmon_tray_windows.py install-startup --target wsl:Ubuntu-24.04
```

The startup command is stored under the current user's standard Windows `Run`
registry key and uses `pythonw.exe`, so no console window is opened. To remove
it from startup (without stopping an already-running icon):

```powershell
py agentmon_tray_windows.py uninstall-startup
```

When `%APPDATA%\agentmon\targets.json` already contains the desired targets,
omit `--target` from all three commands.

### Windows without Python

The Windows release contains two self-contained files; neither requires a
Python installation:

- `agentmon-tray.exe` — portable version; double-click to run.
- `agentmon-setup.exe` — per-user installer with Start Menu and optional
  sign-in startup entries.

On a fresh install with no targets config, the tray application monitors the
default WSL distribution. Right-click the tray icon to inspect sessions or
quit. Windows may put a newly installed icon under the notification area's `^`
overflow button until it is pinned.

Maintainers can produce both files from the repository's **Build Windows
executable** GitHub Actions workflow. A manual workflow run uploads them as the
`agentmon-windows` artifact; pushing a `v*` tag also attaches them to the
GitHub release. The build recipe is `agentmon_tray.spec`, and the installer
recipe is `installer/agentmon.iss`.

The generated files are not code-signed. Windows SmartScreen may therefore
show an unrecognized-publisher warning until releases are signed with a trusted
code-signing certificate.

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

Use the TUI's `install-hooks` as well, so "waiting for approval" is detected exactly rather than guessed. The tray reads the same targets config and lists remote sessions too; jumping to a tmux pane is intentionally enabled only for local sessions.

## Where the data comes from

| | Live sessions | Status | Usage |
|---|---|---|---|
| Claude Code | `~/.claude/sessions/<pid>.json` (pid must be alive) | `status` busy/idle in the same file | `message.usage` in `~/.claude/projects/*/<session>.jsonl`, deduplicated per request |
| Codex | threads a running `codex` process holds in `~/.codex/thread-writer-locks/` (guardian sub-agents hidden), mapped to `~/.codex/sessions/**/rollout-*.jsonl` | `task_started` / `task_complete` | latest `token_count` event, including `rate_limits` |

Session files alone can't show whether an agent is waiting for approval. Without hooks, agentmon shows `waiting?` when a tool call has no result and the transcript hasn't changed for 5 seconds. A long-running command looks the same, so this is only a guess.

`install-hooks` adds `agentmon.py hook --agent …` entries to `~/.claude/settings.json` and `~/.codex/hooks.json`. Existing hooks are kept, the original files are backed up to `*.agentmon.bak`, and running it again is safe. Each hook appends one line to `~/.local/state/agentmon/events.jsonl`. `PermissionRequest`, or a `Notification` of type `permission_prompt`, turns the row red as **waiting**. Already-running sessions pick up the hooks only after a restart.

Hooks are local to each target. Run `install-hooks` from a stable copy of
`agentmon.py` inside every WSL distribution or SSH host where you want exact
approval detection. Remote monitoring still works without this step, but a
pending tool call is shown as `waiting?` after five seconds because it cannot
be distinguished from a long-running command.

*The screenshots use made-up demo sessions.*

## Tests

```sh
python3 -m unittest test_agentmon.py
```
