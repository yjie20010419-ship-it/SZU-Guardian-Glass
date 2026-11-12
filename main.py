from __future__ import annotations

import argparse
import ctypes
import sys

from szu_guardian.storage import ConfigStore
from szu_guardian.ui import GuardianApp


def enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SZU 校园网自动重连工具")
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="由 Windows 开机启动项调用；窗口将最小化并自动开始监控",
    )
    parser.add_argument("--ui-smoke", metavar="DIRECTORY", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.ui_smoke:
        from pathlib import Path
        from szu_guardian.ui_smoke import render
        render(Path(args.ui_smoke))
        return 0
    enable_windows_dpi_awareness()
    from PySide6.QtWidgets import QApplication

    from szu_guardian import cache_dir
    from szu_guardian.single_instance import MESSAGE_AUTOSTART, MESSAGE_SHOW, SingleInstance

    QApplication.instance() or QApplication(sys.argv[:1])
    guard = SingleInstance()
    if guard.notify_existing(MESSAGE_AUTOSTART if args.autostart else MESSAGE_SHOW):
        return 0  # Already running: the existing window was brought forward.

    startup_notes = []
    try:
        cache_dir.ensure_registered()
        removed, freed = cache_dir.cleanup_stale(
            cache_dir.default_search_dirs(), cache_dir.running_from()
        )
        if removed:
            startup_notes.append(
                f"已清理 {removed} 个残留缓存文件夹，释放 {cache_dir.format_size(freed)}"
            )
    except (OSError, ValueError) as exc:
        startup_notes.append(f"缓存文件夹检查失败：{exc}")

    app = GuardianApp(ConfigStore(), launched_by_autostart=args.autostart)
    guard.listen(lambda message: app.handle_instance_message(message))
    for note in startup_notes:
        app.note(note)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
