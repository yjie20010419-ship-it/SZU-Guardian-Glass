import datetime as dt
import threading
import time
import unittest

from szu_guardian import uu_remote
from szu_guardian.net_watch import AddressChangeWatcher
from szu_guardian.outage_stats import compute_day, describe, format_duration
from szu_guardian.uu_remote import RestartResult


def at(text):
    return dt.datetime.strptime("2026-11-12 " + text, "%Y-%m-%d %H:%M:%S")


# The 13:57-14:44 episode from the real log, trimmed.
EPISODE = [
    (at("13:52:00"), "SUCCESS", "网络连接正常（校园网直连）"),
    (at("13:57:13"), "WARNING", "检测到校园网认证已失效，正在重新认证…"),
    (at("13:57:21"), "SUCCESS", "已自动重连，网络恢复正常"),
    (at("14:07:49"), "WARNING", "检测到校园网认证已失效，正在重新认证…"),
    (at("14:08:04"), "WARNING", "认证服务器已确认请求，但外网仍不可用；请查看本地日志中的认证结果"),
    (at("14:13:04"), "WARNING", "检测到校园网认证已失效，正在重新认证…"),
    (at("14:39:19"), "SUCCESS", "已自动重连，网络恢复正常"),
    (at("14:40:00"), "INFO", "账号在线设备 2 台"),
]


class OutageStatsTests(unittest.TestCase):
    def test_episode_counts_two_outages_and_longest(self):
        stats = compute_day(dt.date(2026, 11, 12), EPISODE, now=at("23:00:00"))
        self.assertEqual(stats.count, 2)
        self.assertEqual(stats.longest.start, at("14:07:49"))
        self.assertEqual(stats.longest.end, at("14:39:19"))
        text = describe(stats, "今天")
        self.assertIn("掉线 2 次", text)
        self.assertIn("14:07–14:39", text)

    def test_ongoing_outage_and_program_exit(self):
        entries = [
            (at("10:00:00"), "WARNING", "直连外网不可用（ConnectTimeout）"),
            (at("10:05:00"), "INFO", "程序退出"),
            (at("11:00:00"), "WARNING", "直连外网不可用（ConnectTimeout）"),
        ]
        stats = compute_day(dt.date(2026, 11, 12), entries, now=at("11:30:00"))
        self.assertEqual(stats.count, 2)
        self.assertEqual(stats.outages[0].seconds, 300)
        self.assertTrue(stats.outages[1].ongoing)
        self.assertIn("仍未恢复", describe(stats, "今天"))

    def test_quiet_day(self):
        self.assertIn("没有掉线", describe(compute_day(dt.date(2026, 11, 12), []), "今天"))
        self.assertEqual(format_duration(8), "8 秒")
        self.assertEqual(format_duration(1890), "31 分钟")
        self.assertEqual(format_duration(4000), "1 小时 6 分钟")


class NetWatchTests(unittest.TestCase):
    def test_burst_of_changes_fires_once_after_settling(self):
        changes = threading.Semaphore(0)
        fired = []

        def wait_for_change():
            changes.acquire()
            return True

        watcher = AddressChangeWatcher(lambda: fired.append(time.monotonic()), settle_seconds=0.3, wait_for_change=wait_for_change)
        watcher.start()
        for _ in range(4):
            changes.release()
            time.sleep(0.05)
        time.sleep(1.2)
        watcher.stop()
        changes.release()
        self.assertEqual(len(fired), 1)


NETSTAT = """
  Proto  Local Address          Foreign Address        State           PID
  TCP    192.168.1.5:50001      183.57.79.253:443      ESTABLISHED     4242
  TCP    192.168.1.5:50002      198.18.0.206:443       ESTABLISHED     4242
  TCP    127.0.0.1:50003        127.0.0.1:9000         ESTABLISHED     4242
  TCP    192.168.1.5:50004      192.168.1.1:80         ESTABLISHED     4242
  TCP    192.168.1.5:50005      183.57.79.252:443      TIME_WAIT       4242
  TCP    192.168.1.5:50006      183.57.79.252:443      ESTABLISHED     999
"""


class UuOnlineTests(unittest.TestCase):
    def test_counts_only_external_established_connections_of_uu(self):
        self.assertEqual(uu_remote.parse_established(NETSTAT, {4242}), 2)
        self.assertEqual(uu_remote.parse_established(NETSTAT, {1}), 0)

    def test_wait_reports_online_offline_or_unknown(self):
        sleeps = []
        probes = iter([0, 0, 5])
        self.assertEqual(uu_remote.wait_until_online(sleeps.append, timeout=60, probe=lambda: next(probes)), 5)
        self.assertEqual(uu_remote.wait_until_online(lambda _: None, timeout=0, probe=lambda: 0), 0)
        self.assertIsNone(uu_remote.wait_until_online(lambda _: None, timeout=0, probe=lambda: None))

    def run_verify(self, waits):
        restarts = []
        waits = iter(waits)

        def restarter(progress, sleeper):
            restarts.append(1)
            return RestartResult(True, "UU 远程已重启（主程序 + 后台服务）")

        result = uu_remote.restart_and_verify(
            None, lambda _: None, restarter=restarter, waiter=lambda sleeper: next(waits)
        )
        return result, len(restarts)

    def test_verified_on_first_try(self):
        result, restarts = self.run_verify([6])
        self.assertTrue(result.success)
        self.assertIn("已确认上线", result.message)
        self.assertEqual(restarts, 1)

    def test_retries_once_when_not_online(self):
        result, restarts = self.run_verify([0, 4])
        self.assertTrue(result.success)
        self.assertIn("第二次重启后已确认上线", result.message)
        self.assertEqual(restarts, 2)

    def test_gives_up_after_two_restarts(self):
        result, restarts = self.run_verify([0, 0])
        self.assertFalse(result.success)
        self.assertIn("重启两次后仍未连上服务器", result.message)
        self.assertEqual(restarts, 2)

    def test_failed_restart_is_returned_as_is(self):
        result = uu_remote.restart_and_verify(
            None, lambda _: None,
            restarter=lambda p, s: RestartResult(False, "未检测到 UU 远程，已跳过重启", "warning"),
            waiter=lambda sleeper: self.fail("should not wait"),
        )
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
