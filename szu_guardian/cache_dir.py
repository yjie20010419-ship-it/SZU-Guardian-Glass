"""Where the one-file EXE unpacks itself at start-up.

The EXE is built with ``--runtime-tmpdir %SZU_GUARDIAN_CACHE%``; the
PyInstaller bootloader expands that per-user environment variable on
Windows, so the unpack location can be changed without rebuilding.  The
variable lives in ``HKCU\\Environment``; a change applies on the next start.

If the variable were missing, the bootloader would unpack into a folder
literally named ``%SZU_GUARDIAN_CACHE%`` under the working directory, so the
app re-registers it on every start.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ENV_NAME = "SZU_GUARDIAN_CACHE"
# E:\Apps is the umbrella folder for all projects, one sub-folder each:
#   E:\Apps\SZU-Guardian\Cache    unpacked program files
#   E:\Apps\SZU-Guardian\Config   settings
#   E:\Apps\SZU-Guardian\Logs     daily logs
# (v1.0.0–v1.0.2 used the type-first layout E:\Apps\<Kind>\SZU-Guardian;
# those folders are migrated and removed.)
APPS_ROOT_NAME = "Apps"
CACHE_FOLDER_NAME = "Cache"
PROJECT_FOLDER = "SZU-Guardian"
_UNEXPANDED = f"%{ENV_NAME}%"


def project_root() -> Path | None:
    """E:\\Apps\\SZU-Guardian when E: is a local hard disk, else None."""
    if _is_fixed_drive("E:\\"):
        return Path("E:/") / APPS_ROOT_NAME / PROJECT_FOLDER
    return None


def old_layout_dir(kind: str) -> Path:
    """Where v1.0.0–v1.0.2 put this kind of folder (E:\\Apps\\<kind>\\SZU-Guardian)."""
    return Path("E:/") / APPS_ROOT_NAME / kind / PROJECT_FOLDER


def same_path(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def remove_old_layout(kind: str) -> None:
    """Delete E:\\Apps\\<kind>\\SZU-Guardian and E:\\Apps\\<kind> once empty."""
    folder = old_layout_dir(kind)
    for item in (folder, folder.parent):
        try:
            item.rmdir()  # only removes an empty folder
        except OSError:
            continue  # already gone, or still has content


def local_cache_dir() -> Path:
    """Always-available fallback inside the user's own profile."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "SZUNetworkGuardian" / "cache"


def _is_fixed_drive(root: str) -> bool:
    """True for a local hard disk (not a CD drive, USB stick or network share)."""
    if sys.platform != "win32":
        return Path(root).exists()
    try:
        import ctypes

        DRIVE_FIXED = 3
        return ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED
    except (AttributeError, OSError):
        return False


def default_cache_dir() -> Path:
    """E:\\Apps\\SZU-Guardian\\Cache on a local E: disk, else under LocalAppData."""
    root = project_root()
    return root / CACHE_FOLDER_NAME if root else local_cache_dir()


def read_user_env(name: str) -> str | None:
    """A per-user environment variable as stored in HKCU\\Environment."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    value = str(value).strip()
    return value or None


def write_user_env(name: str, value: str) -> None:
    """Persist a per-user environment variable and tell running programs."""
    os.environ[name] = value
    if sys.platform != "win32":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    _broadcast_environment_change()


def registered_cache_dir() -> Path | None:
    value = read_user_env(ENV_NAME)
    return Path(value) if value else None


def validate_cache_dir(value: str | os.PathLike[str]) -> Path:
    """Check a folder path from the settings page (cache or logs) and create it."""
    text = str(value).strip().strip('"')
    if not text:
        raise ValueError("请填写文件夹路径")
    if "%" in text:
        raise ValueError("路径里不能包含 %")
    path = Path(text)
    if not path.is_absolute() or not path.drive:
        raise ValueError("请填写带盘符的完整路径，例如 E:\\Apps\\SZU-Guardian\\Logs")
    if not Path(path.drive + "\\").exists():
        raise ValueError(f"找不到磁盘 {path.drive}")
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"无法写入该文件夹：{exc}") from exc
    return path


def _broadcast_environment_change() -> None:
    try:
        import ctypes

        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 2000, ctypes.byref(result),
        )
    except (AttributeError, OSError):
        pass


def register_cache_dir(path: Path) -> None:
    write_user_env(ENV_NAME, str(path))


def set_cache_dir(value: str | os.PathLike[str]) -> Path:
    path = validate_cache_dir(value)
    register_cache_dir(path)
    return path


def ensure_registered() -> Path:
    """Make sure the variable exists and its folder is usable; return it."""
    current = registered_cache_dir()
    if same_path(current, old_layout_dir(CACHE_FOLDER_NAME)):
        current = None  # the old default: move to the new layout
    if current is not None:
        try:
            return validate_cache_dir(current)
        except ValueError:
            pass  # e.g. the drive went away: fall back to the default.
    try:
        return set_cache_dir(default_cache_dir())
    except ValueError:
        # E: exists but cannot be written (full, protected...): use the profile.
        return set_cache_dir(local_cache_dir())


def running_from() -> Path | None:
    """The ``_MEIxxxx`` folder this process was unpacked into, if any."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


