# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build recipe for SZU-Guardian-Glass (run through build.ps1).
#
# The one-file EXE unpacks into %SZU_GUARDIAN_CACHE% (a per-user environment
# variable the app keeps registered; see szu_guardian/cache_dir.py).
#
# Parts of Qt this app never uses are dropped to make the EXE smaller and
# quicker to unpack at every start.

import os

PROJECT = os.path.abspath(SPECPATH)


def _norm(name):
    return name.replace("\\", "/").lower()


# Destination paths (prefix match) left out of the bundle.
DROP_PREFIXES = (
    "pyside6/opengl32sw.dll",              # software OpenGL fallback (~20 MB)
    "pyside6/translations/",               # no QTranslator is loaded
    "pyside6/plugins/platforms/qdirect2d.dll",
    "pyside6/plugins/platforms/qminimal.dll",
    "pyside6/plugins/tls/",                # only local sockets use QtNetwork
    "pyside6/plugins/networkinformation/",
    "pyside6/plugins/generic/",
    "pyside6/plugins/styles/",             # the app forces the Fusion style
    "szu_guardian/assets/app.png",         # source artwork; the UI uses app.ico
)
# Image plugins: only ICO and SVG are needed.
KEEP_IMAGE_PLUGINS = {"qico.dll", "qsvg.dll"}


def keep(entry):
    name = _norm(entry[0])
    if name.startswith(DROP_PREFIXES):
        return False
    if name.startswith("pyside6/plugins/imageformats/"):
        return name.rsplit("/", 1)[-1] in KEEP_IMAGE_PLUGINS
    return True


a = Analysis(
    [os.path.join(PROJECT, "main.py")],
    pathex=[PROJECT],
    binaries=[],
    datas=[(os.path.join(PROJECT, "szu_guardian", "assets"), "szu_guardian/assets")],
    hiddenimports=["PySide6.QtSvg"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PIL", "pystray", "unittest", "pydoc", "test"],
    noarchive=False,
    optimize=0,
)
a.binaries = [entry for entry in a.binaries if keep(entry)]
a.datas = [entry for entry in a.datas if keep(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SZU-Guardian-Glass",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir="%SZU_GUARDIAN_CACHE%",
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(PROJECT, "szu_guardian", "assets", "app.ico")],
)
