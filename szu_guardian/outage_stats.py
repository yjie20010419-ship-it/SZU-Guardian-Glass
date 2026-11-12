"""Per-day outage statistics computed from the local log files.

An outage starts at the first warning that the network is down (expired
authentication, or the direct internet probe failing) and ends at the next
success line ("网络连接正常" / "已自动重连") or when the program stops.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

DOWN_MARKERS = ("认证已失效", "直连外网不可用", "外网仍不可用", "检测或重连失败")
UP_MARKERS = ("网络连接正常", "已自动重连")
STOP_MARKERS = ("程序退出", "监控已停止")


@dataclass(slots=True)
class Outage:
    start: dt.datetime
    end: dt.datetime
    ongoing: bool = False

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass(slots=True)
class DayStats:
    day: dt.date
    outages: list[Outage] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.outages)

    @property
    def total_seconds(self) -> float:
        return sum(item.seconds for item in self.outages)

    @property
    def longest(self) -> Outage | None:
        return max(self.outages, key=lambda item: item.seconds, default=None)


def _is_down(level: str, message: str) -> bool:
    return level in ("WARNING", "ERROR") and any(marker in message for marker in DOWN_MARKERS)


def _is_up(level: str, message: str) -> bool:
    return level == "SUCCESS" and any(marker in message for marker in UP_MARKERS)


def compute_day(
    day: dt.date,
    entries: list[tuple[dt.datetime, str, str]],
    now: dt.datetime | None = None,
) -> DayStats:
    """``entries`` are (time, LEVEL, message) for one day, oldest first."""
    stats = DayStats(day)
    started: dt.datetime | None = None
    for stamp, level, message in entries:
        if started is None:
            if _is_down(level, message):
                started = stamp
        elif _is_up(level, message) or any(marker in message for marker in STOP_MARKERS):
            stats.outages.append(Outage(started, stamp))
            started = None
    if started is not None:
        end_of_day = dt.datetime.combine(day, dt.time(23, 59, 59))
        now = now or dt.datetime.now()
        stats.outages.append(Outage(started, min(now, end_of_day), ongoing=now <= end_of_day))
    return stats


def format_duration(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分钟" if seconds < 30 or minutes >= 10 else f"{minutes} 分 {seconds} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分钟"


def describe(stats: DayStats, label: str) -> str:
    if not stats.count:
        return f"{label}：没有掉线"
    longest = stats.longest
    text = (
        f"{label}：掉线 {stats.count} 次，累计 {format_duration(stats.total_seconds)}，"
        f"最长 {format_duration(longest.seconds)}（{longest.start:%H:%M}–{longest.end:%H:%M}"
    )
    text += "，仍未恢复）" if longest.ongoing else "）"
    return text
