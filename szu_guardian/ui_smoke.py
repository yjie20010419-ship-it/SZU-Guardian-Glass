import time
from pathlib import Path
from .storage import ConfigStore
from .monitor import MonitorEvent


def _settle(app, seconds: float = 0.8) -> None:
    """Let page fades and spring animations finish before a screenshot."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.qt.processEvents()
        time.sleep(0.01)


def render(destination: Path) -> None:
    """Render demo screenshots (fake account, example data) into ``destination``."""
    from .ui import GuardianApp
    destination.mkdir(parents=True, exist_ok=True)
    app = GuardianApp(ConfigStore(destination / 'demo-config.json'), preview=True)
    app.username.setText('2024••••68')
    app.password.setText('demonstration')
    app.interval.setValue(3)
    app.active_interval = 3
    app.uu_status.setText('完整重启：主程序 + 后台服务（已授权）')
    app.authorize_button.setText('取消授权')
    app._set_running_buttons(True)
    app.events.put(('uu-conn', 5))
    app._poll()
    app._apply_event(MonitorEvent('online', '网络检测正常（3/3 个探针通过）'))
    app._apply_event(MonitorEvent('waiting', '下次检测', next_in_seconds=180))
    app.next_check = time.time() + 161
    app._update_countdown()
    app.window.show()
    _settle(app, 1.2)
    for index, name in [(0, '状态页'), (1, '记录页'), (2, '设置页')]:
        app._select_page(index)
        _settle(app)
        if not app.window.grab().save(str(destination / (name + '.png'))):
            raise OSError('截图写入失败')
    app._select_page(0)
    app.island.expand()
    _settle(app, 1.0)
    app.window.grab().save(str(destination / '灵动岛展开.png'))
    app.island.collapse()
    app._apply_event(MonitorEvent('offline', '校园网认证已失效', 'warning'))
    app._apply_event(MonitorEvent('waiting', '重试', next_in_seconds=30))
    _settle(app, 1.2)
    app.window.grab().save(str(destination / '掉线.png'))
    app._apply_theme('#ADE5D1')
    app._apply_event(MonitorEvent('online', '恢复'))
    _settle(app, 1.2)
    app.window.grab().save(str(destination / '薄荷绿.png'))
    app.timer.stop()
    app.clock_timer.stop()
    app.is_exiting = True
    app.window.close()
