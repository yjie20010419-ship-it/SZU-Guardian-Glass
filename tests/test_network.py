import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from szu_guardian.models import AppConfig, ZONE_AUTO, ZONE_DORMITORY
from szu_guardian.network import ConnectionResult, NetworkClient
from szu_guardian.srun import SrunDevice, SrunStatus


def response(url, status=200, text=""):
    value = Mock()
    value.url = url
    value.status_code = status
    value.text = text
    value.headers = {}
    value.raise_for_status.return_value = None
    return value


def connected_probe_response(url, **kwargs):
    if "msftconnecttest.com" in url:
        return response(url, text="Microsoft Connect Test")
    return response(url, status=204)


def portal_response(url, **kwargs):
    """What a captive portal answers before login."""
    value = response(url, status=302, text="")
    value.headers = {"Location": "https://net.szu.edu.cn/index_1.html"}
    return value


class NetworkClientTests(unittest.TestCase):
    def test_online_does_not_send_login(self):
        session = Mock()
        session.headers = {}
        session.get.side_effect = connected_probe_response
        client = NetworkClient(
            session=session,
            portal_session=session,
            sleeper=lambda _: None,
        )

        result = client.ensure_connected(
            AppConfig(
                username="user",
                password="password",
                zone=ZONE_DORMITORY,
            )
        )

        self.assertTrue(result.connected)
        self.assertFalse(result.reconnected)
        session.post.assert_not_called()

    def test_expired_teaching_session_reconnects_even_when_web_is_reachable(self):
        session = Mock()
        session.headers = {}
        client = NetworkClient(
            session=session,
            portal_session=session,
            sleeper=lambda _: None,
            verify_delays=(0,),
        )
        config = AppConfig(username="user", password="password")

        with (
            patch.object(
                client,
                "_teaching_status",
                return_value=SrunStatus(False, "not_online_error"),
            ),
            patch.object(
                client,
                "check_connection",
                return_value=ConnectionResult(True, "网络连接正常"),
            ) as check,
            patch.object(client, "send_login", return_value="认证成功") as login,
        ):
            result = client.ensure_connected(config)

        self.assertTrue(result.connected)
        login.assert_called_once_with(config, None)
        check.assert_called_once_with()

    def test_offline_sends_login_and_verifies(self):
        session = Mock()
        session.headers = {}
        calls = 0

        def offline_then_login_then_online(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return response("http://portal.local", text="login")
            if calls == 3:
                return response(
                    "http://172.30.255.42:801/eportal/portal/login/",
                    text='jsonpReturn({"result":1,"msg":"认证成功"});',
                )
            return connected_probe_response(url)

        session.get.side_effect = offline_then_login_then_online
        client = NetworkClient(
            session=session,
            portal_session=session,
            sleeper=lambda _: None,
            verify_delays=(0,),
        )

        result = client.ensure_connected(
            AppConfig(
                username="user",
                password="password",
                zone="dormitory",
            )
        )

        self.assertTrue(result.connected)
        self.assertTrue(result.reconnected)
        self.assertIn("自动重连", result.message)

    def _teaching_client(self):
        session = Mock()
        session.headers = {}
        return NetworkClient(
            session=session,
            portal_session=session,
            sleeper=lambda _: None,
            verify_delays=(0,),
        )

    def test_device_list_is_logged_only_when_it_changes(self):
        client = self._teaching_client()
        config = AppConfig(username="user", password="password")
        two = SrunStatus(
            True,
            "ok",
            online_ip="172.30.10.20",
            login_time=1794465873,
            devices=(
                SrunDevice("Macintosh", "172.28.1.50"),
                SrunDevice("Windows", "172.30.10.20"),
            ),
        )
        three = replace(
            two, devices=two.devices + (SrunDevice("Android", "172.28.1.9"),)
        )
        srun = Mock()
        srun.status.side_effect = [two, two, three]
        online = ConnectionResult(True, "网络连接正常")

        with (
            patch.object(client, "_teaching_srun_client", return_value=srun),
            patch.object(client, "check_connection", return_value=online),
        ):
            first = client.ensure_connected(config)
            second = client.ensure_connected(config)
            third = client.ensure_connected(config)

        self.assertEqual(len(first.notes), 1)
        self.assertIn("账号在线设备 2 台", first.notes[0])
        self.assertIn("Windows 172.30.10.20（本机）", first.notes[0])
        self.assertEqual(second.notes, ())
        self.assertIn("账号在线设备 3 台", third.notes[0])

    def test_internet_outage_with_session_online_does_not_log_in(self):
        client = self._teaching_client()
        config = AppConfig(username="user", password="password")
        srun = Mock()
        srun.status.return_value = SrunStatus(True, "ok")

        with (
            patch.object(client, "_teaching_srun_client", return_value=srun),
            patch.object(
                client,
                "check_connection",
                return_value=ConnectionResult(False, "直连外网不可用（微软 ConnectTimeout / 华为 ConnectTimeout）"),
            ),
            patch.object(client, "send_login") as login,
        ):
            result = client.ensure_connected(config)

        login.assert_not_called()
        self.assertFalse(result.connected)
        self.assertFalse(result.login_attempted)  # so UU Remote is not restarted later
        self.assertIn("不是认证问题", result.message)
        self.assertIn("直连外网不可用", result.message)  # counted by the outage stats

    def test_two_of_three_probes_decide(self):
        session = Mock()
        session.headers = {}
        client = NetworkClient(session=session, portal_session=session)
        answers = {
            "msftconnecttest": response("m", status=200, text="Microsoft Connect Test"),
            "hicloud": response("h", status=302, text="portal"),
            "vivo": response("v", status=204),
        }
        session.get.side_effect = lambda url, **kw: next(v for k, v in answers.items() if k in url)
        self.assertTrue(client.check_connection().connected)
        self.assertEqual(session.get.call_count, 3)

        session.get.reset_mock()
        answers["msftconnecttest"] = response("m", status=200, text="<html>portal</html>")
        result = client.check_connection()
        self.assertFalse(result.connected)
        self.assertEqual(session.get.call_count, 2)  # two failures settle it
        self.assertIn("微软返回 200", result.message)

        session.get.reset_mock()
        session.get.side_effect = connected_probe_response
        self.assertTrue(client.check_connection().connected)
        self.assertEqual(session.get.call_count, 2)  # two passes settle it

    def test_captive_portal_redirect_is_not_online(self):
        session = Mock()
        session.headers = {}
        session.get.side_effect = portal_response
        client = NetworkClient(session=session, portal_session=session)
        self.assertFalse(client.check_connection().connected)
        for call in session.get.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_failed_recovery_reports_server_recheck(self):
        client = self._teaching_client()
        config = AppConfig(username="user", password="password")
        offline = SrunStatus(False, "not_online_error")
        srun = Mock()
        srun.status.side_effect = [offline, offline]
        progress = []

        with (
            patch.object(client, "_teaching_srun_client", return_value=srun),
            patch.object(
                client,
                "check_connection",
                return_value=ConnectionResult(False, "直连外网不可用（ConnectTimeout）"),
            ),
            patch.object(client, "send_login", return_value="认证成功"),
        ):
            result = client.ensure_connected(config, progress.append)

        self.assertFalse(result.connected)
        self.assertIn("not_online_error", progress[0])
        self.assertIn("ConnectTimeout", result.message)
        self.assertIn("仍不在线", result.message)

    def test_auto_mode_tries_teaching_first(self):
        session = Mock()
        session.headers = {}
        client = NetworkClient(session=session, portal_session=session)
        config = AppConfig(
            username="user",
            password="password",
            zone=ZONE_AUTO,
        )

        with (
            patch.object(
                client,
                "_login_teaching",
                return_value="教学区认证成功",
            ) as teaching,
            patch.object(client, "_login_dormitory") as dormitory,
        ):
            result = client.send_login(config)

        self.assertEqual(result, "教学区认证成功")
        teaching.assert_called_once_with(config)
        dormitory.assert_not_called()

    def test_auto_mode_falls_back_to_dormitory(self):
        session = Mock()
        session.headers = {}
        client = NetworkClient(session=session, portal_session=session)
        config = AppConfig(
            username="user",
            password="password",
            zone=ZONE_AUTO,
        )

        with (
            patch.object(
                client,
                "_login_teaching",
                side_effect=ConnectionError("不在教学区"),
            ) as teaching,
            patch.object(
                client,
                "_login_dormitory",
                return_value="宿舍区认证成功",
            ) as dormitory,
        ):
            result = client.send_login(config)

        self.assertEqual(result, "宿舍区认证成功")
        teaching.assert_called_once_with(config)
        dormitory.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
