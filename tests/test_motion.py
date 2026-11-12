import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import time
import unittest

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QPushButton

from szu_guardian.motion import (
    AnimatedSwitch,
    ColorTween,
    SlidingNav,
    StatusOrb,
    blend,
    surface_top_color,
)


def settle(app, seconds=0.5):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


class MotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_blend_and_top_color_follow_the_tint(self):
        self.assertEqual(blend(QColor("#000000"), QColor("#FFFFFF"), 0.5).name(), "#808080")
        mint = surface_top_color(QColor("#ADE5D1"))
        pink = surface_top_color(QColor("#F3BECD"))
        self.assertNotEqual(mint.name(), pink.name())
        self.assertGreater(mint.green(), mint.red())

    def test_switch_knob_slides_to_the_new_state(self):
        switch = AnimatedSwitch("开机启动", False)
        switch.show()
        switch.setChecked(True)
        settle(self.app, 0.4)
        self.assertAlmostEqual(switch._position, 1.0)
        self.assertTrue(switch.isChecked())
        switch.setChecked(False)
        settle(self.app, 0.4)
        self.assertAlmostEqual(switch._position, 0.0)

    def test_orb_states_and_countdown(self):
        orb = StatusOrb(92)
        orb.set_state("checking")
        self.assertEqual(orb.color().name(), orb.accent.name())
        orb.set_countdown(time.time(), time.time() + 60)
        orb.set_state("offline")
        self.assertEqual(orb.color().name().upper(), "#C0473A")
        orb.set_state("waiting")  # waiting keeps the previous state
        self.assertEqual(orb.state, "offline")
        orb.set_state("stopped")
        self.assertIsNone(orb._countdown)

    def test_orb_only_animates_while_visible(self):
        orb = StatusOrb(92)
        self.assertFalse(orb._timer.isActive())
        orb.show()
        self.assertTrue(orb._timer.isActive())
        orb.hide()
        self.assertFalse(orb._timer.isActive())

    def test_nav_pill_follows_checked_tab(self):
        nav = QFrame()
        layout = QHBoxLayout(nav)
        buttons = []
        for text in ("状态", "设置", "记录"):
            item = QPushButton(text)
            item.setCheckable(True)
            item.setAutoExclusive(True)
            item.setFixedSize(100, 30)
            layout.addWidget(item)
            buttons.append(item)
        buttons[0].setChecked(True)
        slider = SlidingNav(nav, buttons)
        nav.show()
        settle(self.app, 0.2)
        self.assertEqual(slider.pill.geometry(), buttons[0].geometry())
        buttons[2].setChecked(True)
        settle(self.app, 0.5)
        self.assertEqual(slider.pill.geometry(), buttons[2].geometry())

    def test_color_tween_reaches_target(self):
        frames = []
        tween = ColorTween(None, frames.append, duration=80)
        tween.go(QColor("#000000"), animated=False)
        tween.go(QColor("#FFFFFF"))
        settle(self.app, 0.3)
        self.assertEqual(frames[-1].name(), "#ffffff")
        self.assertGreater(len(frames), 2)


if __name__ == "__main__":
    unittest.main()
