# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_all

datas = [('resource/font_map_table.json', 'resource')]
binaries = []
hiddenimports = []
datas += collect_data_files('certifi')
tmp_ret = collect_all('fontTools')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 注意：PIL 不能排除 —— 考试模式的滑块验证码识别走「纯 PIL」这条路
    # （cv2 / ddddocr / numpy 体积太大，源码里保留为可选加速路径，打包时不带）
    excludes=['ddddocr', 'onnxruntime', 'numpy', 'cv2', 'celery', 'flask', 'matplotlib', 'pandas', 'scipy', 'tkinter', 'PyQt5'],
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
    name='chaoxing-3.1.4-fixed',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
