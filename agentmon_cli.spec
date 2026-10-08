# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform PyInstaller recipe for the standalone terminal monitor."""
from pathlib import Path


root = Path(SPEC).resolve().parent

a = Analysis(
    [str(root / "agentmon.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "agentmon.py"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["gi"],
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
    name="agentmon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
