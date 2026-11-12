from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Callable

from .models import AppConfig
from .network import NetworkClient
from .uu_remote import RestartResult

UuRestarter = Callable[[Callable[[str], None]], RestartResult]


def _connection_settings(config: AppConfig) -> tuple[object, ...]:
    return (config.username, config.password, config.zone, config.direct_mode)


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    state: str
    message: str
    level: str = "info"
    latency_ms: int | None = None
    # For "waiting" events: seconds until the next check.
    next_in_seconds: float | None = None


class MonitorController:
    WAIT_SLICE_SECONDS = 15.0
    RESUME_GAP_SECONDS = 30.0
    # After a failed check, look again sooner than the normal interval:
    # 30 s, then 60 s, then every 120 s (never later than the interval).
    RETRY_DELAYS = (30.0, 60.0, 120.0)

    @classmethod
    def next_delay(cls, config: AppConfig, failures: int) -> float:
        interval = config.interval_minutes * 60.0
        if not failures:
            return interval
        return min(interval, cls.RETRY_DELAYS[min(failures, len(cls.RETRY_DELAYS)) - 1])

    def __init__(
        self,
        event_callback: Callable[[MonitorEvent], None],
        client_factory: Callable[[], NetworkClient] = NetworkClient,
        uu_restarter: UuRestarter | None = None,
    ):
        self.event_callback = event_callback
        self.client_factory = client_factory
        # Restarts UU Remote after a reconnect.  ``None`` disables the feature,
        # so nothing outside the guardian is touched unless a caller opts in.
        self.uu_restarter = uu_restarter
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._pending_config: AppConfig | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, config: AppConfig) -> None:
        with self._lock:
            if self.running:
                self._wake_event.set()
                return
            self._stop_event.clear()
            self._wake_event.clear()
            copied = replace(config)
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(copied,),
                name="szu-network-monitor",
                daemon=True,
            )
            self._thread.start()

    def check_now(self) -> None:
        if self.running:
            self._wake_event.set()

    def update_config(self, config: AppConfig) -> None:
        """Hand new settings to the running loop and check right away."""
        with self._lock:
            self._pending_config = replace(config)
        self.check_now()

    def _take_pending_config(self) -> AppConfig | None:
        with self._lock:
            pending, self._pending_config = self._pending_config, None
        return pending

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        else:
            self.event_callback(MonitorEvent("stopped", "监控已停止"))

    def _emit(self, event: MonitorEvent) -> None:
        self.event_callback(event)

    def _run_loop(self, config: AppConfig) -> None:
        client = self.client_factory()
        self._emit(MonitorEvent("checking", "正在检测网络状态…"))
        # Set once a login has been sent (or a check crashed mid-way) and the
        # network is not back yet: the restart of UU Remote is then owed on
        # the first check that finds the network working again.
        uu_restart_owed = False
        failures = 0

        while not self._stop_event.is_set():
            self._wake_event.clear()
            pending = self._take_pending_config()
            if pending is not None:
                if _connection_settings(pending) != _connection_settings(config):
                    client = self.client_factory()
                config = pending
            try:
                result = client.ensure_connected(
                    config,
                    progress_callback=lambda message: self._emit(
                        MonitorEvent("reconnecting", message, "warning")
                    ),
                )
                for note in getattr(result, "notes", ()):
                    self._emit(MonitorEvent("note", note))
                if result.reconnected or getattr(result, "login_attempted", False):
                    uu_restart_owed = True
                if result.connected:
                    failures = 0
                    self._emit(
                        MonitorEvent(
                            "online",
                            result.message,
                            "success",
                            result.latency_ms,
                        )
                    )
                    if uu_restart_owed:
                        uu_restart_owed = False
                        self._restart_uu_remote(config, result.latency_ms)
                else:
                    failures += 1
                    self._emit(MonitorEvent("offline", result.message, "warning"))
                    # A sleep/wake cycle or DHCP renewal can invalidate the
                    # bound source address, DNS route, and HTTP sessions.
                    client = self.client_factory()
            except Exception as exc:
                failures += 1
                uu_restart_owed = True
                self._emit(
                    MonitorEvent(
                        "offline",
                        f"检测或重连失败：{exc}",
                        "error",
                    )
                )
                client = self.client_factory()

            if self._stop_event.is_set():
                break
            delay = self.next_delay(config, failures)
            if failures:
                text = f"{delay:g} 秒后复查（连续第 {failures} 次异常）"
            else:
                text = f"下次检测将在 {config.interval_minutes} 分钟内进行"
            self._emit(MonitorEvent("waiting", text, next_in_seconds=delay))
            wait_reason = self._wait_until_next_check(delay)
            if wait_reason == "stop" or self._stop_event.is_set():
                break
            if wait_reason == "resume":
                client = self.client_factory()
                self._emit(
                    MonitorEvent(
                        "checking",
                        "检测到系统从休眠恢复，正在重新建立直连…",
                        "warning",
                    )
                )
            else:
                self._emit(MonitorEvent("checking", "正在检测网络状态…"))
        self._emit(MonitorEvent("stopped", "监控已停止"))

    def _restart_uu_remote(
        self,
        config: AppConfig,
        latency_ms: int | None,
    ) -> None:
        """Restart UU Remote once the network is back after a reconnect."""
        if not config.restart_uu_remote or self.uu_restarter is None:
            return
        if self._stop_event.is_set():
            return
        try:
            result = self.uu_restarter(
                lambda message: self._emit(
                    MonitorEvent("restarting", message, "warning")
                )
            )
        except Exception as exc:
            self._emit(
                MonitorEvent(
                    "online",
                    f"重启 UU 远程时出错：{exc}",
                    "error",
                    latency_ms,
                )
            )
            return
        self._emit(MonitorEvent("online", result.message, result.level, latency_ms))

    def _wait_until_next_check(self, interval_seconds: float) -> str:
        """Wait for a manual check, the normal deadline, or a wake-up gap."""
        started = time.time()
        deadline = started + interval_seconds
        while not self._stop_event.is_set():
            remaining = max(0.0, deadline - time.time())
            if remaining == 0:
                return "interval"

            wait_started = time.time()
            if self._wake_event.wait(
                min(self.WAIT_SLICE_SECONDS, remaining)
            ):
                return "manual"

            # Windows may suspend the thread while the Event wait is pending.
            # A wall-clock gap larger than one wait slice indicates that the
            # machine likely resumed; refresh the network client immediately.
            if time.time() - wait_started >= self.RESUME_GAP_SECONDS:
                return "resume"
        return "stop"
