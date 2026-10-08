# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the native macOS menu-bar application."""
from pathlib import Path


root = Path(SPEC).resolve().parent

a = Analysis(
    [str(root / "agentmon_tray_macos.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "agentmon.py"), ".")],
    hiddenimports=["AppKit", "Foundation", "objc"],
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
    [],
    exclude_binaries=True,
    name="agentmon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="agentmon",
)
app = BUNDLE(
    coll,
    name="agentmon.app",
    icon=None,
    bundle_identifier="dev.agentmon.Tray",
    info_plist={
        "CFBundleDisplayName": "agentmon",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
