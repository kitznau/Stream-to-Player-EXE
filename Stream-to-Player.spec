# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['stream_player_test.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('icon.ico',     '.'),
        ('hls_proxy.py', '.'),
    ],
    # Fix #15: hls_proxy is imported at runtime via importlib — declare it as
    # hiddenimport so PyInstaller includes it in the frozen bundle reliably.
    hiddenimports=['hls_proxy'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    # Fix #14: optimize=1 strips docstrings and assert statements,
    # reducing bytecode size and therefore final EXE size.
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Stream-to-Player',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)
