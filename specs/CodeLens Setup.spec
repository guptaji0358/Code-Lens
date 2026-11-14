# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['../scripts/installer_app.py'],
    pathex=[],
    binaries=[],
    datas=[('../Assets', 'Assets'), ('../Installer/LICENSE.txt', '.'), ('../out/CodeLens', 'payload/CodeLens'), ('../out/CodeLensCLI', 'payload/CodeLensCLI'), ('../out/CodeLens_Uninstall.exe', 'uninstaller')],
    hiddenimports=[],
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
    name='CodeLens Setup',
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
    icon=['../Assets/app_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CodeLens Setup',
)
