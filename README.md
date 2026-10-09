# agentmon

agentmon shows the status of **Claude Code** and **Codex** sessions in the
Windows notification area, Ubuntu top bar, or macOS menu bar. It can monitor:

- Agents running locally
- Agents running in WSL on Windows
- Agents running on remote servers over SSH

It tracks processing, approval, idle, and exited states. The icon blinks when
attention is needed or a task finishes. Windows and Ubuntu also display system
notifications.

## Download

Download the package for your system from
[GitHub Releases](https://github.com/jacques7zhu/agent_monitor/releases/latest).
You do not need to configure Python locally. The Ubuntu package installs its
system dependencies through the package manager.

| Platform | Recommended download | Alternative |
|---|---|---|
| Windows x64 | `agentmon-setup.exe` | `agentmon-tray.exe` portable app |
| Ubuntu x86_64 | `agentmon_VERSION_amd64.deb` | `agentmon-linux-x86_64` terminal app |
| macOS Apple Silicon | `agentmon-macos-arm64.dmg` | `agentmon-macos-arm64` terminal app |
| macOS Intel | `agentmon-macos-x86_64.dmg` | `agentmon-macos-x86_64` terminal app |

## Install

### Windows

1. Download and run `agentmon-setup.exe`.
2. Choose the optional start-at-sign-in task during installation.
3. Start agentmon and look for its icon in the notification area. Windows may
   initially place it under the `^` overflow menu.

Without a targets configuration, the Windows app monitors the default WSL
distribution. You can also run the portable `agentmon-tray.exe` directly.

Windows SmartScreen may show an unknown-publisher warning because the current
release is not signed with a commercial certificate.

### Ubuntu

Download the `.deb` file and install it:

```sh
sudo apt install ./agentmon_VERSION_amd64.deb
```

Open **agentmon** from the application menu after installation. To launch it
automatically at login, add `agentmon-tray` to Ubuntu's Startup Applications.

### macOS

1. Download `agentmon-macos-arm64.dmg` for Apple Silicon or
   `agentmon-macos-x86_64.dmg` for an Intel Mac.
2. Open the DMG and drag `agentmon.app` into **Applications**.
3. Start agentmon and enable **Start at Login** from its menu if desired.

The app is ad-hoc signed but not notarized. If macOS blocks the first launch,
Control-click `agentmon.app`, choose **Open**, and confirm.

## Configure monitoring targets

Windows monitors the default WSL distribution by default. Ubuntu and macOS
monitor the local system. To monitor multiple targets, create:

- Windows: `%APPDATA%\agentmon\targets.json`
- Ubuntu and macOS: `~/.config/agentmon/targets.json`

Example:

```json
{
  "targets": [
    "local",
    "wsl:Ubuntu-24.04",
    "ssh:build"
  ]
}
```

Supported target values:

- `local` — the current operating system
- `wsl` — the default WSL distribution on Windows
- `wsl:Ubuntu-24.04` — a named WSL distribution
- `ssh:build` — the `build` host from `~/.ssh/config`
- `ssh:user@example.com` — a directly specified SSH user and host

Quit and restart agentmon after changing the configuration.

SSH targets require key-based authentication and `python3` on the remote
server. agentmon uses the system SSH client; it does not require a remote
service or additional open ports. An unavailable target does not interrupt
monitoring of other targets.

## Status

| Icon | Status | Meaning |
|---|---|---|
| 🔴 Red | `waiting` | The agent is waiting for approval |
| 🟠/🟡 Orange or yellow | `waiting?` | The agent may be waiting, based on recent activity |
| 🟡 Yellow | `processing` | The agent is processing a task |
| 🟢 Green | `idle` | The agent has finished and is idle |
| ⚪ Grey | no agents | No agents are currently running |

Click the icon to inspect sessions, working directories, models, token usage,
and recent prompts and replies. The icon blinks when approval is needed, a task
finishes, or a running session exits. Windows and Ubuntu also display system
notifications.

The Windows menu includes **Test notification and blinking**. If the icon
blinks during the test but no notification appears, check:

- Whether agentmon notifications are enabled under **Settings → System →
  Notifications**
- Whether Windows **Do not disturb** is enabled
- Whether the icon is hidden under the notification area's `^` overflow menu

## Screenshots

<p>
  <img src="docs/topbar.png" alt="agentmon in the Ubuntu top bar" height="56"><br>
  <img src="docs/menu.png" alt="agentmon session menu" width="480">
</p>

![agentmon details window](docs/details.png)

![agentmon terminal view](docs/terminal.png)
