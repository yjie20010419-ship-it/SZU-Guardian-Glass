from __future__ import annotations
import datetime as dt
import math
import os
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path
from PySide6 import QtSvg  # Load SVG dependency before sanitizing child-process environment.
from PySide6.QtCore import Qt, QTimer, QRectF, QEvent
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QLinearGradient, QRadialGradient, QFontDatabase
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout, QLineEdit, QComboBox, QSpinBox, QCheckBox, QStackedWidget, QScrollArea, QTextEdit, QColorDialog, QFileDialog, QMessageBox, QSystemTrayIcon, QMenu, QButtonGroup
from . import cache_dir, uu_remote
from .appearance import ThemeStore, PRESETS, DEFAULT_COLOR, normalize_color, accent_color
from .models import AppConfig, ZONE_AUTO, ZONE_OFFICE, ZONE_DORMITORY
from .monitor import MonitorController, MonitorEvent
from .storage import ConfigStore
from .startup import is_autostart_enabled, set_autostart
from .local_log import LocalLog
from .log_policy import RoutineLogFilter
from .runtime_environment import sanitize_external_environment
from .net_watch import AddressChangeWatcher
from .outage_stats import compute_day, describe, format_duration
from .motion import AnimatedSwitch, ColorTween, HoverLift, SlidingNav, StatusOrb, blend, fade_in, set_title_bar_color, surface_top_color
ZONES = [('教学 / 办公区（实验室）', ZONE_OFFICE), ('宿舍区', ZONE_DORMITORY), ('自动尝试（实验性）', ZONE_AUTO)]
ASSETS = Path(__file__).resolve().parent / 'assets'

def label(text: str, name: str='') -> QLabel:
    item = QLabel(text)
    item.setObjectName(name)
    item.setTextFormat(Qt.TextFormat.PlainText)
    return item

def button(text: str, callback, primary: bool=False) -> QPushButton:
    item = QPushButton(text)
    item.setCursor(Qt.CursorShape.PointingHandCursor)
    item.setProperty('primary', primary)
    item.clicked.connect(callback)
    return item

def card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName('glassCard')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 18, 22, 18)
    layout.setSpacing(12)
    return (frame, layout)

Switch = AnimatedSwitch

