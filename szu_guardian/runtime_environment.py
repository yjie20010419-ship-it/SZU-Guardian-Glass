from __future__ import annotations
import os
from pathlib import Path


def sanitize_external_environment(bundle_root: str | None) -> None:
    """Keep the frozen Qt app's private plugin/DLL directories out of UU's environment.

    Called after QApplication has captured its internal library paths, before
    any background operation starts. Claude's UU restart module stays unchanged.
    """
    if not bundle_root:
        return
    root = Path(bundle_root).resolve()
    def bundled(value: str) -> bool:
        try:
            return bool(value) and Path(value).resolve().is_relative_to(root)
        except (OSError, ValueError):
            return False
    for key in ('QT_PLUGIN_PATH', 'QML2_IMPORT_PATH', 'QML_IMPORT_PATH', 'PATH'):
        if key not in os.environ:
            continue
        clean = [part for part in os.environ[key].split(os.pathsep) if not bundled(part)]
        if clean:
            os.environ[key] = os.pathsep.join(clean)
        else:
            os.environ.pop(key, None)


def inside_job() -> bool:
    """True if this process sits in a Windows job object.

    That happens when the guardian was started by another app (a terminal, an
    IDE, an AI assistant…) instead of the desktop or the autostart entry. Ending
    or updating that app can then end the guardian, and UU Remote with it.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32")  # private copy: argtypes stay local
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        result = wintypes.BOOL()
        if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(result)):
            return False
        return bool(result.value)
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False
