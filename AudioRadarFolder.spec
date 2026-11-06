# -*- mode: python ; coding: utf-8 -*-
#
# One-DIRECTORY build (SSD fork). The upstream one-file build can show a blank
# dashboard window (a known QtWebEngine quirk), so this folder build is the
# recommended packaging: everything lands in dist/VisualAudioOverlay-SSD/ and
# VisualAudioOverlay-SSD.exe inside it runs offline.

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # Bundle the web UI (HTML/CSS/JS), its icons, and the self-hosted font so the
    # packaged .exe renders correctly offline for end users.
    datas=[
        ('dashboard_v2', 'dashboard_v2'),
        ('assets', 'assets'),
    ],
    # process_loopback is imported lazily (inside functions), and the COM/audio
    # stack uses dynamic imports PyInstaller can miss - list them explicitly.
    hiddenimports=[
        'process_loopback',
        'comtypes',
        'pycaw',
        'pycaw.api.audioclient',
        'pycaw.utils',
        'psutil',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VisualAudioOverlay-SSD',
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
    icon='assets/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VisualAudioOverlay-SSD',
)
