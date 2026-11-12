"""Where the settings files live (config.json, appearance.json, ...).

The program must know this folder before it can read any setting, so the
choice is kept in the per-user environment variable ``SZU_GUARDIAN_DATA_DIR``
(HKCU\\Environment), the same way the unpack folder is chosen.  Default:
E:\\Apps\\Config\\SZU-Guardian on a local E: disk, else %LOCALAPPDATA%.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .cache_dir import (
    APPS_ROOT_NAME,
    PROJECT_FOLDER,
    _is_fixed_drive,
    read_user_env,
    validate_cache_dir,
    write_user_env,
)

ENV_NAME = "SZU_GUARDIAN_DATA_DIR"
LEGACY_FOLDER = "SZUNetworkGuardian"
# Everything that belongs in the settings folder (logs have their own).
DATA_FILES = ("config.json", "appearance.json", "log_location.json")


def legacy_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / LEGACY_FOLDER


def default_data_dir() -> Path:
    if _is_fixed_drive("E:\\"):
        return Path("E:/") / APPS_ROOT_NAME / "Config" / PROJECT_FOLDER
    return legacy_data_dir()


def current_data_dir() -> Path:
    """This process's setting, then the saved one, then the default."""
    value = os.environ.get(ENV_NAME) or read_user_env(ENV_NAME)
    return Path(value) if value else default_data_dir()


def migrate_data(source: Path, target: Path) -> list[str]:
    """Move the settings files from ``source`` to ``target``.

    A file already present in ``target`` wins (it is the one in use there).
    Returns the names moved.  ``source`` is removed once it is empty.
    """
    try:
        if not source.is_dir() or source.resolve() == target.resolve():
            return []
    except OSError:
        return []
    target.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in DATA_FILES:
        old, new = source / name, target / name
        if not old.is_file() or new.exists():
            continue
        try:
            shutil.move(str(old), str(new))
            moved.append(name)
        except OSError:
            continue
    remove_if_empty(source)
    return moved


def remove_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass


def set_data_dir(value: str | os.PathLike[str], source: Path) -> tuple[Path, list[str]]:
    """Use another settings folder: check it, move the files, remember it."""
    target = validate_cache_dir(value)
    moved = migrate_data(source, target)
    write_user_env(ENV_NAME, str(target))
    return target, moved


def ensure_data_dir() -> tuple[Path, list[str]]:
    """Run once at start-up: settle on the folder and bring old files over.

    Settings saved by earlier versions in %LOCALAPPDATA% are moved to the
    chosen folder, and the choice is saved so every later start agrees.
    """
    target = current_data_dir()
    try:
        target = validate_cache_dir(target)
    except ValueError:
        target = legacy_data_dir()  # e.g. the drive is gone: stay usable
        target.mkdir(parents=True, exist_ok=True)
    moved = migrate_data(legacy_data_dir(), target)
    if read_user_env(ENV_NAME) != str(target) or os.environ.get(ENV_NAME) != str(target):
        write_user_env(ENV_NAME, str(target))
    return target, moved