def _is_guardian_unpack(folder: Path) -> bool:
    return folder.name.startswith("_MEI") and (folder / "szu_guardian").is_dir()


def _folder_size(folder: Path) -> int:
    total = 0
    for item in folder.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


# A second copy that is still unpacking/loading has not taken the
# single-instance lock yet, so its folder must not be treated as a leftover.
MIN_STALE_AGE_SECONDS = 600


def _is_recent(folder: Path, min_age: float) -> bool:
    if min_age <= 0:
        return False
    try:
        info = folder.stat()
    except OSError:
        return True
    newest = max(info.st_mtime, info.st_ctime)
    return time.time() - newest < min_age


def stale_unpacks(
    search_dirs: list[Path],
    current: Path | None = None,
    min_age: float = MIN_STALE_AGE_SECONDS,
) -> list[Path]:
    """Leftover guardian unpack folders (from crashes or forced kills).

    Folders touched within ``min_age`` seconds are skipped: they may belong
    to a copy that is starting up right now.
    """
    found = []
    current_resolved = current.resolve() if current else None
    for directory in search_dirs:
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir() or not _is_guardian_unpack(child):
                    continue
                if current_resolved and child.resolve() == current_resolved:
                    continue
                if _is_recent(child, min_age):
                    continue
            except OSError:
                continue
            found.append(child)
    return found


def cleanup_stale(
    search_dirs: list[Path],
    current: Path | None = None,
    min_age: float = MIN_STALE_AGE_SECONDS,
) -> tuple[int, int]:
    """Delete leftover unpack folders; return (folders removed, bytes freed).

    Only call this while holding the single-instance lock, so no other
    guardian process can be using one of these folders.
    """
    removed = freed = 0
    for folder in stale_unpacks(search_dirs, current, min_age):
        size = _folder_size(folder)
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            removed += 1
            freed += size
    # The very first launch on a PC happens before the variable exists, so
    # it unpacks into a folder literally named %SZU_GUARDIAN_CACHE% next to
    # the EXE; remove it once it is empty.
    for directory in search_dirs:
        if directory.name == _UNEXPANDED:
            try:
                directory.rmdir()
            except OSError:
                pass
        elif same_path(directory, old_layout_dir(CACHE_FOLDER_NAME)):
            remove_old_layout(CACHE_FOLDER_NAME)
    return removed, freed


def default_search_dirs() -> list[Path]:
    dirs = []
    registered = registered_cache_dir()
    if registered:
        dirs.append(registered)
    for name in ("TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            dirs.append(Path(value))
    # Unpacks made while the variable was missing land under the working dir.
    dirs.append(Path.cwd() / _UNEXPANDED)
    # The cache folder of the old type-first layout (v1.0.0–v1.0.2).
    dirs.append(old_layout_dir(CACHE_FOLDER_NAME))
    unique = []
    for item in dirs:
        if item not in unique:
            unique.append(item)
    return unique


def format_size(size: int) -> str:
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.0f} MB"
    return f"{size / 1024:.0f} KB"
