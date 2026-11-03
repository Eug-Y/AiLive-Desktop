# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

binaries = [('E:/AiLive/env/Library/bin/libssl-3-x64.dll', '.'), ('E:/AiLive/env/Library/bin/libcrypto-3-x64.dll', '.')]
hiddenimports = ['webview.platforms.edgechromium', 'webview.platforms.winforms', 'webview.http', 'clr_loader', 'pythonnet']
binaries += collect_dynamic_libs('clr_loader')
binaries += collect_dynamic_libs('pythonnet')
hiddenimports += collect_submodules('webview')


a = Analysis(
    ['E:/AiLive/app/app.py'],
    pathex=[],
    binaries=binaries,
    datas=[('E:/AiLive/app/splash.html', '.'), ('E:/AiLive/app/app.ico', '.'), ('E:/AiLive/app/app.png', '.'), ('E:/AiLive/app/ui', 'ui')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PyQt5', 'PySide6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AILiveDesktop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='E:/AiLive/app/version_info.txt',
    icon=['E:/AiLive/app/app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AILiveDesktop',
)
