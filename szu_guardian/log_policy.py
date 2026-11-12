"""Decide which monitor events are worth keeping in the local log file.

Routine checks ("正在检测…", "网络连接正常", "下次检测…") repeat every few
minutes and bury the lines that matter.  While the network stays healthy
they are summarised in one heartbeat line per hour; every state change,
warning, error and diagnostic note is still written immediately.
"""

from __future__ import annotations

import time
from typing import Callable

ROUTINE_ONLINE_PREFIX = "网络连接正常"
HEARTBEAT_SECONDS = 3600.0


class RoutineLogFilter:
    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ):
        self.clock = clock
        self.heartbeat_seconds = heartbeat_seconds
        self._online = False
        self._quiet_since: float | None = None
        self._quiet_checks = 0
        self._latencies: list[int] = []

    def lines_to_write(
        self,
        state: str,
        message: str,
        level: str = "info",
        latency_ms: int | None = None,
    ) -> list[str]:
        """Return the lines to persist for this event (possibly none)."""
        if state in ("checking", "waiting") and level == "info":
            return []

        routine_online = (
            state == "online"
            and level == "success"
            and message.startswith(ROUTINE_ONLINE_PREFIX)
        )
        if routine_online and self._online:
            return self._count_quiet_check(latency_ms)

        lines = []
        if state in ("online", "offline", "stopped"):
            lines.extend(self._flush_summary())
            self._online = state == "online"
            self._quiet_since = self.clock() if self._online else None
        elif level in ("warning", "error"):
            # Something went wrong mid-check: close the quiet period so the
            # recovery that follows is written even if it reads "正常".
            lines.extend(self._flush_summary())
            self._online = False
            self._quiet_since = None
        lines.append(message)
        return lines

    def _count_quiet_check(self, latency_ms: int | None) -> list[str]:
        now = self.clock()
        if self._quiet_since is None:
            self._quiet_since = now
        self._quiet_checks += 1
        if latency_ms is not None:
            self._latencies.append(latency_ms)
        if now - self._quiet_since >= self.heartbeat_seconds:
            return self._flush_summary()
        return []

    def _flush_summary(self) -> list[str]:
        if not self._quiet_checks or self._quiet_since is None:
            return []
        minutes = max(1, round((self.clock() - self._quiet_since) / 60))
        text = f"运行正常：过去 {minutes} 分钟检测 {self._quiet_checks} 次，网络均正常"
        if self._latencies:
            average = round(sum(self._latencies) / len(self._latencies))
            text += f"（平均耗时 {average} ms）"
        self._quiet_checks = 0
        self._latencies = []
        self._quiet_since = self.clock()
        return [text]
