import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from szu_guardian.appearance import ThemeStore, PRESETS, normalize_color, accent_color
from szu_guardian.storage import ConfigStore
from szu_guardian.monitor import MonitorEvent
from szu_guardian.ui import GuardianApp
from PySide6.QtWidgets import QLineEdit

class AppearanceTests(unittest.TestCase):

    def test_validation_and_corrupt_file(self):
        self.assertEqual(normalize_color(' #abcdef '), '#ABCDEF')
        for value in ('red', '#123', 'url(x)', '#GG0000'):
            with self.assertRaises(ValueError):
                normalize_color(value)
        with tempfile.TemporaryDirectory() as temp:
            store = ThemeStore(Path(temp) / 'appearance.json')
            store.save('#ABCDEF')
            self.assertEqual(store.load(), '#ABCDEF')
            store.path.write_text('[]')
            self.assertEqual(store.load(), '#A8C7FA')

    def test_pastel_and_custom_buttons_have_readable_white_text(self):
        for color in [c for _, c in PRESETS] + ['#FFFFFF', '#000000', '#00FF00', '#FF0000']:
            foreground = accent_color(color)
            channels = [int(foreground[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            luminance = sum((c * w for c, w in zip(linear, [0.2126, 0.7152, 0.0722])))
            self.assertGreaterEqual(1.05 / (luminance + 0.05), 4.5, foreground)

class InterfaceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = GuardianApp(ConfigStore(Path(cls.temp.name) / 'config.json'), preview=True)
        cls.app.window.show()
        cls.app.qt.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.app.timer.stop()
        cls.app.clock_timer.stop()
        cls.app.is_exiting = True
        cls.app.window.close()
        cls.temp.cleanup()

    def test_color_controls_only_on_settings_and_persist_without_credentials(self):
        app = self.app
        app._select_page(0)
        self.assertFalse(app.hex_input.isVisible())
        app._select_page(1)
        self.assertTrue(app.hex_input.isVisible())
        app._apply_theme('#ADE5D1')
        app._save_theme()
        self.assertEqual(app.theme_store.load(), '#ADE5D1')
        self.assertFalse(app.store.path.exists())
        app._select_page(0)

    def test_live_countdown_and_offline_not_shown_as_online(self):
        app = self.app
        app.active_interval = 3
        app._apply_event(MonitorEvent('online', 'online'))
        app._apply_event(MonitorEvent('waiting', 'next'))
        self.assertIn('3 分钟', app.countdown.text())
        app.next_check = time.time() + 59
        app._update_countdown()
        self.assertIn('1 分钟', app.countdown.text())
        app._apply_event(MonitorEvent('offline', 'offline'))
        app._apply_event(MonitorEvent('waiting', 'retry'))
        self.assertEqual(app.status_title.text(), '校园网连接异常')
        self.assertEqual(app.status_badge.text(), '等待重试')
        app._apply_event(MonitorEvent('stopped', 'stop'))
        self.assertIsNone(app.next_check)

    def test_password_is_masked_by_default_and_can_toggle(self):
        app = self.app
        self.assertEqual(app.password.echoMode(), QLineEdit.EchoMode.Password)
        app._toggle_password()
        self.assertEqual(app.password.echoMode(), QLineEdit.EchoMode.Normal)
        app._toggle_password()

    def test_form_preserves_all_operational_options(self):
        app = self.app
        app.username.setText('demo')
        app.password.setText('not-a-real-password')
        app.interval.setValue(7)
        app.direct_mode.setChecked(True)
        app.restart_uu.setChecked(True)
        app.start_on_launch.setChecked(False)
        cfg = app._read_form()
        self.assertEqual(cfg.interval_minutes, 7)
        self.assertTrue(cfg.restart_uu_remote)
        self.assertTrue(cfg.direct_mode)
        self.assertFalse(cfg.start_on_launch)

    def test_uu_result_is_displayed_without_starting_real_uu(self):
        self.app.events.put(('uu-status', ('完整重启：主程序 + 后台服务（已授权）', True)))
        self.app._poll()
        self.assertEqual(self.app.authorize_button.text(), '取消授权')

    def test_saving_while_running_applies_new_interval(self):
        app = self.app
        app.username.setText('demo')
        app.password.setText('not-a-real-password')
        app.interval.setValue(2)
        with patch.object(app, 'monitor') as monitor, patch.object(app.store, 'save'):
            monitor.running = True
            self.assertTrue(app._save_settings())
            monitor.update_config.assert_called_once()
            self.assertEqual(monitor.update_config.call_args.args[0].interval_minutes, 2)
            self.assertEqual(app.active_interval, 2)
            app._apply_event(MonitorEvent('waiting', 'next'))
            self.assertIn('2 分钟', app.countdown.text())

    def test_tray_icon_follows_status_and_duplicate_launch_is_logged(self):
        app = self.app
        app._apply_event(MonitorEvent('offline', '外网不可用', 'warning'))
        self.assertEqual(app.tray.icon().cacheKey(), app._tray_icon('offline').cacheKey())
        app._apply_event(MonitorEvent('checking', '正在检测网络状态…'))
        self.assertEqual(app.tray.icon().cacheKey(), app._tray_icon('busy').cacheKey())
        app.handle_instance_message(b'autostart')
        self.assertIn('重复的开机启动', app.log_entries[-1][1])

    def test_cache_folder_setting_validates_input(self):
        app = self.app
        app.cache_input.setText('relative')
        app._apply_cache()
        self.assertIn('盘符', app.cache_notice.text())
        target = Path(self.temp.name) / 'Apps' / 'Cache' / 'SZU-Guardian'
        app.cache_input.setText(str(target))
        app._apply_cache()
        self.assertTrue(target.is_dir())
        self.assertIn('下次启动生效', app.cache_notice.text())

    def test_log_folder_setting_moves_logs_immediately(self):
        app = self.app
        original = app.local_log.directory
        target = Path(self.temp.name) / 'Apps' / 'Logs' / 'SZU-Guardian'
        try:
            app.log_dir_input.setText(str(target))
            app._apply_log_dir()
            self.assertEqual(app.local_log.directory, target)
            self.assertTrue(any(target.glob('guardian-*.log')))
            self.assertIn('已改为', app.log_dir_notice.text())
        finally:
            app.local_log.move_to(original)

    def test_settings_folder_can_be_changed_and_files_follow(self):
        app = self.app
        original = app.data_dir
        (original / 'appearance.json').write_text('{"color": "#A8C7FA"}', encoding='utf-8')
        target = Path(self.temp.name) / 'Apps' / 'Config' / 'SZU-Guardian'
        try:
            app.data_dir_input.setText(str(target))
            app._apply_data_dir()
            self.assertEqual(app.data_dir, target)
            self.assertEqual(app.store.path, target / 'config.json')
            self.assertEqual(app.theme_store.path, target / 'appearance.json')
            self.assertTrue((target / 'appearance.json').exists())
            self.assertIn('已改为', app.data_dir_notice.text())
        finally:
            app.data_dir_input.setText(str(original))
            app._apply_data_dir()

    def test_monitor_button_routes_to_existing_controller(self):
        app = self.app
        with patch.object(app, 'monitor') as monitor, patch.object(app, '_save_settings', return_value=True):
            monitor.running = False
            app.preview = False
            try:
                app._check_now()
                monitor.start.assert_called_once_with(app.config)
                monitor.running = True
                app._check_now()
                monitor.check_now.assert_called_once()
            finally:
                app.preview = True
