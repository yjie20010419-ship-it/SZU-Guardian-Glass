"""Wake the monitor as soon as the PC's IP addresses change.

``NotifyAddrChange`` blocks until the IPv4 address table changes: a cable
plugged in, Wi-Fi reconnecting, DHCP renewing, a VPN adapter coming up.
Changes usually arrive in bursts, so the callback fires once things have
been quiet for ``settle_seconds``.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable


class AddressChangeWatcher:
    def __init__(
        self,
        on_change: Callable[[], None],
        settle_seconds: float = 4.0,
        wait_for_change: Callable[[], bool] | None = None,
    ):
        self.on_change = on_change
        self.settle_seconds = settle_seconds
        self._wait_for_change = wait_for_change or _notify_addr_change
        self._stop = threading.Event()
        self._last_change = 0.0
        self._pending = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> bool:
        if sys.platform != "win32" and self._wait_for_change is _notify_addr_change:
            return False
        for target, name in ((self._watch, "addr-watch"), (self._debounce, "addr-debounce")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return True

    def stop(self) -> None:
        self._stop.set()
        self._pending.set()

    def _watch(self) -> None:
        while not self._stop.is_set():
            if self._wait_for_change():
                self._last_change = time.monotonic()
                self._pending.set()
            else:
                self._stop.wait(30)  # API failed; try again later.

    def _debounce(self) -> None:
        while not self._stop.is_set():
            self._pending.wait()
            if self._stop.is_set():
                return
            while time.monotonic() - self._last_change < self.settle_seconds:
                if self._stop.wait(0.5):
                    return
            self._pending.clear()
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 - never kill the watcher thread
                pass


def _notify_addr_change() -> bool:
    try:
        import ctypes

        return ctypes.windll.iphlpapi.NotifyAddrChange(None, None) == 0
    except (AttributeError, OSError):
        return False
