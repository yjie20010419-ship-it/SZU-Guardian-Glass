import unittest

from szu_guardian.models import AppConfig
from szu_guardian.monitor import MonitorController
from szu_guardian.network import ConnectionResult
from szu_guardian.uu_remote import RestartResult


class _FakeClient:
    def __init__(self, result):
        self.result = result

    def ensure_connected(self, config, progress_callback=None):
        return self.result


class MonitorControllerTests(unittest.TestCase):
    def test_failed_check_refreshes_client_before_next_cycle(self):
        clients = []
        results = [
            ConnectionResult(False, "offline"),
            ConnectionResult(True, "online"),
        ]

        def factory():
            client = _FakeClient(results[len(clients)])
            clients.append(client)
            return client

        controller = MonitorController(lambda event: None, client_factory=factory)
        controller._wait_until_next_check = lambda interval: "stop"

        controller._run_loop(AppConfig(username="u", password="p"))

        self.assertEqual(len(clients), 2)

    def test_resume_refreshes_client_immediately(self):
        clients = []

        def factory():
            client = _FakeClient(ConnectionResult(True, "online"))
            clients.append(client)
            return client

        controller = MonitorController(lambda event: None, client_factory=factory)
        wait_reasons = iter(("resume", "stop"))
        controller._wait_until_next_check = lambda interval: next(wait_reasons)

        controller._run_loop(AppConfig(username="u", password="p"))

        self.assertEqual(len(clients), 2)


class ConfigUpdateTests(unittest.TestCase):
    def test_saved_interval_applies_to_running_loop(self):
        events = []
        controller = MonitorController(
            events.append,
            client_factory=lambda: _FakeClient(ConnectionResult(True, "online")),
        )
        waits = []

        def fake_wait(interval):
            waits.append(interval)
            if len(waits) == 1:
                controller.update_config(
                    AppConfig(username="u", password="p", interval_minutes=2)
                )
                return "manual"
            return "stop"

        controller._wait_until_next_check = fake_wait
        controller._run_loop(AppConfig(username="u", password="p", interval_minutes=5))

        self.assertEqual(waits, [300, 120])
        waiting = [e.message for e in events if e.state == "waiting"]
        self.assertIn("2 分钟", waiting[-1])

    def test_changed_account_rebuilds_the_client(self):
        clients = []

        def factory():
            client = _FakeClient(ConnectionResult(True, "online"))
            clients.append(client)
            return client

        controller = MonitorController(lambda event: None, client_factory=factory)
        waits = iter(("manual", "stop"))

        def fake_wait(interval):
            controller.update_config(AppConfig(username="other", password="p"))
            return next(waits)

        controller._wait_until_next_check = fake_wait
        controller._run_loop(AppConfig(username="u", password="p"))

        self.assertEqual(len(clients), 2)


class _ScriptedClient:
    """Returns one scripted outcome per check; an exception is raised."""

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    def ensure_connected(self, config, progress_callback=None):
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class UuRemoteRestartTests(unittest.TestCase):
    ONLINE = ConnectionResult(True, "网络连接正常", 20)
    RECONNECTED = ConnectionResult(True, "已自动重连，网络恢复正常", 30, True)
    OFFLINE = ConnectionResult(False, "直连外网不可用")

    def run_cycles(self, outcomes, config=None, restarter="default"):
        events = []
        restarts = []

        def fake_restarter(progress):
            restarts.append(len(events))
            progress("正在重启 UU 远程…")
            return RestartResult(True, "UU 远程已重启")

        outcomes = list(outcomes)
        client = _ScriptedClient(outcomes)
        controller = MonitorController(
            events.append,
            client_factory=lambda: client,
            uu_restarter=fake_restarter if restarter == "default" else restarter,
        )
        wait_reasons = iter(["interval"] * (len(outcomes) - 1) + ["stop"])
        controller._wait_until_next_check = lambda interval: next(wait_reasons)

        controller._run_loop(config or AppConfig(username="u", password="p"))
        return events, restarts

    def test_reconnect_restarts_uu_remote_once(self):
        events, restarts = self.run_cycles([self.RECONNECTED, self.ONLINE])

        self.assertEqual(len(restarts), 1)
        states = [event.state for event in events]
        self.assertLess(states.index("online"), states.index("restarting"))
        self.assertIn("UU 远程已重启", [event.message for event in events])

    def test_normal_checks_never_restart_uu_remote(self):
        _, restarts = self.run_cycles([self.ONLINE, self.ONLINE, self.ONLINE])

        self.assertEqual(restarts, [])

    def test_recovery_seen_on_a_later_check_still_restarts(self):
        _, restarts = self.run_cycles(
            [self.ONLINE, self.OFFLINE, self.ONLINE, self.ONLINE]
        )

        self.assertEqual(len(restarts), 1)

    def test_recovery_after_an_error_restarts(self):
        _, restarts = self.run_cycles([RuntimeError("boom"), self.ONLINE])

        self.assertEqual(len(restarts), 1)

    def test_every_reconnect_restarts_again(self):
        _, restarts = self.run_cycles(
            [self.RECONNECTED, self.ONLINE, self.RECONNECTED]
        )

        self.assertEqual(len(restarts), 2)

    def test_disabled_option_skips_the_restart(self):
        config = AppConfig(username="u", password="p", restart_uu_remote=False)

        _, restarts = self.run_cycles([self.RECONNECTED], config=config)

        self.assertEqual(restarts, [])

    def test_restart_failure_does_not_stop_monitoring(self):
        def broken(progress):
            raise OSError("拒绝访问")

        events, _ = self.run_cycles(
            [self.RECONNECTED, self.ONLINE], restarter=broken
        )

        messages = [event.message for event in events]
        self.assertTrue(any("重启 UU 远程时出错" in message for message in messages))
        self.assertEqual(messages.count("网络连接正常"), 1)

    def test_notes_are_emitted_before_the_result(self):
        noted = ConnectionResult(True, "网络连接正常", 20, notes=("账号在线设备 2 台",))

        events, _ = self.run_cycles([noted])

        states = [event.state for event in events]
        self.assertLess(states.index("note"), states.index("online"))

    def test_no_restarter_means_no_restart(self):
        events, _ = self.run_cycles([self.RECONNECTED], restarter=None)

        self.assertNotIn("restarting", [event.state for event in events])


if __name__ == "__main__":
    unittest.main()
