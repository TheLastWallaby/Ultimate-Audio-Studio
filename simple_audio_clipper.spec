# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

import deno

datas = []
# deno.exe is the JavaScript runtime yt-dlp needs for YouTube (installed by the yt-dlp[deno] extra).
binaries = [('ffmpeg.exe', '.'), ('ffprobe.exe', '.'), (deno.find_deno_bin(), '.')]
hiddenimports = [
    'certifi', 'tinytag', 'pygame', 'send2trash',
    'app.services.updater', 'app.ui.update_dialog', 'app.core.cache_manager',
    'app.controllers', 'app.controllers.library_controller', 'app.controllers.playback_controller',
    'app.controllers.playlist_controller', 'app.controllers.export_controller',
    'app.controllers.download_controller', 'app.controllers.update_controller',
    # app.main imports this dynamically to close the splash; PyInstaller only bundles it when listed.
    'pyi_splash',
]

for pkg in ('yt_dlp', 'yt_dlp_ejs', 'certifi', 'tinytag'):
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

# Shown by the bootloader while the one-file .exe unpacks (several seconds on older PCs), so a
# double-click gives immediate feedback instead of a second launch and an "Already running" message.
# No live text: in one-file mode it would list every file being unpacked. app.main closes it.
splash = Splash(
    'assets/splash.png',
    binaries=a.binaries,
    datas=a.datas,
    minify_script=True,
)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    splash.binaries,
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
