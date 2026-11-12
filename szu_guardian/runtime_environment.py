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
