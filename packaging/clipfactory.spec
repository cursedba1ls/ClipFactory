# PyInstaller spec for ClipFactory.exe (one folder, windowed). Built by packaging/build_windows.py.
# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 (SPECPATH is provided by PyInstaller)
BUILD = Path(os.environ["CLIPFACTORY_BUILD_DIR"])

datas = [
    (str(ROOT / "frontend"), "frontend"),
    (str(BUILD / "build.json"), "."),
]
datas += collect_data_files("faster_whisper")  # Silero VAD model
datas += collect_data_files("webview")
binaries = []
binaries += collect_dynamic_libs("ctranslate2")
binaries += collect_dynamic_libs("onnxruntime")

hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("backend")
hiddenimports += ["webview.platforms.edgechromium", "webview.platforms.winforms", "clr_loader", "pythonnet"]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClipFactory",
    console=False,
    icon=str(ROOT / "packaging" / "clipfactory.ico"),
    version=str(BUILD / "version_info.txt"),
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ClipFactory",
)
