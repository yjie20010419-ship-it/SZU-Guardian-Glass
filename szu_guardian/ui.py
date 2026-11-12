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
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout, QLineEdit, QComboBox, QSpinBox, QStackedWidget, QScrollArea, QTextEdit, QColorDialog, QFileDialog, QMessageBox, QSystemTrayIcon, QMenu, QButtonGroup
from . import __version__, cache_dir, uu_remote
from .appearance import ThemeStore, normalize_color
from .models import AppConfig, ZONE_AUTO, ZONE_OFFICE, ZONE_DORMITORY
from .monitor import MonitorController, MonitorEvent
from .storage import ConfigStore
from .startup import is_autostart_enabled, set_autostart
from .local_log import LocalLog, LOCATION_FILE, chosen_log_directory, legacy_log_directory, migrate_logs, old_log_locations, save_log_location
from . import data_dir as data_location
from .data_dir import legacy_data_dir, remove_if_empty
from .log_policy import RoutineLogFilter
from .runtime_environment import sanitize_external_environment
from .net_watch import AddressChangeWatcher
from .outage_stats import compute_day, describe, format_duration
from .motion import AnimatedSwitch, HoverLift, fade_in, set_title_bar_color
from .glass import (THEME, BAD_COLOR, OK_COLOR, WARN_COLOR, Backdrop, ColorChip, GlassCard, GradientLabel, HueSlider, Island, NavItem, PointerLight,
                    ProgressRing, StatTile, StatusPill, Ticker, accent_for_hue, hue_of)

ZONES = [('教学 / 办公区（实验室）', ZONE_OFFICE), ('宿舍区', ZONE_DORMITORY), ('自动尝试（实验性）', ZONE_AUTO)]
ZONE_SHORT = {ZONE_OFFICE: '教学/办公区', ZONE_DORMITORY: '宿舍区', ZONE_AUTO: '自动选择区域'}
THEME_PRESETS = [('小清新蓝', 207), ('薄荷绿', 165), ('湖水青', 188), ('薰衣草', 255), ('樱花粉', 335), ('暖杏', 28)]
ASSETS = Path(__file__).resolve().parent / 'assets'
PAGE_STATUS, PAGE_RECORDS, PAGE_SETTINGS = 0, 1, 2
BUSY_STATES = ('checking', 'reconnecting', 'restarting')
BAD_WORDS = ('失效', '失败', '异常', '不可用', '断开', '错误')
GOOD_WORDS = ('恢复', '正常', '成功', '已重连', '已连接', '上线')


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


def card() -> tuple[GlassCard, QVBoxLayout]:
    frame = GlassCard()
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 18, 22, 18)
    layout.setSpacing(10)
    return (frame, layout)


Switch = AnimatedSwitch


def clock(seconds: float) -> str:
    seconds = max(0, math.ceil(seconds))
    return f'{seconds // 60}:{seconds % 60:02d}'


