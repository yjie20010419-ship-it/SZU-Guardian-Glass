import time
from pathlib import Path
from .storage import ConfigStore
from .monitor import MonitorEvent


def _settle(app, seconds: float = 0.6) -> None:
    """Let page fades and sliding animations finish before a screenshot."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.qt.processEvents()
        time.sleep(0.01)


def render(destination: Path) -> None:
    from .ui import GuardianApp
    destination.mkdir(parents=True, exist_ok=True)
    app = GuardianApp(ConfigStore(destination / 'demo-config.json'), preview=True)
    app.username.setText('2024••••68')
    app.password.setText('demonstration')
    app.interval.setValue(3)
    app.active_interval = 3
    app.subtitle.setText('教学 / 办公区 · 认证强制直连')
    app.uu_status.setText('完整重启：主程序 + 后台服务（已授权）')
    app.authorize_button.setText('取消授权')
    app.check_button.setText('立即检测')
    app.stop_button.setEnabled(True)
    app._apply_event(MonitorEvent('online', '网络检测正常（界面演示）'))
    app._apply_event(MonitorEvent('waiting', '下次检测将在 3 分钟内进行'))
    app.window.show()
    _settle(app)
    for index, name in [(0, '状态页'), (1, '设置页'), (2, '记录页')]:
        app._select_page(index)
        _settle(app)
        if not app.window.grab().save(str(destination / (name + '.png'))):
            raise OSError('截图写入失败')
    app._apply_theme('#ADE5D1')
    app._select_page(0)
    _settle(app)
    app.window.grab().save(str(destination / '薄荷绿.png'))
    app.window.resize(820, 660)
    _settle(app)
    app.window.grab().save(str(destination / '小窗口.png'))
    app.timer.stop()
    app.clock_timer.stop()
    app.is_exiting = True
    app.window.close()
