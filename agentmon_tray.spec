# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the standalone Windows tray executable."""
from pathlib import Path
import sys

# PyInstaller intentionally does not add the spec directory to sys.path.
# Resolve all inputs from SPEC so this also works when invoked from elsewhere.
root = Path(SPEC).resolve().parent
sys.path.insert(0, str(root))

import agentmon_tray_windows as tray  # noqa: E402

build_assets = root / "build" / "windows-assets"
build_assets.mkdir(parents=True, exist_ok=True)
app_icon = build_assets / "agentmon.ico"
app_icon.write_bytes(tray._ico_bytes(tray.COLORS[tray.am.IDLE]))

a = Analysis(
    [str(root / "agentmon_tray_windows.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "agentmon.py"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["curses", "gi"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="agentmon-tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(app_icon),
)
