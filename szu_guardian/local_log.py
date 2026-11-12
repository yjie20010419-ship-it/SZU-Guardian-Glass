from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import threading
from pathlib import Path


LOG_RETENTION_DAYS = 7
LOG_GLOB = "guardian-*.log"
LOCATION_FILE = "log_location.json"


def legacy_log_directory(data_dir: Path | None = None) -> Path:
    """``logs`` inside a settings folder; by default the original location
    %LOCALAPPDATA%\\SZUNetworkGuardian\\logs used before v1.0.1."""
    if data_dir is None:
        from .data_dir import legacy_data_dir

        data_dir = legacy_data_dir()
    return data_dir / "logs"


LOGS_FOLDER_NAME = "Logs"


def default_log_directory(data_dir: Path | None = None) -> Path:
    """E:\\Apps\\SZU-Guardian\\Logs on a local E: disk, else the data folder."""
    from .cache_dir import project_root

    root = project_root()
    return root / LOGS_FOLDER_NAME if root else legacy_log_directory(data_dir)


def old_log_locations() -> list[Path]:
    """Default log folders of earlier versions, oldest first."""
    from .cache_dir import old_layout_dir

    return [legacy_log_directory(None), old_layout_dir(LOGS_FOLDER_NAME)]


def chosen_log_directory(data_dir: Path) -> Path:
    """The folder picked in the settings, or the default."""
    try:
        payload = json.loads((data_dir / LOCATION_FILE).read_text(encoding="utf-8"))
        value = str(payload.get("path", "")).strip()
        if value:
            return Path(value)
    except (OSError, ValueError, AttributeError):
        pass
    return default_log_directory(data_dir)


def save_log_location(data_dir: Path, directory: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    temporary = data_dir / (LOCATION_FILE + ".tmp")
    temporary.write_text(json.dumps({"path": str(directory)}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(data_dir / LOCATION_FILE)


def migrate_logs(source: Path, target: Path) -> int:
    """Move every daily log from ``source`` into ``target``.

    When both folders hold the same day, the lines are merged in time order
    (every line starts with its timestamp).  Returns the number of files moved.
    """
    try:
        if not source.is_dir() or source.resolve() == target.resolve():
            return 0
    except OSError:
        return 0
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for old in sorted(source.glob(LOG_GLOB)):
        new = target / old.name
        try:
            if new.exists():
                lines = old.read_text(encoding="utf-8", errors="replace").splitlines()
                lines += new.read_text(encoding="utf-8", errors="replace").splitlines()
                # Stable sort on the "YYYY-MM-DD HH:MM:SS" prefix keeps the
                # original order of lines written in the same second.
                lines.sort(key=lambda line: line[:19])
                new.write_text("\n".join(lines) + "\n", encoding="utf-8")
                old.unlink()
            else:
                shutil.move(str(old), str(new))
            moved += 1
        except OSError:
            continue
    try:
        source.rmdir()  # only succeeds once it is empty
    except OSError:
        pass
    return moved


class LocalLog:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or default_log_directory()
        self._lock = threading.Lock()
        self._last_cleanup: dt.datetime | None = None
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cleanup()

    def move_to(self, directory: Path) -> int:
        """Switch to another folder, taking the existing logs along."""
        with self._lock:
            moved = migrate_logs(self.directory, directory)
            directory.mkdir(parents=True, exist_ok=True)
            self.directory = directory
        return moved

    def write(self, message: str, level: str = "info") -> None:
        now = dt.datetime.now()
        safe_message = " ".join(str(message).splitlines())
        line = f"{now:%Y-%m-%d %H:%M:%S} [{level.upper()}] {safe_message}\n"
        path = self.directory / f"guardian-{now:%Y-%m-%d}.log"
        with self._lock:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
            if (
                self._last_cleanup is None
                or now - self._last_cleanup >= dt.timedelta(hours=1)
            ):
                self.cleanup(now)

    def read_day(
        self,
        day: dt.date | None = None,
        limit: int = 2000,
    ) -> list[tuple[dt.datetime, str]]:
        """Entries of one day's log file, oldest first (at most ``limit``)."""
        return [
            (stamp, message)
            for stamp, _level, message in self.read_entries(day)[-limit:]
        ]

    def read_entries(
        self,
        day: dt.date | None = None,
    ) -> list[tuple[dt.datetime, str, str]]:
        """(time, LEVEL, message) for every line of one day's log file."""
        day = day or dt.date.today()
        path = self.directory / f"guardian-{day:%Y-%m-%d}.log"
        try:
            with self._lock:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        entries = []
        for line in lines:
            try:
                stamp = dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            message = line[20:]
            level = "INFO"
            if message.startswith("[") and "] " in message:
                level, message = message[1:].split("] ", 1)
            entries.append((stamp, level, message))
        return entries

    def cleanup(self, now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now()
        cutoff = now.date() - dt.timedelta(days=LOG_RETENTION_DAYS)
        for path in self.directory.glob("guardian-*.log"):
            try:
                file_date = dt.date.fromisoformat(path.stem.removeprefix("guardian-"))
                if file_date < cutoff:
                    path.unlink()
            except (OSError, ValueError):
                continue
        self._last_cleanup = now

    def open_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(self.directory)  # type: ignore[attr-defined]
        else:
            raise OSError("打开日志目录目前仅支持 Windows")
