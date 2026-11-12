from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from typing import Callable

import requests

from .direct_network import DirectRoute
from .models import (
    AppConfig,
    ZONE_AUTO,
    ZONE_DORMITORY,
    ZONE_OFFICE,
)
from .srun import SrunClient, SrunStatus


DORMITORY_LOGIN_URL = "http://172.30.255.42:801/eportal/portal/login/"

MICROSOFT_CHECK_URL = "http://www.msftconnecttest.com/connecttest.txt"
MICROSOFT_CHECK_TEXT = "Microsoft Connect Test"


@dataclass(frozen=True, slots=True)
class Probe:
    """A connectivity-check endpoint and the exact answer it must give.

    A captive portal answers these with a redirect or its own page, so only
    the exact status (and text, where given) proves the internet is reachable.
    """

    name: str
    url: str
    status: int
    text: str | None = None


# Three independent providers; the internet counts as reachable when at least
# two answer correctly.  If each probe fails spuriously with probability p,
# requiring all of two misreports an outage with ≈2p, while "2 of 3" needs two
# simultaneous failures: 3p²(1-p) + p³ ≈ 3p² (p = 1% → 2% vs 0.03%).
PROBES = (
    Probe("微软", MICROSOFT_CHECK_URL, 200, MICROSOFT_CHECK_TEXT),
    Probe("华为", "http://connectivitycheck.platform.hicloud.com/generate_204", 204),
    Probe("vivo", "http://wifi.vivo.com.cn/generate_204", 204),
)
PROBES_REQUIRED = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    )
}

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    connected: bool
    message: str
    latency_ms: int | None = None
    # True when this check had to log in again to bring the network back.
    reconnected: bool = False
    # True whenever a login was sent, successful or not.  UU Remote is
    # restarted once the network is back after any re-login, never after an
    # outage the login was not involved in.
    login_attempted: bool = False
    # Diagnostic lines (server status, online devices) for the local log.
    notes: tuple[str, ...] = ()


def _format_time(timestamp: int | None) -> str:
    if not timestamp:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


def describe_devices(status: SrunStatus) -> str:
    items = []
    for device in status.devices:
        label = f"{device.name} {device.ip}".strip()
        if device.ip and device.ip == status.online_ip:
            label += "（本机）"
        items.append(label)
    text = f"账号在线设备 {len(status.devices)} 台：" + "、".join(items)
    login_time = _format_time(status.login_time)
    if login_time:
        text += f"；本机本次登录于 {login_time}"
    return text


def _new_direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(HEADERS)
    return session


