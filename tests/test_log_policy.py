import unittest

from szu_guardian.log_policy import RoutineLogFilter


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RoutineLogFilterTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.filter = RoutineLogFilter(clock=self.clock, heartbeat_seconds=3600)

    def cycle(self, latency=20):
        lines = []
        lines += self.filter.lines_to_write("checking", "正在检测网络状态…")
        lines += self.filter.lines_to_write(
            "online", "网络连接正常（校园网直连）", "success", latency
        )
        lines += self.filter.lines_to_write("waiting", "下次检测将在 2 分钟内进行")
        self.clock.now += 120
        return lines

    def test_first_online_is_written_then_routine_checks_are_quiet(self):
        self.assertEqual(self.cycle(), ["网络连接正常（校园网直连）"])
        self.assertEqual(self.cycle(), [])
        self.assertEqual(self.cycle(), [])

    def test_hourly_heartbeat_summarises_quiet_checks(self):
        self.cycle()
        written = []
        for _ in range(30):
            written += self.cycle(latency=30)
        self.assertEqual(len(written), 1)
        self.assertIn("运行正常", written[0])
        self.assertIn("网络均正常", written[0])
        self.assertIn("平均耗时 30 ms", written[0])

    def test_problems_are_written_immediately_with_pending_summary(self):
        self.cycle()
        self.cycle()
        self.cycle()
        lines = self.filter.lines_to_write(
            "reconnecting", "检测到校园网认证已失效", "warning"
        )
        self.assertEqual(lines, ["检测到校园网认证已失效"])
        lines = self.filter.lines_to_write("offline", "外网仍不可用", "warning")
        self.assertIn("检测 2 次", lines[0])
        self.assertEqual(lines[-1], "外网仍不可用")
        # Recovery is written even though it is an online event.
        lines = self.filter.lines_to_write(
            "online", "网络连接正常（校园网直连）", "success", 20
        )
        self.assertEqual(lines, ["网络连接正常（校园网直连）"])

    def test_wake_from_sleep_and_notes_are_kept(self):
        self.assertEqual(
            self.filter.lines_to_write(
                "checking", "检测到系统从休眠恢复，正在重新建立直连…", "warning"
            ),
            ["检测到系统从休眠恢复，正在重新建立直连…"],
        )
        self.assertEqual(
            self.filter.lines_to_write("note", "账号在线设备 2 台"),
            ["账号在线设备 2 台"],
        )


if __name__ == "__main__":
    unittest.main()