class Shell(QWidget):
    """Central widget: animated backdrop, floating sidebar, pages and island."""

    SIDE_W = 188

    def __init__(self):
        super().__init__()
        self.backdrop = Backdrop(self)
        self.sidebar: QWidget | None = None
        self.pages: QWidget | None = None
        self.island: Island | None = None

    def resizeEvent(self, event) -> None:
        w, h = self.width(), self.height()
        self.backdrop.setGeometry(0, 0, w, h)
        left = 12 + self.SIDE_W + 24
        if self.sidebar is not None:
            self.sidebar.setGeometry(12, 10, self.SIDE_W, h - 22)
        if self.pages is not None:
            self.pages.setGeometry(left, 86, w - left - 28, h - 86 - 16)
        if self.island is not None:
            self.island.place((left + w - 28) // 2, 10)
            self.island.raise_()
        super().resizeEvent(event)


class GlassWindow(QMainWindow):

    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def showEvent(self, event) -> None:
        super().showEvent(event)
        set_title_bar_color(self, THEME.palette.bg)

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
    UU_REFRESH_MS = 120_000

    def __init__(self, store: ConfigStore, launched_by_autostart: bool=False, *, preview: bool=False):
        self.qt = QApplication.instance() or QApplication(sys.argv[:1])
        self.qt.setStyle('Fusion')
        self.qt.setLibraryPaths(self.qt.libraryPaths())
        sanitize_external_environment(getattr(sys, '_MEIPASS', None))
        if not QFontDatabase.families():
            fonts = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
            for font_name in ('msyh.ttc', 'msyhbd.ttc', 'SegUIVar.ttf'):
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
        self.data_dir = store.path.parent
        # Preview/tests keep everything inside their temporary data folder.
        legacy_logs = legacy_log_directory(self.data_dir if preview else None)
        log_dir = legacy_logs if preview else chosen_log_directory(self.data_dir)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_dir = legacy_logs  # e.g. the chosen drive is gone
        # Logs left at an earlier version's default location follow along.
        self.migrated_logs = 0
        for old in ([legacy_logs] if preview else old_log_locations()):
            if not cache_dir.same_path(old, log_dir):
                self.migrated_logs += migrate_logs(old, log_dir)
        if not preview:
            cache_dir.remove_old_layout('Logs')
            if legacy_data_dir() != self.data_dir:
                remove_if_empty(legacy_data_dir())  # nothing left on C: once moved
        self.local_log = LocalLog(log_dir)
        self.log_filter = RoutineLogFilter()
        self.log_entries = deque()
        self.is_exiting = False
        self.tray_notice_shown = False
        self.busy = False
        self.manual_thread = None
        self.next_check = None
        self.wait_total = None
        self.last_network_state = 'stopped'
        self.ever_started = False
        self.uu_connections: int | None = None
        self.uu_authorized = False
        self.window = GlassWindow(self)
        self.window.setWindowTitle('校园网守护')
        self.window.resize(1000, 640)
        self.window.setMinimumSize(920, 600)
        self.icon = QIcon(str(ASSETS / 'app.ico'))
        self.window.setWindowIcon(self.icon)
        self.shell = Shell()
        self.window.setCentralWidget(self.shell)
        self.pointer_light = PointerLight(self.window)
        self._build_ui()
        self._build_tray()
        self._apply_theme(self.saved_color, animated=False)
        THEME.changed.connect(self._theme_frame)
        self.timer = QTimer(self.window)
        self.timer.timeout.connect(self._poll)
        self.timer.start(150)
        self.clock_timer = QTimer(self.window)
        self.clock_timer.timeout.connect(self._update_countdown)
        self.clock_timer.start(1000)
        self.uu_timer = QTimer(self.window)
        self.uu_timer.timeout.connect(self._refresh_uu_connections)
        # Show what was logged earlier today, so a restart keeps the history.
        self.log_entries.extend(self.local_log.read_day(limit=self.LOG_VIEW_LIMIT))
        self._append_log('程序已启动')
        if self.migrated_logs:
            self._append_log(f'已把 {self.migrated_logs} 个日志文件移到 {self.local_log.directory}')
        self._refresh_stats()
        self._set_idle_state()
        self.last_network_trigger = 0.0
        self.net_watch = AddressChangeWatcher(lambda: self.events.put(('netchange', None)))
        if not preview:
            self.net_watch.start()
            self._refresh_uu_status_async()
            self._refresh_uu_connections()
            self.uu_timer.start(self.UU_REFRESH_MS)
            QTimer.singleShot(300, self._initial_start)

    # ------------------------------------------------------------------ layout
    def _build_ui(self) -> None:
        self._build_sidebar()
        self.pages = QStackedWidget(self.shell)
        self.shell.pages = self.pages
        self._build_status()
        self._build_logs()
        self._build_settings()
        self.island = Island(self.shell, self._check_now, self._stop_monitoring)
        self.shell.island = self.island
        for key, page in (('1', PAGE_STATUS), ('2', PAGE_RECORDS), ('3', PAGE_SETTINGS)):
            QShortcut(QKeySequence(key), self.window, activated=lambda p=page: self._select_page(p))
        QShortcut(QKeySequence('R'), self.window, activated=self._check_now)
        QShortcut(QKeySequence('P'), self.window, activated=self._stop_monitoring)
        QShortcut(QKeySequence('U'), self.window, activated=self._restart_uu_now)

    def _build_sidebar(self) -> None:
        side = GlassCard(self.shell)
        self.shell.sidebar = side
        layout = QVBoxLayout(side)
        layout.setContentsMargins(10, 14, 10, 12)
        layout.setSpacing(3)
        head = QHBoxLayout()
        head.setContentsMargins(8, 2, 0, 12)
        logo = QLabel()
        logo.setPixmap(self.icon.pixmap(22, 22))
        head.addWidget(logo)
        head.addSpacing(6)
        head.addWidget(label('校园网守护', 'appTitle'))
        head.addStretch()
        layout.addLayout(head)
        self.nav_group = QButtonGroup(self.window)
        self.nav_group.setExclusive(True)
        for index, (text, key) in enumerate((('状态', '1'), ('记录', '2'), ('设置', '3'))):
            item = NavItem(text, key)
            self.nav_group.addButton(item, index)
            layout.addWidget(item)
        self.nav_group.button(PAGE_STATUS).setChecked(True)
        self.nav_group.idClicked.connect(self._select_page)
        caption = label('快捷操作', 'faint')
        caption.setContentsMargins(10, 16, 0, 6)
        layout.addWidget(caption)
        for text, key, callback in (('立即检测', 'R', self._check_now), ('暂停守护', 'P', self._stop_monitoring), ('重启 UU', 'U', self._restart_uu_now)):
            item = NavItem(text, key, checkable=False)
            item.clicked.connect(callback)
            layout.addWidget(item)
        layout.addStretch()
        self.notice = label('关闭窗口后仍在托盘守护', 'faint')
        self.notice.setWordWrap(True)
        self.notice.setContentsMargins(10, 0, 6, 0)
        layout.addWidget(self.notice)
        foot = QHBoxLayout()
        foot.setContentsMargins(10, 4, 6, 0)
        foot.addWidget(label(f'v{__version__}', 'faint'))
        foot.addStretch()
        quit_link = QPushButton('退出程序')
        quit_link.setProperty('link', True)
        quit_link.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_link.clicked.connect(self.exit_application)
        foot.addWidget(quit_link)
        layout.addLayout(foot)

    def _page(self, scroll: bool=False) -> QVBoxLayout:
        content = QWidget()
        content.setObjectName('page')
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 4 if scroll else 0, 6)
        layout.setSpacing(12)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            area.setWidget(content)
            self.pages.addWidget(area)
        else:
            self.pages.addWidget(content)
        return layout

    def _build_status(self) -> None:
        layout = self._page()
        layout.addStretch(2)
        self.ring = ProgressRing(250, 10)
        inner = QVBoxLayout(self.ring)
        inner.setContentsMargins(24, 0, 24, 0)
        inner.setSpacing(4)
        inner.addStretch()
        self.status_badge = StatusPill('尚未开始')
        inner.addWidget(self.status_badge, 0, Qt.AlignmentFlag.AlignHCenter)
        self.status_title = GradientLabel('未开始', 38)
        inner.addWidget(self.status_title, 0, Qt.AlignmentFlag.AlignHCenter)
        self.countdown = label('点“开始守护”自动检测网络', 'lead')
        self.countdown.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inner.addWidget(self.countdown, 0, Qt.AlignmentFlag.AlignHCenter)
        inner.addStretch()
        layout.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(6)
        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        tiles.addStretch()
        self.rate_tile = StatTile('rate', '—', '今日在线率')
        self.drop_tile = StatTile('drop', '0 次', '今日掉线')
        self.uu_tile = StatTile('uu', '—', 'UU 远程连接')
        for tile in (self.rate_tile, self.drop_tile, self.uu_tile):
            tiles.addWidget(tile)
        tiles.addStretch()
        layout.addLayout(tiles)
        layout.addSpacing(6)
        row = QHBoxLayout()
        row.addStretch()
        self.check_button = button('开始守护', self._check_now, True)
        self.check_button.setMinimumWidth(120)
        self.stop_button = button('暂停', self._stop_monitoring)
        self.stop_button.setMinimumWidth(84)
        self.stop_button.setEnabled(False)
        row.addWidget(self.check_button)
        row.addSpacing(8)
        row.addWidget(self.stop_button)
        row.addStretch()
        layout.addLayout(row)
        self.hover_lifts = [HoverLift(self.check_button), HoverLift(self.stop_button)]
        self.ticker = Ticker()
        layout.addWidget(self.ticker)
        layout.addStretch(3)

    def _build_logs(self) -> None:
        layout = self._page()
        head = QHBoxLayout()
        self.records_title = GradientLabel('运行记录', 34, align=Qt.AlignmentFlag.AlignLeft)
        head.addWidget(self.records_title)
        head.addStretch()
        head.addWidget(button('打开日志目录', self._open_logs))
        layout.addLayout(head)
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
        logs, body = card()
        body.addWidget(label('今天的记录（含重启前，最新的在最上面）· 完整日志按天保留 7 天', 'muted'))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFrameShape(QFrame.Shape.NoFrame)
        self.log_text.viewport().setAutoFillBackground(False)
        body.addWidget(self.log_text, 1)
        layout.addWidget(logs, 1)

    def _build_settings(self) -> None:
        layout = self._page(scroll=True)
        layout.addWidget(GradientLabel('设置', 34, align=Qt.AlignmentFlag.AlignLeft))
        layout.addWidget(label('主题色和守护选项立即生效；账号密码改完点“保存”。', 'muted'))
        layout.addSpacing(2)
        # -- campus network account
        network, form = card()
        form.addWidget(label('校园网', 'sectionTitle'))
        form.addWidget(label('密码用 Windows 加密保存，只有这台电脑上的当前账号能解开', 'muted'))
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        self.username = QLineEdit(self.config.username)
        self.username.setPlaceholderText('校园网账号（学号/工号）')
        self.password = QLineEdit(self.config.password)
        self.password.setPlaceholderText('统一身份认证密码')
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        password_row = QHBoxLayout()
        password_row.setContentsMargins(0, 0, 0, 0)
        password_row.addWidget(self.password)
        self.reveal = button('显示', self._toggle_password)
        self.reveal.setFixedWidth(64)
        password_row.addWidget(self.reveal)
        self.zone = QComboBox()
        for name, value in ZONES:
            self.zone.addItem(name, value)
        self.zone.setCurrentIndex(max(0, self.zone.findData(self.config.zone)))
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setSuffix(' 分钟')
        self.interval.setValue(self.config.interval_minutes)
        # The spin box's inner editor ignores the style sheet padding; match the other fields.
        self.interval.findChild(QLineEdit).setTextMargins(12, 0, 0, 0)
        for row, (name, widget) in enumerate([('账号', self.username), ('密码', None), ('区域', self.zone), ('检测间隔', self.interval)]):
            grid.addWidget(label(name, 'formLabel'), row, 0)
            if widget is not None:
                grid.addWidget(widget, row, 1)
            else:
                grid.addLayout(password_row, row, 1)
        grid.setColumnMinimumWidth(0, 70)
        grid.setColumnStretch(1, 1)
        form.addLayout(grid)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button('保存', self._save_settings, True))
        form.addLayout(row)
        layout.addWidget(network)
        # -- theme colour
        palette, body = card()
        body.addWidget(label('主题色', 'sectionTitle'))
        body.addWidget(label('整个界面的光斑、按钮、标题渐变和光晕都会跟着变', 'muted'))
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.swatches: list[ColorChip] = []
        for name, hue in THEME_PRESETS:
            chip = ColorChip(name, hue)
            chip.clicked.connect(lambda checked=False, h=hue: self._choose_hue(h))
            self.swatches.append(chip)
            chips.addWidget(chip)
        chips.addStretch()
        body.addLayout(chips)
        row = QHBoxLayout()
        row.addWidget(label('自定义', 'formLabel'))
        self.hue_slider = HueSlider()
        self.hue_slider.hueChanged.connect(self._choose_hue)
        row.addWidget(self.hue_slider, 1)
        self.hue_value = label('', 'muted')
        self.hue_value.setFixedWidth(38)
        row.addWidget(self.hue_value)
        row.addWidget(button('更多颜色…', self._pick_color))
        body.addLayout(row)
        self.appearance_notice = label('', 'muted')
        self.appearance_notice.hide()  # only shown when saving fails
        body.addWidget(self.appearance_notice)
        layout.addWidget(palette)
        # -- guard options
        options, body = card()
        body.addWidget(label('守护选项', 'sectionTitle'))
        self.start_on_launch = Switch('启动后自动守护', self.config.start_on_launch)
        self.restart_uu = Switch('重连后重启 UU 远程', self.config.restart_uu_remote)
        self.direct_mode = Switch('检测与认证强制直连（绕过 Clash 等代理）', self.config.direct_mode)
        self.autostart = Switch('开机启动', self.config.autostart if self.preview else is_autostart_enabled() or self.config.autostart)
        for switch in (self.start_on_launch, self.restart_uu, self.direct_mode, self.autostart):
            switch.toggled.connect(self._autosave)
            body.addWidget(switch)
        layout.addWidget(options)
        # -- UU remote
        uu, body = card()
        body.addWidget(label('UU 远程', 'sectionTitle'))
        self.uu_status = label('正在读取授权状态…', 'muted')
        self.uu_status.setWordWrap(True)
        body.addWidget(self.uu_status)
        row = QHBoxLayout()
        self.authorize_button = button('授权完整重启', self._toggle_authorization)
        self.uu_button = button('立即重启', self._restart_uu_now)
        row.addWidget(self.authorize_button)
        row.addWidget(self.uu_button)
        row.addStretch()
        body.addLayout(row)
        layout.addWidget(uu)
        layout.addWidget(self._build_data_dir_card())
        layout.addWidget(self._build_log_dir_card())
        layout.addWidget(self._build_cache_card())
        layout.addStretch()

    def _build_data_dir_card(self) -> QFrame:
        frame, body = card()
        body.addWidget(label('配置文件夹', 'sectionTitle'))
        body.addWidget(label('账号（密码已加密）、外观和日志位置等设置保存在这里。修改后立即生效，现有设置会一起移过去。', 'muted'))
        row = QHBoxLayout()
        self.data_dir_input = QLineEdit(str(self.data_dir))
        row.addWidget(self.data_dir_input, 1)
        row.addWidget(button('浏览…', lambda: self._browse_into(self.data_dir_input, '选择配置文件夹')))
        row.addWidget(button('打开', lambda: self._open_folder(self.data_dir, self.data_dir_notice)))
        row.addWidget(button('应用', self._apply_data_dir, True))
        body.addLayout(row)
        self.data_dir_notice = label(f'当前位置：{self.data_dir}', 'muted')
        self.data_dir_notice.setWordWrap(True)
        body.addWidget(self.data_dir_notice)
        return frame

    def _open_folder(self, path: Path, notice: QLabel) -> None:
        if path.is_dir() and os.name == 'nt':
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            notice.setText('文件夹不存在')

    def _apply_data_dir(self) -> None:
        old = self.data_dir
        try:
            if self.preview:
                path = cache_dir.validate_cache_dir(self.data_dir_input.text())
                moved = data_location.migrate_data(old, path)
            else:
                path, moved = data_location.set_data_dir(self.data_dir_input.text(), old)
        except (ValueError, OSError) as exc:
            self.data_dir_notice.setText(str(exc))
            return
        self.data_dir = path
        self.store.path = path / 'config.json'
        self.theme_store.path = path / 'appearance.json'
        # Without an explicit log folder the logs may live inside the settings folder.
        if not (path / LOCATION_FILE).exists() and self.local_log.directory == old / 'logs':
            self.local_log.move_to(path / 'logs')
            self.log_dir_input.setText(str(self.local_log.directory))
        self.data_dir_input.setText(str(path))
        text = f'配置文件夹已改为 {path}' + (f'，已移入 {len(moved)} 个文件' if moved else '')
        self.data_dir_notice.setText(text)
        self._append_log(text)

    def _build_log_dir_card(self) -> QFrame:
        frame, body = card()
        body.addWidget(label('日志文件夹', 'sectionTitle'))
        body.addWidget(label('运行日志按天保存在这里，保留 7 天。修改后立即生效，已有日志会一起移过去。', 'muted'))
        row = QHBoxLayout()
        self.log_dir_input = QLineEdit(str(self.local_log.directory))
        row.addWidget(self.log_dir_input, 1)
        row.addWidget(button('浏览…', lambda: self._browse_into(self.log_dir_input, '选择日志文件夹')))
        row.addWidget(button('打开', self._open_logs))
        row.addWidget(button('应用', self._apply_log_dir, True))
        body.addLayout(row)
        self.log_dir_notice = label(f'当前位置：{self.local_log.directory}', 'muted')
        self.log_dir_notice.setWordWrap(True)
        body.addWidget(self.log_dir_notice)
        return frame

    def _browse_into(self, field: QLineEdit, title: str) -> None:
        chosen = QFileDialog.getExistingDirectory(self.window, title, field.text())
        if chosen:
            field.setText(str(Path(chosen)))

    def _apply_log_dir(self) -> None:
        try:
            path = cache_dir.validate_cache_dir(self.log_dir_input.text())
            moved = self.local_log.move_to(path)
            save_log_location(self.data_dir, path)
        except (ValueError, OSError) as exc:
            self.log_dir_notice.setText(str(exc))
            return
        self.log_dir_input.setText(str(path))
        text = f'日志文件夹已改为 {path}' + (f'，已移入 {moved} 个日志文件' if moved else '')
        self.log_dir_notice.setText(text)
        self._append_log(text)

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
        if index == PAGE_RECORDS:
            self._refresh_stats()

    # ------------------------------------------------------------------ theme
    def _stylesheet(self) -> str:
        pal = THEME.palette

        def rgba(c: QColor) -> str:
            return f'rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})'
        css = (ASSETS / 'style.qss').read_text(encoding='utf-8-sig')
        for key, value in (('ACCENT2', pal.a2.name()), ('ACCENT', pal.a.name()), ('DEEP', pal.deep.name()), ('MUTE', pal.mute.name()), ('FAINT', pal.faint.name()),
                           ('TINT2', rgba(pal.tint2)), ('TINT', rgba(pal.tint)), ('TEXT', pal.text.name()),
                           ('CHEVRON', (ASSETS / 'chevron.svg').as_posix()), ('UPARROW', (ASSETS / 'up.svg').as_posix())):
            css = css.replace(key, value)
        return css

    def _apply_theme(self, color: str, animated: bool=True, hue: int | None=None) -> None:
        color = normalize_color(color)
        self.current_color = color
        hue = hue_of(color) if hue is None else int(hue) % 360
        THEME.set_hue(hue)
        self.window.setStyleSheet(self._stylesheet())
        for switch in self.window.findChildren(AnimatedSwitch):
            switch.set_accent(THEME.palette.a)
        for lift in getattr(self, 'hover_lifts', []):
            lift.set_color(THEME.palette.a.darker(130))
        for chip in self.swatches:
            chip.selected = abs((chip.hue - hue + 180) % 360 - 180) <= 2  # hex rounding
            chip.update()
        self.hue_slider.set_hue(hue)
        self.hue_value.setText(f'{hue}°')
        if self.window.isVisible():
            set_title_bar_color(self.window, THEME.palette.bg)

    def _theme_frame(self) -> None:
        """Keep the native title bar in step while the page fades to/from red."""
        if self.window.isVisible():
            set_title_bar_color(self.window, THEME.palette.bg)

    def _choose_hue(self, hue: int) -> None:
        self._apply_theme(accent_for_hue(hue), hue=hue)
        self._save_theme()

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self.current_color), self.window, '自定义主题颜色')
        if color.isValid():
            self._apply_theme(color.name())
            self._save_theme()

    def _save_theme(self) -> None:
        try:
            self.theme_store.save(self.current_color)
            self.saved_color = self.current_color
            self.appearance_notice.hide()
        except OSError as exc:
            self.appearance_notice.setText(f'无法保存主题色：{exc}')
            self.appearance_notice.show()

    # ------------------------------------------------------------------ settings
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
            self._refresh_island()
            return True
        except (ValueError, OSError) as exc:
            self._error('无法保存', str(exc))
            return False

    def _autosave(self, *_args) -> None:
        """Switches apply at once; without an account yet they wait for “保存”."""
        try:
            self._read_form()
        except ValueError:
            self.notice.setText('填写账号密码并保存后，选项一起生效')
            return
        self._save_settings(quiet=True)

    def _toggle_password(self) -> None:
        visible = self.password.echoMode() == QLineEdit.EchoMode.Password
        self.password.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.reveal.setText('隐藏' if visible else '显示')

    # ------------------------------------------------------------------ actions
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
            self._set_running_buttons(True)
            self._append_log(f'开始监控，间隔 {self.active_interval} 分钟')

    def _stop_monitoring(self) -> None:
        if not self.monitor.running:
            return
        self.monitor.stop()
        self.stop_button.setEnabled(False)
        self.next_check = None
        self.countdown.setText('正在停止，请稍候…')

    def _set_running_buttons(self, running: bool) -> None:
        self.check_button.setText('立即检测' if running else '开始守护')
        self.island.check_button.setText('立即检测' if running else '开始守护')
        self.stop_button.setEnabled(running)
        self.island.pause_button.setEnabled(running)

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

    def _refresh_uu_connections(self) -> None:
        """Count UU's server connections in the background (netstat is slow)."""
        if self.preview or self.is_exiting:
            return

        def work() -> None:
            try:
                self.events.put(('uu-conn', uu_remote.online_connections()))
            except Exception:
                self.events.put(('uu-conn', None))
        threading.Thread(target=work, daemon=True, name='guardian-uu-count').start()

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

    # ------------------------------------------------------------------ status
    def _refresh_stats(self) -> None:
        today = dt.date.today()
        days = [today - dt.timedelta(days=offset) for offset in range(7)]
        results = [compute_day(day, self.local_log.read_entries(day)) for day in days]
        self.stats_today.setText(describe(results[0], '今天'))
        week = [f"{item.day:%m-%d} {item.count} 次 / {format_duration(item.total_seconds)}" for item in results[1:] if item.count]
        total = sum(item.count for item in results)
        self.stats_week.setText(('近 7 天共掉线 %d 次。' % total) + ('之前几天：' + '；'.join(week) if week else '之前 6 天都没有掉线记录。'))
        day = results[0]
        midnight = dt.datetime.combine(today, dt.time())
        elapsed = max(60.0, (dt.datetime.now() - midnight).total_seconds())
        rate = max(0.0, 100.0 * (1 - day.total_seconds / elapsed))
        self.rate_tile.set(f'{rate:.1f}%' if rate < 99.95 else '100%')
        self.drop_tile.set(f'{day.count} 次', f'今日掉线 · 共 {format_duration(day.total_seconds)}' if day.count else '今日掉线')

    def _uu_text(self) -> str:
        if self.uu_connections is None:
            return 'UU 状态未知'
        return f'UU 在线 {self.uu_connections} 条连接' if self.uu_connections else 'UU 未连上服务器'

    def _zone_text(self) -> str:
        return ZONE_SHORT.get(self.config.zone, '教学/办公区')

    def _refresh_island(self) -> None:
        state = self.last_network_state
        alert = state == 'offline'
        status = {'online': '在线 · 守护中', 'offline': '已断开 · 重连中'}.get(state, '处理中')
        if state == 'stopped':
            status = '已暂停' if self.ever_started else '未开始'
        subtitle = f"{self._zone_text()} · {'强制直连' if self.config.direct_mode else '常规连接'} · {self._uu_text()}"
        title = self.island_title if hasattr(self, 'island_title') else '校园网守护'
        self.island.set_state(status=status, title=title, subtitle=subtitle, alert=alert)

    def _set_idle_state(self) -> None:
        self.island_title = '尚未开始守护' if not self.config.username else '守护已暂停'
        self.status_badge.set_dot(QColor('#9AA8B8'))
        self._set_running_buttons(False)
        self._refresh_island()

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
                self.uu_authorized = authorized
                self.uu_status.setText(status)
                self.authorize_button.setText('取消授权' if authorized else '授权完整重启')
            elif kind == 'uu-conn':
                self.uu_connections = value
                self.uu_tile.set('—' if value is None else f'{value} 条', 'UU 远程连接' if value else ('未连上服务器' if value == 0 else 'UU 远程未运行'))
                self._refresh_island()
            elif kind == 'uu-result':
                self._append_log(value.message, value.level)
                QTimer.singleShot(5000, self._refresh_uu_connections)
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
        short = {'checking': '检测中', 'reconnecting': '重连中', 'restarting': '重启 UU', 'online': '已连接', 'offline': '已断开', 'stopped': '已暂停'}
        # Routine lines stay in the on-screen activity list but are folded
        # into an hourly summary in the log file.
        for line in self.log_filter.lines_to_write(event.state, event.message, event.level, event.latency_ms):
            self.local_log.write(line, event.level if line == event.message else 'info')
        if event.state == 'note':
            # Diagnostic line only: record it without touching the status card.
            self._append_log(event.message, event.level, persist=False)
            return
        tray_state = 'busy' if event.state in BUSY_STATES else event.state
        if tray_state in self.TRAY_COLORS:
            self.tray.setIcon(self._tray_icon(tray_state))
        if event.state == 'waiting':
            delay = event.next_in_seconds if event.next_in_seconds is not None else getattr(self, 'active_interval', self.config.interval_minutes) * 60
            self.next_check = time.time() + delay
            self.wait_total = max(1.0, float(delay))
            self.ring.set_busy(False)
            offline = self.last_network_state == 'offline'
            self.status_badge.setText('等待重试' if offline else f'守护中 · {self._zone_text()}')
            self.status_badge.set_dot(BAD_COLOR if offline else OK_COLOR)
            self._update_countdown()
        else:
            self.next_check = None
            previous = self.last_network_state
            if event.state != 'stopped':
                self.ever_started = True
            self.status_title.setText(short.get(event.state, '守护中'))
            self.island_title = titles.get(event.state, '校园网守护')
            self.ring.set_busy(event.state in BUSY_STATES)
            if event.state in BUSY_STATES:
                self.status_badge.set_dot(WARN_COLOR)
            self.status_badge.setText({'online': '连接正常', 'offline': '连接异常', 'stopped': '已暂停'}.get(event.state, '正在处理'))
            if event.state == 'stopped':
                self.countdown.setText('点“开始守护”继续自动检测')
                self.ring.set_fraction(0.0, animated=True)
                self.status_badge.set_dot(QColor('#9AA8B8'))
            else:
                self.countdown.setText(f'检测耗时 {event.latency_ms} ms' if event.latency_ms is not None else '请稍候…')
            if event.state in ('online', 'offline', 'stopped'):
                if event.state != previous:
                    QTimer.singleShot(0, self._refresh_stats)
                    if event.state in ('online', 'offline') and previous in ('online', 'offline'):
                        self.island.pop(3600 if event.state == 'offline' else 2400)
                self.last_network_state = event.state
                self.status_title.alert = event.state == 'offline'
                self.status_title.update()
                THEME.set_down(event.state == 'offline', animated=self.window.isVisible())
                if event.state == 'online':
                    self.status_badge.set_dot(OK_COLOR)
                elif event.state == 'offline':
                    self.status_badge.set_dot(BAD_COLOR)
            if event.state == 'stopped':
                self._set_running_buttons(False)
            self._append_log(event.message, event.level, persist=False)
        self._refresh_island()
        self.tray.setToolTip('校园网守护 · ' + self.island_title)

    def _update_countdown(self) -> None:
        if self.next_check is None:
            return
        remaining = max(0.0, self.next_check - time.time())
        total = self.wait_total or max(remaining, 1.0)
        if remaining <= 0:
            self.countdown.setText('即将进行下一次检测')
        elif self.last_network_state == 'offline':
            self.countdown.setText(f'{math.ceil(remaining)} 秒后复查')
        else:
            self.countdown.setText(f'{clock(remaining)} 后再次检测')
        self.ring.set_fraction(remaining / total)
        self.island.set_countdown(clock(remaining), remaining / total)

    def _append_log(self, message: str, level: str='info', persist: bool=True) -> None:
        if persist:
            self.local_log.write(message, level)
        now = dt.datetime.now()
        self.log_entries.append((now, message))
        while self.log_entries and (self.log_entries[0][0].date() < now.date() or len(self.log_entries) > self.LOG_VIEW_LIMIT):
            self.log_entries.popleft()
        # Newest first, so the latest event is always the top line.
        self.log_text.setPlainText('\n'.join((f'{stamp:%H:%M:%S}   {text}' for stamp, text in reversed(self.log_entries))))
        self.log_text.verticalScrollBar().setValue(0)
        recent = [(stamp, text) for stamp, text in self.log_entries if not text.startswith('运行正常')][-4:]
        self.ticker.set_items([(f'{stamp:%H:%M}　{text}', self._event_color(text)) for stamp, text in reversed(recent)])

    @staticmethod
    def _event_color(text: str) -> QColor:
        if any(word in text for word in BAD_WORDS):
            return BAD_COLOR
        if any(word in text for word in GOOD_WORDS):
            return OK_COLOR
        return THEME.palette.faint

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