def _parse_jsonp_object(text: str) -> dict[str, object]:
    match = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", text.strip(), re.DOTALL)
    if not match:
        raise ConnectionError("宿舍区认证服务器返回了无法识别的数据")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ConnectionError("宿舍区认证服务器返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise ConnectionError("宿舍区认证响应格式不正确")
    return payload


class NetworkClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        portal_session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        verify_delays: tuple[float, ...] = (2.0, 4.0, 8.0),
    ):
        self.session = session or _new_direct_session()
        self.portal_session = portal_session or (
            session if session is not None else _new_direct_session()
        )
        for active_session in {id(self.session): self.session, id(self.portal_session): self.portal_session}.values():
            active_session.trust_env = False
            active_session.headers.update(HEADERS)
        self.sleeper = sleeper
        self.verify_delays = verify_delays
        self._allow_direct_setup = session is None and portal_session is None
        self.direct_route: DirectRoute | None = None
        self._last_device_summary: str | None = None

    def _configure_direct(self, enabled: bool) -> None:
        if not enabled or not self._allow_direct_setup or self.direct_route:
            return
        route = DirectRoute.discover()
        self.direct_route = route
        self.session = route.session()
        self.portal_session = route.session()
        self.session.headers.update(HEADERS)
        self.portal_session.headers.update(HEADERS)

    def _get_check_target(self, url: str) -> requests.Response:
        # Never follow redirects: a captive portal answers with one.  In
        # direct mode the session resolves names through the campus adapter.
        return self.session.get(url, timeout=(3, 5), allow_redirects=False)

    def check_connection(self) -> ConnectionResult:
        started = time.perf_counter()
        errors: list[str] = []
        passed = 0
        for index, probe in enumerate(PROBES):
            try:
                response = self._get_check_target(probe.url)
                if response.status_code == probe.status and (
                    probe.text is None or probe.text in response.text
                ):
                    passed += 1
                else:
                    errors.append(f"{probe.name}返回 {response.status_code}")
            except requests.RequestException as exc:
                errors.append(f"{probe.name} {type(exc).__name__}")
            remaining = len(PROBES) - index - 1
            if passed >= PROBES_REQUIRED or passed + remaining < PROBES_REQUIRED:
                break  # the verdict can no longer change

        if passed >= PROBES_REQUIRED:
            latency = round((time.perf_counter() - started) * 1000)
            mode = "（校园网直连）" if self.direct_route else ""
            return ConnectionResult(True, f"网络连接正常{mode}", latency)

        detail = " / ".join(errors) if errors else "无响应"
        return ConnectionResult(False, f"直连外网不可用（{detail}）")

    def _teaching_srun_client(self, config: AppConfig) -> SrunClient:
        # Real hostname with full TLS verification; in direct mode the
        # portal session resolves it through the campus adapter's DNS.
        return SrunClient(
            config.username,
            config.password,
            session=self.portal_session,
            base_url="https://net.szu.edu.cn",
        )

    def _teaching_status(
        self,
        config: AppConfig,
        notes: list[str],
    ) -> SrunStatus | None:
        try:
            status = self._teaching_srun_client(config).status()
        except (ConnectionError, requests.RequestException) as exc:
            notes.append(f"查询认证状态失败：{exc}")
            return None
        if status.online is None:
            notes.append(f"认证服务器返回未知状态：{status.describe()}")
        elif status.online and status.devices:
            summary = describe_devices(status)
            if summary != self._last_device_summary:
                self._last_device_summary = summary
                notes.append(summary)
        return status

    def _teaching_online_status(self, config: AppConfig) -> bool | None:
        status = self._teaching_status(config, [])
        return status.online if status else None

    def _login_dormitory(self, config: AppConfig) -> str:
        response = self.portal_session.get(
            DORMITORY_LOGIN_URL,
            params={
                "user_account": config.username,
                "user_password": config.password,
            },
            timeout=(4, 10),
            allow_redirects=True,
        )
        response.raise_for_status()
        payload = _parse_jsonp_object(response.text)
        message = str(payload.get("msg", "")).strip()
        success = (
            payload.get("result") in (1, "1", True)
            or "success" in message.lower()
            or "成功" in message
        )
        if not success:
            raise ConnectionError(message or "宿舍区认证失败，请检查账号和密码")
        return message or "宿舍区认证成功"

    def _login_teaching(self, config: AppConfig) -> str:
        result = self._teaching_srun_client(config).login()
        if not result.success:
            raise ConnectionError(
                f"教学 / 办公区认证失败：{result.message or '请检查账号和密码'}"
            )
        return result.message

    def send_login(
        self,
        config: AppConfig,
        progress_callback: ProgressCallback | None = None,
    ) -> str:
        zone = config.zone
        if zone == ZONE_AUTO:
            errors: list[str] = []
            attempts = (
                ("教学 / 办公区", self._login_teaching),
                ("宿舍区", self._login_dormitory),
            )
            for zone_name, login in attempts:
                if progress_callback:
                    progress_callback(f"自动尝试：正在使用{zone_name}认证…")
                try:
                    return login(config)
                except (ConnectionError, requests.RequestException) as exc:
                    errors.append(f"{zone_name}：{exc}")
            details = "；".join(errors)
            raise ConnectionError(
                f"自动尝试均未成功，请手动选择实际区域。{details}"
            )

        if progress_callback:
            zone_name = "宿舍区" if zone == ZONE_DORMITORY else "教学 / 办公区"
            progress_callback(f"正在使用{zone_name}认证…")

        if zone == ZONE_DORMITORY:
            return self._login_dormitory(config)
        if zone == ZONE_OFFICE:
            return self._login_teaching(config)
        raise ConnectionError("无法确定校园网区域")

    def ensure_connected(
        self,
        config: AppConfig,
        progress_callback: ProgressCallback | None = None,
    ) -> ConnectionResult:
        self._configure_direct(config.direct_mode)
        notes: list[str] = []
        teaching = config.zone == ZONE_OFFICE
        authentication_expired = False
        # True when the server confirmed the session is still online: then a
        # failed probe is an internet hiccup, not a lost login.
        authenticated = False
        if teaching:
            status = self._teaching_status(config, notes)
            authentication_expired = status is not None and status.online is False
            authenticated = status is not None and status.online is True
            if authentication_expired:
                # The device list is gone once we are offline; the last one
                # logged shows who was online right before the drop.
                self._last_device_summary = None
                if progress_callback:
                    progress_callback(
                        "检测到校园网认证已失效（服务器返回 "
                        f"{status.describe()}），正在重新认证…"
                    )

        if not authentication_expired:
            current = self.check_connection()
            if current.connected:
                return replace(current, notes=tuple(notes))
            if authenticated:
                # The server says this PC is logged in, so logging in again
                # cannot help: the problem is beyond the campus login (school
                # uplink or the probe sites).  Report it and let the monitor
                # re-check soon.
                return ConnectionResult(
                    False,
                    f"认证在线，但{current.message}；不是认证问题，暂不重新登录",
                    notes=tuple(notes),
                )
            if progress_callback:
                progress_callback(f"外网检测失败：{current.message}（认证状态未知），尝试重新登录…")

        login_message = self.send_login(config, progress_callback)
        if progress_callback:
            progress_callback(f"{login_message}，正在等待外网恢复…")

        verified = ConnectionResult(False, "直连外网不可用（未检测）")
        for attempt, delay in enumerate(self.verify_delays, start=1):
            self.sleeper(delay)
            verified = self.check_connection()
            if verified.connected:
                if teaching:
                    self._teaching_status(config, notes)
                return ConnectionResult(
                    True,
                    "已自动重连，网络恢复正常",
                    verified.latency_ms,
                    reconnected=True,
                    login_attempted=True,
                    notes=tuple(notes),
                )
            if progress_callback and attempt < len(self.verify_delays):
                progress_callback(
                    f"外网尚未恢复，{self.verify_delays[attempt]:g} 秒后再次复查…"
                )

        recheck = ""
        if teaching:
            status = self._teaching_status(config, notes)
            if status is None:
                recheck = "；复查认证状态失败"
            elif status.online:
                recheck = "；复查认证状态：在线（认证正常，但外网不通）"
            else:
                recheck = (
                    f"；复查认证状态：仍不在线（{status.describe()}），"
                    "可能刚登录就被其他设备挤下线"
                )
        return ConnectionResult(
            False,
            "认证服务器已接受登录，但外网仍不可用"
            f"（最后一次检测：{verified.message}{recheck}）",
            login_attempted=True,
            notes=tuple(notes),
        )
