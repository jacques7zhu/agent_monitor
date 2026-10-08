#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || -z $1 ]]; then
  echo "usage: $0 VERSION [ARCH]" >&2
  exit 2
fi

repo_root=$(cd "$(dirname "$0")/.." && pwd)
version=${1#v}
version=${version//[^0-9A-Za-z.+:~-]/.}
arch=${2:-amd64}
if [[ -z $version ]]; then
  echo "version must contain at least one character after an optional v prefix" >&2
  exit 2
fi
build_root="$repo_root/build/deb/agentmon_${version}_${arch}"
output="$repo_root/dist/agentmon_${version}_${arch}.deb"

rm -rf "$build_root"
install -d "$build_root/DEBIAN" "$build_root/usr/bin" \
  "$build_root/usr/lib/agentmon" "$build_root/usr/share/applications"
install -m 0644 "$repo_root/agentmon.py" "$build_root/usr/lib/agentmon/agentmon.py"
install -m 0644 "$repo_root/agentmon_tray.py" "$build_root/usr/lib/agentmon/agentmon_tray.py"

printf '%s\n' '#!/bin/sh' \
  'exec python3 /usr/lib/agentmon/agentmon.py "$@"' \
  > "$build_root/usr/bin/agentmon"
printf '%s\n' '#!/bin/sh' \
  'exec python3 /usr/lib/agentmon/agentmon_tray.py "$@"' \
  > "$build_root/usr/bin/agentmon-tray"
chmod 0755 "$build_root/usr/bin/agentmon" "$build_root/usr/bin/agentmon-tray"

cat > "$build_root/DEBIAN/control" <<EOF
Package: agentmon
Version: $version
Section: devel
Priority: optional
Architecture: $arch
Maintainer: agentmon
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-3.0, gir1.2-notify-0.7, gir1.2-ayatanaappindicator3-0.1
Description: tray and terminal monitor for Claude Code and Codex
 Monitor local and remote Claude Code and Codex sessions from an Ubuntu
 top-bar indicator or interactive terminal dashboard.
EOF

cat > "$build_root/usr/share/applications/agentmon.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=agentmon
Comment=Monitor Claude Code and Codex sessions
Exec=agentmon-tray
Icon=utilities-system-monitor
Terminal=false
Categories=Development;Monitor;
StartupNotify=false
EOF

mkdir -p "$(dirname "$output")"
dpkg-deb --build --root-owner-group "$build_root" "$output"
echo "$output"