class Glyph(QWidget):

    def __init__(self, kind: str='network', size: int=80):
        super().__init__()
        self.kind = kind
        self.color = QColor('#299259')
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self.width() / 80, self.height() / 80)
        p.setPen(QPen(QColor(255, 255, 255, 220), 1))
        p.setBrush(QColor(255, 255, 255, 130))
        p.drawRoundedRect(QRectF(1, 1, 78, 78), 18, 18)
        p.setPen(QPen(self.color, 2.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.kind == 'network':
            p.drawRoundedRect(QRectF(33, 17, 14, 14), 3, 3)
            p.drawLine(40, 32, 40, 43)
            p.drawLine(21, 43, 59, 43)
            p.drawLine(21, 43, 21, 53)
            p.drawLine(59, 43, 59, 53)
            p.drawRoundedRect(QRectF(14, 53, 14, 14), 3, 3)
            p.drawRoundedRect(QRectF(52, 53, 14, 14), 3, 3)
        else:
            p.drawEllipse(QRectF(22, 22, 36, 36))
            p.drawEllipse(QRectF(33, 33, 14, 14))
            for x, y, dx, dy in [(40, 16, 0, 6), (40, 58, 0, 6), (16, 40, 6, 0), (58, 40, 6, 0)]:
                p.drawLine(x, y, x + dx, y + dy)

class GlassSurface(QWidget):

    def __init__(self):
        super().__init__()
        self.tint = QColor(DEFAULT_COLOR)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        white = QColor('white')
        # Mostly vertical so the top edge is one even colour that the
        # (tinted) Windows title bar continues seamlessly.
        g = QLinearGradient(0, 0, self.width() * 0.25, self.height())
        g.setColorAt(0, surface_top_color(self.tint))
        g.setColorAt(0.5, blend(self.tint, white, 0.30))
        g.setColorAt(1, blend(self.tint, white, 0.10))
        p.fillRect(self.rect(), g)
        for x, y, scale, alpha in [(0.15, 0.30, 0.7, 85), (0.9, 0.35, 0.5, 70), (0.45, 0.88, 0.55, 80)]:
            glow = QRadialGradient(self.width() * x, self.height() * y, self.width() * scale)
            glow.setColorAt(0, QColor(255, 255, 255, alpha))
            glow.setColorAt(1, QColor(255, 255, 255, 0))
            p.fillRect(self.rect(), glow)

class GlassWindow(QMainWindow):

    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def showEvent(self, event) -> None:
        super().showEvent(event)
        set_title_bar_color(self, surface_top_color(self.owner.surface.tint))

    def closeEvent(self, event) -> None:
        if self.owner.is_exiting:
            event.accept()
        elif self.owner.tray.isVisible():
            event.ignore()
            self.hide()
            if not self.owner.tray_notice_shown:
                self.owner.tray.showMessage('校园网守护', '已收起至系统托盘，守护继续运行。')
                self.owner.tray_notice_shown = True
        else:
            event.ignore()
            self.owner.exit_application()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized() and self.owner.tray.isVisible():
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

class GuardianApp:
    LOG_VIEW_LIMIT = 2000
    TRAY_COLORS = {'online': '#2FA866', 'offline': '#D2483A', 'busy': '#E3A23B', 'stopped': '#8C97A8'}

    def __init__(self, store: ConfigStore, launched_by_autostart: bool=False, *, preview: bool=False):
        self.qt = QApplication.instance() or QApplication(sys.argv[:1])
        self.qt.setStyle('Fusion')
        self.qt.setLibraryPaths(self.qt.libraryPaths())
        sanitize_external_environment(getattr(sys, '_MEIPASS', None))
        if not QFontDatabase.families():
            fonts = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
            for font_name in ('msyh.ttc', 'msyhbd.ttc'):
                QFontDatabase.addApplicationFont(str(fonts / font_name))
        self.qt.setFont(QFont('Microsoft YaHei UI', 10))
        self.qt.setQuitOnLastWindowClosed(False)
        self.store = store
        self.config = store.load()
        self.preview = preview
        self.launched_by_autostart = launched_by_autostart
        self.theme_store = ThemeStore(store.path.with_name('appearance.json'))
        self.saved_color = self.theme_store.load()
        self.current_color = self.saved_color
        self.events = queue.Queue()
        self.monitor = MonitorController(self.events.put, uu_restarter=uu_remote.restart_and_verify)
        self.local_log = LocalLog(store.path.parent / 'logs')
        self.log_filter = RoutineLogFilter()
        self.log_entries = deque()
        self.is_exiting = False
        self.tray_notice_shown = False
        self.busy = False
        self.manual_thread = None
        self.next_check = None
        self.last_network_state = 'stopped'
        self.window = GlassWindow(self)
        self.window.setWindowTitle('校园网守护')
        self.window.resize(1010, 810)
        self.window.setMinimumSize(820, 660)
        self.icon = QIcon(str(ASSETS / 'app.ico'))
        self.window.setWindowIcon(self.icon)
        self.surface = GlassSurface()
        self.window.setCentralWidget(self.surface)
        self.tint_tween = ColorTween(self.window, self._paint_tint)
        self._build_ui()
        self._build_tray()
        self.hover_lifts = [HoverLift(item) for item in self.window.findChildren(QPushButton) if item.objectName() != 'tab' and not item.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)]
        self._apply_theme(self.saved_color, animated=False)
        self.timer = QTimer(self.window)
        self.timer.timeout.connect(self._poll)
        self.timer.start(150)
        self.clock_timer = QTimer(self.window)
        self.clock_timer.timeout.connect(self._update_countdown)
        self.clock_timer.start(1000)
        # Show what was logged earlier today, so a restart keeps the history.
        self.log_entries.extend(self.local_log.read_day(limit=self.LOG_VIEW_LIMIT))
        self._append_log('程序已启动')
        self._refresh_stats()
        self.last_network_trigger = 0.0
        self.net_watch = AddressChangeWatcher(lambda: self.events.put(('netchange', None)))
        if not preview:
            self.net_watch.start()
            self._refresh_uu_status_async()
            QTimer.singleShot(300, self._initial_start)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self.surface)
        outer.setContentsMargins(30, 16, 30, 14)
        outer.setSpacing(12)
        row = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(self.icon.pixmap(28, 28))
        row.addWidget(logo)
        row.addWidget(label('校园网守护', 'appTitle'))
        row.addStretch()
        row.addWidget(label('SZU NETWORK', 'eyebrow'))
        outer.addLayout(row)
        row = QHBoxLayout()
        row.addStretch()
        nav = QFrame()
        nav.setObjectName('nav')
        nav.setFixedHeight(46)
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(4, 4, 4, 4)
        nav_layout.setSpacing(3)
        self.nav_group = QButtonGroup(self.window)
        self.nav_group.setExclusive(True)
        for index, text in enumerate(('状态', '设置', '记录')):
            tab = QPushButton(text)
            tab.setObjectName('tab')
            tab.setCheckable(True)
            tab.setFixedSize(112, 34)
            self.nav_group.addButton(tab, index)
            nav_layout.addWidget(tab)
        self.nav_group.button(0).setChecked(True)
        self.nav_group.idClicked.connect(self._select_page)
        self.nav_slider = SlidingNav(nav, list(self.nav_group.buttons()))
        row.addWidget(nav)
        row.addStretch()
        outer.addLayout(row)
        self.pages = QStackedWidget()
        self.pages.setObjectName('pages')
        outer.addWidget(self.pages, 1)
        self._build_status()
        self._build_appearance()
        self._build_logs()
        footer = QHBoxLayout()
        self.notice = label('关闭窗口后仍在托盘守护', 'muted')
        footer.addWidget(self.notice, 1)
        footer.addWidget(button('退出', self.exit_application))
        outer.addLayout(footer)

    def _page(self) -> QVBoxLayout:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 10, 2, 4)
        layout.setSpacing(12)
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        return layout

    def _build_status(self) -> None:
        layout = self._page()
        hero = QHBoxLayout()
        hero.setContentsMargins(20, 0, 20, 0)
        hero.addStretch()
        self.orb = StatusOrb(92)
        hero.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(8)
        self.status_title = label('准备连接校园网', 'heroTitle')
        text.addWidget(self.status_title)
        self.subtitle = label('选择网络区域，开始自动守护', 'subtitle')
        text.addWidget(self.subtitle)
        row = QHBoxLayout()
        self.status_badge = label('尚未开始', 'badge')
        self.countdown = label('启动后自动检测网络', 'countdown')
        row.addWidget(self.status_badge)
        row.addWidget(self.countdown)
        row.addStretch()
        text.addLayout(row)
        hero.addSpacing(20)
        hero.addLayout(text)
        hero.addStretch()
        layout.addLayout(hero)
        row = QHBoxLayout()
        row.addStretch()
        self.check_button = button('开始守护', self._check_now, True)
        self.check_button.setMinimumWidth(170)
        self.stop_button = button('暂停守护', self._stop_monitoring)
        self.stop_button.setMinimumWidth(150)
        self.stop_button.setEnabled(False)
        row.addWidget(self.check_button)
        row.addSpacing(10)
        row.addWidget(self.stop_button)
        row.addStretch()
        layout.addLayout(row)
        panels = QHBoxLayout()
        panels.setSpacing(16)
        network, form = card()
        heading = QHBoxLayout()
        heading.addWidget(label('校园网', 'sectionTitle'))
        heading.addStretch()
        heading.addWidget(button('保存设置', self._save_settings))
        form.addLayout(heading)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        self.username = QLineEdit(self.config.username)
        self.username.setPlaceholderText('校园网账号')
        self.password = QLineEdit(self.config.password)
        self.password.setPlaceholderText('统一身份认证密码')
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        password_row = QHBoxLayout()
        password_row.setContentsMargins(0, 0, 0, 0)
        password_row.addWidget(self.password)
        self.reveal = button('显示', self._toggle_password)
        self.reveal.setFixedWidth(60)
        password_row.addWidget(self.reveal)
        self.zone = QComboBox()
        for name, value in ZONES:
            self.zone.addItem(name, value)
        self.zone.setCurrentIndex(max(0, self.zone.findData(self.config.zone)))
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setSuffix(' 分钟')
        self.interval.setValue(self.config.interval_minutes)
        for row, (name, widget) in enumerate([('账号', self.username), ('密码', None), ('区域', self.zone), ('检测间隔', self.interval)]):
            grid.addWidget(label(name), row, 0)
            if widget is not None:
                grid.addWidget(widget, row, 1)
            else:
                grid.addLayout(password_row, row, 1)
        grid.setColumnStretch(1, 1)
        form.addLayout(grid)
        panels.addWidget(network, 1)
        uu, body = card()
        body.addWidget(label('UU 远程', 'sectionTitle'))
        self.uu_status = label('正在读取授权状态…', 'muted')
        self.uu_status.setWordWrap(True)
        body.addWidget(self.uu_status)
        self.restart_uu = Switch('重连后重启 UU 远程', self.config.restart_uu_remote)
        body.addWidget(self.restart_uu)
        body.addStretch()
        self.authorize_button = button('授权完整重启', self._toggle_authorization)
        self.uu_button = button('立即重启', self._restart_uu_now)
        row = QHBoxLayout()
        row.addWidget(self.authorize_button)
        row.addWidget(self.uu_button)
        body.addLayout(row)
        panels.addWidget(uu, 1)
        layout.addLayout(panels)
        options, option_layout = card()
        option_layout.setContentsMargins(18, 10, 18, 10)
        toggles = QGridLayout()
        self.autostart = Switch('开机启动', self.config.autostart if self.preview else is_autostart_enabled() or self.config.autostart)
        self.start_on_launch = Switch('启动后自动守护', self.config.start_on_launch)
        self.direct_mode = Switch('检测与认证强制直连', self.config.direct_mode)
        for i, w in enumerate((self.autostart, self.start_on_launch, self.direct_mode)):
            toggles.addWidget(w, 0, i)
        option_layout.addLayout(toggles)
        layout.addWidget(options)
        recent, body = card()
        row = QHBoxLayout()
        row.addWidget(label('最近活动', 'sectionTitle'))
        row.addStretch()
        row.addWidget(button('查看全部', lambda: self._select_page(2)))
        body.addLayout(row)
        self.recent_lines = [label('—', 'logLine') for _ in range(2)]
        for line in self.recent_lines:
            line.setWordWrap(True)
            body.addWidget(line)
        layout.addWidget(recent)
        layout.addStretch()

    def _build_appearance(self) -> None:
        layout = self._page()
        heading = QHBoxLayout()
        self.settings_glyph = Glyph('settings', 66)
        heading.addWidget(self.settings_glyph)
        text = QVBoxLayout()
        text.addWidget(label('外观设置', 'heroTitle'))
        text.addWidget(label('选择你喜欢的颜色', 'subtitle'))
        heading.addSpacing(12)
        heading.addLayout(text)
        heading.addStretch()
        layout.addLayout(heading)
        palette, body = card()
        body.addWidget(label('主题颜色', 'sectionTitle'))
        swatches = QHBoxLayout()
        swatches.setSpacing(12)
        self.swatches = []
        for name, color in PRESETS:
            item = QPushButton(name)
            item.setMinimumHeight(110)
            item.setCursor(Qt.CursorShape.PointingHandCursor)
            item.clicked.connect(lambda checked=False, c=color: self._apply_theme(c))
            self.swatches.append((item, name, color))
            swatches.addWidget(item, 1)
        body.addLayout(swatches)
        row = QHBoxLayout()
        row.addWidget(label('自定义颜色'))
        self.hex_input = QLineEdit()
        self.hex_input.setMaximumWidth(140)
        self.hex_input.setMaxLength(7)
        self.hex_input.setPlaceholderText('#A8C7FA')
        self.hex_input.editingFinished.connect(self._apply_hex)
        row.addWidget(self.hex_input)
        row.addWidget(button('选择颜色…', self._pick_color))
        row.addStretch()
        body.addLayout(row)
        layout.addWidget(palette)
        preview, body = card()
        body.addWidget(label('效果预览', 'sectionTitle'))
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(Glyph('network', 64))
        text = QVBoxLayout()
        text.addWidget(label('校园网已连接', 'sectionTitle'))
        text.addWidget(label('下次检测将在 3 分钟内进行', 'muted'))
        row.addLayout(text)
        example = button('立即检测', lambda: None, True)
        example.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        example.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(example)
        row.addStretch()
        body.addLayout(row)
        body.addWidget(label('此区域仅预览外观，不执行网络检测。', 'muted'))
        layout.addWidget(preview)
        layout.addWidget(self._build_cache_card())
        row = QHBoxLayout()
        self.appearance_notice = label('选择颜色可即时预览，保存后下次启动生效。', 'muted')
        row.addWidget(self.appearance_notice, 1)
        row.addWidget(button('恢复默认', lambda: self._apply_theme(DEFAULT_COLOR)))
        row.addWidget(button('保存外观', self._save_theme, True))
        layout.addLayout(row)
        layout.addStretch()

    def _build_cache_card(self) -> QFrame:
        frame, body = card()
        heading = QHBoxLayout()
        heading.addWidget(label('程序缓存文件夹', 'sectionTitle'))
        heading.addStretch()
        heading.addWidget(button('清理残留', self._clean_cache))
        body.addLayout(heading)
        body.addWidget(label('程序启动时会把运行文件解压到这里，正常退出后自动删除。修改后下次启动生效。', 'muted'))
        row = QHBoxLayout()
        current = cache_dir.registered_cache_dir() if not self.preview else None
        self.cache_input = QLineEdit(str(current or cache_dir.default_cache_dir()))
        self.cache_input.setPlaceholderText(str(cache_dir.default_cache_dir()))
        row.addWidget(self.cache_input, 1)
        row.addWidget(button('浏览…', self._browse_cache))
        row.addWidget(button('打开', self._open_cache))
        row.addWidget(button('应用', self._apply_cache, True))
        body.addLayout(row)
        running = cache_dir.running_from()
        self.cache_notice = label(f'本次运行位置：{running}' if running else '本次以源码方式运行，未使用缓存文件夹', 'muted')
        self.cache_notice.setWordWrap(True)
        body.addWidget(self.cache_notice)
        return frame

    def _browse_cache(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self.window, '选择程序缓存文件夹', self.cache_input.text())
        if chosen:
            self.cache_input.setText(str(Path(chosen)))

    def _open_cache(self) -> None:
        path = Path(self.cache_input.text().strip())
        if path.is_dir() and os.name == 'nt':
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            self.cache_notice.setText('文件夹不存在，请先点“应用”创建')

    def _apply_cache(self) -> None:
        try:
            if self.preview:
                path = cache_dir.validate_cache_dir(self.cache_input.text())
            else:
                path = cache_dir.set_cache_dir(self.cache_input.text())
        except ValueError as exc:
            self.cache_notice.setText(str(exc))
            return
        self.cache_input.setText(str(path))
        self.cache_notice.setText(f'已设置为 {path}，下次启动生效')
        self._append_log(f'程序缓存文件夹已设置为 {path}（下次启动生效）')

    def _clean_cache(self) -> None:
        removed, freed = cache_dir.cleanup_stale(cache_dir.default_search_dirs(), cache_dir.running_from())
        text = f'已清理 {removed} 个残留文件夹，释放 {cache_dir.format_size(freed)}' if removed else '没有需要清理的残留文件'
        self.cache_notice.setText(text)
        if removed:
            self._append_log(text)

    def _build_logs(self) -> None:
        layout = self._page()
        row = QHBoxLayout()
        row.addWidget(label('运行记录', 'heroTitle'))
        row.addStretch()
        row.addWidget(button('打开日志目录', self._open_logs))
        layout.addLayout(row)
        stats, body = card()
        body.setSpacing(6)
        stats_head = QHBoxLayout()
        stats_head.addWidget(label('掉线统计', 'sectionTitle'))
        stats_head.addStretch()
        stats_head.addWidget(button('刷新', self._refresh_stats))
        body.addLayout(stats_head)
        self.stats_today = label('—', 'statsToday')
        self.stats_today.setWordWrap(True)
        body.addWidget(self.stats_today)
        self.stats_week = label('—', 'muted')
        self.stats_week.setWordWrap(True)
        body.addWidget(self.stats_week)
        layout.addWidget(stats)
        layout.addWidget(label('今天的记录（含重启前）· 完整日志按天保留 7 天', 'muted'))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(300)
        layout.addWidget(self.log_text, 1)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.icon, self.window)
        self.tray.setToolTip('校园网守护')
        menu = QMenu()
        menu.addAction('打开主界面', self._show)
        menu.addAction('立即检测', self._check_now)
        menu.addSeparator()
        menu.addAction('退出程序', self.exit_application)
        self.tray.setContextMenu(menu)
        self.tray_menu = menu
        self.tray.activated.connect(lambda reason: self._show() if reason in (QSystemTrayIcon.ActivationReason.DoubleClick, QSystemTrayIcon.ActivationReason.Trigger) else None)
        if not self.preview and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _select_page(self, index: int) -> None:
        changed = self.pages.currentIndex() != index
        self.pages.setCurrentIndex(index)
        self.nav_group.button(index).setChecked(True)
        if changed:
            fade_in(self.pages.currentWidget())
        if index == 2:
            self._refresh_stats()

    def _paint_tint(self, color: QColor) -> None:
        self.surface.tint = QColor(color)
        self.surface.update()
        if self.window.isVisible():
            set_title_bar_color(self.window, surface_top_color(self.surface.tint))

    def _apply_theme(self, color: str, animated: bool=True) -> None:
        color = normalize_color(color)
        self.current_color = color
        self.tint_tween.go(QColor(color), animated=animated and self.window.isVisible())
        accent = accent_color(color)
        self.orb.set_accent(QColor(accent))
        self.settings_glyph.color = QColor(accent)
        self.settings_glyph.update()
        for switch in self.window.findChildren(AnimatedSwitch):
            switch.set_accent(QColor(accent))
        for lift in getattr(self, 'hover_lifts', []):
            lift.set_color(QColor(accent).darker(160))
        self.hex_input.setText(color)
        css = (ASSETS / 'style.qss').read_text(encoding='utf-8-sig').replace('ACCENT', accent).replace('HOVER', QColor(accent).lighter(115).name()).replace('SWITCHOFF', (ASSETS / 'switch-off.svg').as_posix()).replace('SWITCHON', (ASSETS / 'switch-on.svg').as_posix()).replace('CHEVRON', (ASSETS / 'chevron.svg').as_posix()).replace('UPARROW', (ASSETS / 'up.svg').as_posix())
        self.window.setStyleSheet(css)
        for item, name, preset in self.swatches:
            selected = preset.upper() == color
            item.setText(name + ('  · 已选' if selected else ''))
            item.setStyleSheet(f"QPushButton {{background: {preset}; border: {(2 if selected else 1)}px solid {(accent if selected else '#ffffff')}; border-radius: 16px; color: #233149; font-size: 14px; padding-top: 44px;}} QPushButton:hover {{border: 2px solid {accent};}}")

            item.setFixedHeight(124)

    def _apply_hex(self) -> bool:
        try:
            self._apply_theme(self.hex_input.text())
            return True
        except ValueError as exc:
            self.appearance_notice.setText(str(exc))
            return False

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self.current_color), self.window, '自定义主题颜色')
        if color.isValid():
            self._apply_theme(color.name())

    def _save_theme(self) -> None:
        if not self._apply_hex():
            return
        try:
            self.theme_store.save(self.current_color)
            self.saved_color = self.current_color
            self.appearance_notice.setText('外观已保存，下次启动自动使用。')
        except OSError as exc:
            self._error('无法保存外观', str(exc))

    def _read_form(self) -> AppConfig:
        config = AppConfig(username=self.username.text(), password=self.password.text(), zone=self.zone.currentData(), interval_minutes=self.interval.value(), autostart=self.autostart.isChecked(), start_on_launch=self.start_on_launch.isChecked(), direct_mode=self.direct_mode.isChecked(), restart_uu_remote=self.restart_uu.isChecked())
        config.validate()
        return config

    def _save_settings(self, checked: bool=False, *, quiet: bool=False) -> bool:
        try:
            config = self._read_form()
            self.store.save(config)
            if not self.preview:
                set_autostart(config.autostart)
            self.config = config
            running = self.monitor.running
            if running:
                # Apply to the running loop at once; it re-checks with the new settings.
                self.active_interval = config.interval_minutes
                self.monitor.update_config(config)
                self.next_check = None
            if not quiet:
                self._append_log(f'设置已保存并立即生效（检测间隔 {config.interval_minutes} 分钟）' if running else '设置已安全保存')
            self.notice.setText('设置已保存并已生效' if running else '设置已保存')
            return True
        except (ValueError, OSError) as exc:
            self._error('无法保存', str(exc))
            return False

    def _check_now(self) -> None:
        if self.preview or self.is_exiting:
            return
        if self.monitor.running:
            self.monitor.check_now()
            self.next_check = None
            self.countdown.setText('正在请求立即检测…')
            self._append_log('已请求立即检测')
        elif self._save_settings(quiet=True):
            self.active_interval = self.config.interval_minutes
            self.monitor.start(self.config)
            self.check_button.setText('立即检测')
            self.stop_button.setEnabled(True)
            self.subtitle.setText(self.zone.currentText() + ' · ' + ('认证强制直连' if self.config.direct_mode else '常规连接'))
            self._append_log(f'开始监控，间隔 {self.active_interval} 分钟')

    def _stop_monitoring(self) -> None:
        self.monitor.stop()
        self.stop_button.setEnabled(False)
        self.next_check = None
        self.countdown.setText('正在停止，请稍候…')

    def _toggle_password(self) -> None:
        visible = self.password.echoMode() == QLineEdit.EchoMode.Password
        self.password.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.reveal.setText('隐藏' if visible else '显示')

    def _background(self, operation, kind: str) -> None:
        if self.busy or self.is_exiting:
            return
        self.busy = True
        self.authorize_button.setEnabled(False)
        self.uu_button.setEnabled(False)

        def work() -> None:
            try:
                self.events.put((kind, operation()))
            except Exception as exc:
                self.events.put(('error', str(exc)))
            finally:
                self.events.put(('idle', None))
        self.manual_thread = threading.Thread(target=work, daemon=False, name='guardian-uu-ui')
        self.manual_thread.start()

    def _refresh_uu_status_async(self) -> None:
        self._background(lambda: (uu_remote.describe_status(), uu_remote.elevated_task_exists()), 'uu-status')

    def _toggle_authorization(self) -> None:
        if self.preview:
            return
        if self.monitor.running:
            QMessageBox.information(self.window, '请先暂停守护', '暂停守护后可更改 UU 完整重启授权。')
            return

        def authorize():
            if uu_remote.elevated_task_exists():
                uu_remote.unregister_elevated_task()
            else:
                uu_remote.register_elevated_task()
            return (uu_remote.describe_status(), uu_remote.elevated_task_exists())
        self._background(authorize, 'uu-status')

    def _restart_uu_now(self) -> None:
        if self.preview or self.busy:
            return
        if self.monitor.running:
            QMessageBox.information(self.window, '请先暂停守护', '为避免和自动重连同时重启 UU，请先暂停守护，再手动重启。')
            return
        if QMessageBox.question(self.window, '重启 UU 远程', '将结束并重新启动 UU 远程，当前远程连接会中断。是否继续？') != QMessageBox.StandardButton.Yes:
            return
        self._background(lambda: uu_remote.restart_and_verify(lambda message: self.events.put(('log', message))), 'uu-result')

    def _refresh_stats(self) -> None:
        today = dt.date.today()
        days = [today - dt.timedelta(days=offset) for offset in range(7)]
        results = [compute_day(day, self.local_log.read_entries(day)) for day in days]
        self.stats_today.setText(describe(results[0], '今天'))
        week = [f"{item.day:%m-%d} {item.count} 次 / {format_duration(item.total_seconds)}" for item in results[1:] if item.count]
        total = sum(item.count for item in results)
        self.stats_week.setText(('近 7 天共掉线 %d 次。' % total) + ('之前几天：' + '；'.join(week) if week else '之前 6 天都没有掉线记录。'))

    NETWORK_TRIGGER_GAP = 20.0

    def _on_network_change(self) -> None:
        """The PC's addresses changed: check now instead of waiting."""
        if self.is_exiting or not self.monitor.running:
            return
        now = time.monotonic()
        if now - self.last_network_trigger < self.NETWORK_TRIGGER_GAP:
            return
        self.last_network_trigger = now
        self._append_log('检测到网卡地址变化，立即检测')
        self.monitor.check_now()
        self.next_check = None

    def _poll(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, MonitorEvent):
                self._apply_event(event)
                continue
            kind, value = event
            if kind == 'uu-status':
                status, authorized = value
                self.uu_status.setText(status)
                self.authorize_button.setText('取消授权' if authorized else '授权完整重启')
            elif kind == 'uu-result':
                self._append_log(value.message, value.level)
            elif kind == 'error':
                self._append_log('UU 操作失败：' + value, 'error')
                self.uu_status.setText('操作失败，请查看运行记录')
            elif kind == 'log':
                self._append_log(value)
            elif kind == 'netchange':
                self._on_network_change()
            elif kind == 'idle':
                self.busy = False
                self.authorize_button.setEnabled(True)
                self.uu_button.setEnabled(True)

    def _apply_event(self, event: MonitorEvent) -> None:
        titles = {'checking': '正在检测网络', 'reconnecting': '正在重新连接', 'restarting': '正在重启 UU 远程', 'online': '校园网已连接', 'offline': '校园网连接异常', 'stopped': '守护已暂停'}
        # Routine lines stay in the on-screen activity list but are folded
        # into an hourly summary in the log file.
        for line in self.log_filter.lines_to_write(event.state, event.message, event.level, event.latency_ms):
            self.local_log.write(line, event.level if line == event.message else 'info')
        if event.state == 'note':
            # Diagnostic line only: record it without touching the status card.
            self._append_log(event.message, event.level, persist=False)
            return
        self.orb.set_state(event.state)
        tray_state = 'busy' if event.state in ('checking', 'reconnecting', 'restarting') else event.state
        if tray_state in self.TRAY_COLORS:
            self.tray.setIcon(self._tray_icon(tray_state))
        if event.state == 'waiting':
            self.next_check = time.time() + getattr(self, 'active_interval', self.config.interval_minutes) * 60
            self.orb.set_countdown(time.time(), self.next_check)
            self.status_badge.setText('正在守护' if self.last_network_state != 'offline' else '等待重试')
            self._update_countdown()
        else:
            self.next_check = None
            self.status_title.setText(titles.get(event.state, '校园网守护'))
            self.status_badge.setText({'online': '连接正常', 'offline': '连接异常', 'stopped': '已暂停'}.get(event.state, '正在处理'))
            self.countdown.setText('启动后继续自动检测' if event.state == 'stopped' else f'检测耗时 {event.latency_ms} ms' if event.latency_ms is not None else '请稍候…')
            if event.state in ('online', 'offline', 'stopped'):
                if event.state != self.last_network_state:
                    QTimer.singleShot(0, self._refresh_stats)
                self.last_network_state = event.state
            self.status_badge.setStyleSheet('color:#9c3e32;background:rgba(255,205,195,170);' if event.state == 'offline' else '')
            if event.state == 'stopped':
                self.check_button.setText('开始守护')
                self.stop_button.setEnabled(False)
            self._append_log(event.message, event.level, persist=False)
        self.tray.setToolTip('校园网守护 · ' + self.status_title.text())

    def _update_countdown(self) -> None:
        if self.next_check is not None:
            remaining = max(0, self.next_check - time.time())
            self.countdown.setText(f'下次检测将在 {math.ceil(remaining / 60)} 分钟内进行' if remaining > 0 else '即将进行下一次检测')

    def _append_log(self, message: str, level: str='info', persist: bool=True) -> None:
        if persist:
            self.local_log.write(message, level)
        now = dt.datetime.now()
        self.log_entries.append((now, message))
        while self.log_entries and (self.log_entries[0][0].date() < now.date() or len(self.log_entries) > self.LOG_VIEW_LIMIT):
            self.log_entries.popleft()
        self.log_text.setPlainText('\n'.join((f'{stamp:%H:%M:%S}   {text}' for stamp, text in self.log_entries)))
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())
        for index, (stamp, text) in enumerate(reversed(list(self.log_entries)[-2:])):
            self.recent_lines[index].setText(f'{stamp:%H:%M:%S}    {text}')

    def _initial_start(self) -> None:
        if (self.launched_by_autostart or self.config.start_on_launch) and self.config.username and self.config.password:
            self._check_now()
        if self.launched_by_autostart and self.tray.isVisible():
            self.window.hide()

    def _show(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def note(self, message: str) -> None:
        self._append_log(message)

    def handle_instance_message(self, message: bytes) -> None:
        """Another launch of the EXE found this one running."""
        if message == b'autostart':
            self._append_log('检测到重复的开机启动，已忽略（守护程序已在运行）')
            return
        self._append_log('程序已在运行，已切换到当前窗口')
        self._show()

    def _tray_icon(self, state: str) -> QIcon:
        cache = self.__dict__.setdefault('_tray_icons', {})
        if state not in cache:
            pixmap = self.icon.pixmap(64, 64)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor('white'), 5))
            painter.setBrush(QColor(self.TRAY_COLORS[state]))
            painter.drawEllipse(QRectF(36, 36, 25, 25))
            painter.end()
            cache[state] = QIcon(pixmap)
        return cache[state]

    def _open_logs(self) -> None:
        try:
            self.local_log.open_directory()
        except OSError as exc:
            self._error('无法打开日志目录', str(exc))

    def _error(self, title: str, message: str) -> None:
        QMessageBox.warning(self.window, title, message)

    def exit_application(self) -> None:
        self.is_exiting = True
        self.monitor.stop()
        self.notice.setText('正在等待后台操作结束…')
        self.check_button.setEnabled(False)
        self.authorize_button.setEnabled(False)
        self.uu_button.setEnabled(False)
        self._finish_exit()

    def _finish_exit(self) -> None:
        if self.monitor.running or self.busy:
            QTimer.singleShot(200, self._finish_exit)
            return
        self.net_watch.stop()
        self.tray.hide()
        for line in self.log_filter.lines_to_write('stopped', '程序退出'):
            self.local_log.write(line)
        self.window.close()
        self.qt.quit()

    def run(self) -> None:
        if not self.launched_by_autostart or not self.tray.isVisible():
            self.window.show()
        self.qt.exec()
