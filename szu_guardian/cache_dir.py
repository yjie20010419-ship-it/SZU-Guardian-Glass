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
from pathlib import Path

ENV_NAME = "SZU_GUARDIAN_CACHE"
# E:\Apps holds everything installed or cached for these projects:
# E:\Apps\Cache\<project> for caches, E:\Apps\Tools\<tool> for tools.
APPS_ROOT_NAME = "Apps"
CACHE_FOLDER_NAME = "Cache"
PROJECT_FOLDER = "SZU-Guardian"
_UNEXPANDED = f"%{ENV_NAME}%"


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
    """E:\\Apps\\Cache\\SZU-Guardian when E: is a local hard disk, else under LocalAppData."""
    if _is_fixed_drive("E:\\"):
        return Path("E:/") / APPS_ROOT_NAME / CACHE_FOLDER_NAME / PROJECT_FOLDER
    return local_cache_dir()


def registered_cache_dir() -> Path | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, ENV_NAME)
    except OSError:
        return None
    value = str(value).strip()
    return Path(value) if value else None


def validate_cache_dir(value: str | os.PathLike[str]) -> Path:
    """Check a folder path from the settings page (cache or logs) and create it."""
    text = str(value).strip().strip('"')
    if not text:
        raise ValueError("请填写文件夹路径")
    if "%" in text:
        raise ValueError("缓存路径里不能包含 %")
    path = Path(text)
    if not path.is_absolute() or not path.drive:
        raise ValueError("请填写带盘符的完整路径，例如 E:\\Apps\\Logs\\SZU-Guardian")
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
    if sys.platform != "win32":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, ENV_NAME, 0, winreg.REG_SZ, str(path))
    _broadcast_environment_change()


def set_cache_dir(value: str | os.PathLike[str]) -> Path:
    path = validate_cache_dir(value)
    register_cache_dir(path)
    return path


def ensure_registered() -> Path:
    """Make sure the variable exists and its folder is usable; return it."""
    current = registered_cache_dir()
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


def stale_unpacks(search_dirs: list[Path], current: Path | None = None) -> list[Path]:
    """Leftover guardian unpack folders (from crashes or forced kills)."""
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
            except OSError:
                continue
            found.append(child)
    return found


def cleanup_stale(search_dirs: list[Path], current: Path | None = None) -> tuple[int, int]:
    """Delete leftover unpack folders; return (folders removed, bytes freed).

    Only call this while holding the single-instance lock, so no other
    guardian process can be using one of these folders.
    """
    removed = freed = 0
    for folder in stale_unpacks(search_dirs, current):
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
    unique = []
    for item in dirs:
        if item not in unique:
            unique.append(item)
    return unique


def format_size(size: int) -> str:
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.0f} MB"
    return f"{size / 1024:.0f} KB"
