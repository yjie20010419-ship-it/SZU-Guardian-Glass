"""Notice when the previous run ended without a normal exit.

A small ``running.flag`` file sits in the settings folder while the guardian
runs and is removed on a normal exit. If it is still there at the next start,
the program was ended from outside (task kill, a parent app being updated,
power loss…), and the gap without monitoring is written to the log.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from .outage_stats import format_duration

MARKER = "running.flag"


def begin(data_dir: Path) -> bool:
    """Mark this run as started; True if the previous run did not exit normally."""
    path = Path(data_dir) / MARKER
    unclean = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"pid={os.getpid()} started={dt.datetime.now():%Y-%m-%d %H:%M:%S}\n", encoding="utf-8")
    except OSError:
        pass  # a read-only folder must not stop the guardian
    return unclean


def end(data_dir: Path) -> None:
    """Normal exit: remove the marker."""
    try:
        (Path(data_dir) / MARKER).unlink(missing_ok=True)
    except OSError:
        pass


def unclean_message(last_entry: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """Log line for an unclean previous exit, with the unmonitored gap if known."""
    text = "上次程序没有正常退出（可能被系统或其他程序强制结束）"
    if last_entry is None:
        return text
    now = now or dt.datetime.now()
    gap = max(0.0, (now - last_entry).total_seconds())
    when = f"{last_entry:%H:%M}" if last_entry.date() == now.date() else f"{last_entry:%m-%d %H:%M}"
    return f"{text}。上次运行的最后一条记录在 {when}，到这次启动之间约 {format_duration(gap)}没有守护"
