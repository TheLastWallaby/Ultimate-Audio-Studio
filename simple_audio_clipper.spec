# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = [('ffmpeg.exe', '.'), ('ffprobe.exe', '.')]
hiddenimports = [
    'audioop', 'audioop_lts', 'certifi', 'pydub', 'tinytag', 'pygame', 'send2trash',
    'app.services.updater', 'app.ui.update_dialog', 'app.core.cache_manager',
    'app.controllers', 'app.controllers.library_controller', 'app.controllers.playback_controller',
    'app.controllers.playlist_controller', 'app.controllers.export_controller',
    'app.controllers.download_controller', 'app.controllers.update_controller'
]

for pkg in ('yt_dlp', 'certifi', 'tinytag'):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ['simple_audio_clipper.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'pandas', 'scipy', 'PIL', 'pygame.tests', 'pygame.docs', 'tkinter.test'],
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
    name='Ultimate Audio Studio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
