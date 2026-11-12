import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

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
    token = parse_qs(urlparse(url).query)["wd"][0]
    value = response(url, status=302, text=token)
    value.headers = {"Location": "https://wappass.baidu.com/captcha"}
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

    def test_internet_hiccup_with_session_online_is_not_a_reconnect(self):
        client = self._teaching_client()
        config = AppConfig(username="user", password="password")
        srun = Mock()
        srun.status.return_value = SrunStatus(True, "ok")
        probes = iter([
            ConnectionResult(False, "直连外网不可用（ConnectTimeout）"),
            ConnectionResult(True, "网络连接正常", 30),
        ])
        progress = []

        with (
            patch.object(client, "_teaching_srun_client", return_value=srun),
            patch.object(client, "check_connection", side_effect=lambda: next(probes)),
            patch.object(client, "send_login", return_value="认证成功"),
        ):
            result = client.ensure_connected(config, progress.append)

        self.assertTrue(result.connected)
        self.assertFalse(result.reconnected)  # so UU Remote is not restarted
        self.assertIn("认证一直在线", result.message)
        self.assertIn("ConnectTimeout", progress[0])
        self.assertIn("认证仍在线", progress[0])

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
