"""Restart NetEase UU Remote (UU 远程 / GameViewer) after a campus reconnect.

UU Remote consists of two layers:

* ``GameViewer.exe`` - the tray/main program, running as the logged-in user.
* ``GameViewerService`` - a LocalSystem Windows service which spawns
  ``GameViewerServer.exe`` and ``GameViewerHealthd.exe``.

The main program can always be restarted.  Restarting the service needs
administrator rights, so it only happens when the guardian itself is elevated
or after the user has authorised a one-off elevated scheduled task.  That task
only runs fixed System32 commands, so moving or replacing the guardian EXE can
neither break it nor be used to run something else with elevated rights.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape


SERVICE_NAME = "GameViewerService"
CLIENT_IMAGE = "GameViewer.exe"
SERVICE_IMAGES = (
    "GameViewerService.exe",
    "GameViewerServer.exe",
    "GameViewerHealthd.exe",
)
RUN_VALUE_NAME = "GameViewer"
UNINSTALL_DISPLAY_NAME = "UU远程"
CLIENT_AUTO_RUN_ARGUMENT = "--auto-run"
ELEVATED_TASK_NAME = "SZUNetworkGuardian-UURemoteRestart"

SERVICE_RESTART_TIMEOUT_SECONDS = 60.0
CLIENT_START_TIMEOUT_SECONDS = 15.0
POLL_SECONDS = 0.5

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
_SERVICE_KEY = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

_SC_MANAGER_CONNECT = 0x0001
_SERVICE_QUERY_STATUS = 0x0004
_SC_STATUS_PROCESS_INFO = 0
_SERVICE_RUNNING = 4

_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_ERROR_CANCELLED = 1223
_TASKKILL_NOT_FOUND = 128

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class RestartResult:
    success: bool
    message: str
    level: str = "success"


@dataclass(frozen=True, slots=True)
class ServiceState:
    state: int
    pid: int

    @property
    def running(self) -> bool:
        return self.state == _SERVICE_RUNNING


# --------------------------------------------------------------------------
# Low-level helpers.  Every side effect goes through _run() or _launch(), so
# the tests can replace exactly these two and never touch the real machine.
# --------------------------------------------------------------------------


def _system_tool(name: str) -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return str(Path(root) / "System32" / name)


def _run(arguments: list[str], timeout: float = 90.0) -> tuple[int, str]:
    """Run a console tool hidden; return ``(exit code, combined output)``."""
    try:
        completed = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, type(exc).__name__
    return completed.returncode, completed.stdout or ""


def _launch(command: list[str]) -> None:
    """Start the UU Remote main program detached from the guardian."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("_PYI_", "_MEIPASS"))
    }
    # A PyInstaller one-file EXE points the DLL search directory at its
    # temporary unpack folder, and child processes inherit that.  Reset it
    # while spawning so UU Remote loads its own DLLs.
    bundle_directory = getattr(sys, "_MEIPASS", None)
    kernel32 = ctypes.windll.kernel32
    if bundle_directory:
        kernel32.SetDllDirectoryW(None)
    try:
        subprocess.Popen(
            command,
            cwd=str(Path(command[0]).parent),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        )
    finally:
        if bundle_directory:
            kernel32.SetDllDirectoryW(str(bundle_directory))


def is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


# --------------------------------------------------------------------------
# Locating UU Remote.
# --------------------------------------------------------------------------


def _split_command_line(command: str) -> list[str]:
    """Split a registry command line into ``[executable, *arguments]``."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end < 0:
            return []
        return [command[1:end], *command[end + 1 :].split()]
    end = command.lower().find(".exe")
    if end < 0:
        return []
    return [command[: end + 4], *command[end + 4 :].split()]


def _read_registry_string(root: int, key_path: str, value_name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(root, key_path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None
    return value if isinstance(value, str) and value else None


def _uninstall_entry_icon() -> str | None:
    import winreg

    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in views:
            try:
                parent = winreg.OpenKey(
                    root, _UNINSTALL_KEY, 0, winreg.KEY_READ | view
                )
            except OSError:
                continue
            with parent:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(parent, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(parent, name) as entry:
                            display, _ = winreg.QueryValueEx(entry, "DisplayName")
                            if display != UNINSTALL_DISPLAY_NAME:
                                continue
                            icon, _ = winreg.QueryValueEx(entry, "DisplayIcon")
                    except OSError:
                        continue
                    if isinstance(icon, str) and icon:
                        return icon
    return None


def _launch_command_candidates() -> list[list[str]]:
    import winreg

    candidates: list[list[str]] = []

    # 1. UU Remote's own auto-start entry: the exact command it uses at logon.
    run_command = _read_registry_string(
        winreg.HKEY_CURRENT_USER, _RUN_KEY, RUN_VALUE_NAME
    )
    if run_command:
        candidates.append(_split_command_line(run_command))

    # 2. The launcher sits next to the service executable.
    service_command = _read_registry_string(
        winreg.HKEY_LOCAL_MACHINE, _SERVICE_KEY, "ImagePath"
    )
    if service_command:
        parts = _split_command_line(service_command)
        if parts:
            candidates.append(
                [str(Path(parts[0]).with_name(CLIENT_IMAGE)), CLIENT_AUTO_RUN_ARGUMENT]
            )

    # 3. The "Apps & features" entry.
    icon = _uninstall_entry_icon()
    if icon:
        parts = _split_command_line(icon.split(",")[0])
        if parts:
            candidates.append([parts[0], CLIENT_AUTO_RUN_ARGUMENT])

    # 4. Default install locations.
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            candidates.append(
                [
                    str(Path(base) / "Netease" / "GameViewer" / CLIENT_IMAGE),
                    CLIENT_AUTO_RUN_ARGUMENT,
                ]
            )
    return candidates


def find_launch_command() -> list[str] | None:
    """Return the command that starts UU Remote, or ``None`` if not installed."""
    if sys.platform != "win32":
        return None
    for candidate in _launch_command_candidates():
        if candidate and Path(candidate[0]).is_file():
            return candidate
    return None


# --------------------------------------------------------------------------
# Service state (read-only; available to non-elevated users).
# --------------------------------------------------------------------------


def query_service(name: str = SERVICE_NAME) -> ServiceState | None:
    """Return the service state, or ``None`` when the service does not exist."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class ServiceStatusProcess(ctypes.Structure):
        _fields_ = [
            ("dwServiceType", wintypes.DWORD),
            ("dwCurrentState", wintypes.DWORD),
            ("dwControlsAccepted", wintypes.DWORD),
            ("dwWin32ExitCode", wintypes.DWORD),
            ("dwServiceSpecificExitCode", wintypes.DWORD),
            ("dwCheckPoint", wintypes.DWORD),
            ("dwWaitHint", wintypes.DWORD),
            ("dwProcessId", wintypes.DWORD),
            ("dwServiceFlags", wintypes.DWORD),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenSCManagerW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    advapi32.OpenSCManagerW.restype = ctypes.c_void_p
    advapi32.OpenServiceW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    advapi32.OpenServiceW.restype = ctypes.c_void_p
    advapi32.QueryServiceStatusEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.QueryServiceStatusEx.restype = wintypes.BOOL
    advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]
    advapi32.CloseServiceHandle.restype = wintypes.BOOL

    manager = advapi32.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
    if not manager:
        return None
    try:
        service = advapi32.OpenServiceW(manager, name, _SERVICE_QUERY_STATUS)
        if not service:
            return None
        try:
            status = ServiceStatusProcess()
            needed = wintypes.DWORD()
            ok = advapi32.QueryServiceStatusEx(
                service,
                _SC_STATUS_PROCESS_INFO,
                ctypes.byref(status),
                ctypes.sizeof(status),
                ctypes.byref(needed),
            )
            if not ok:
                return None
            return ServiceState(int(status.dwCurrentState), int(status.dwProcessId))
        finally:
            advapi32.CloseServiceHandle(service)
    finally:
        advapi32.CloseServiceHandle(manager)


# --------------------------------------------------------------------------
# The elevated scheduled task ("完整重启" authorisation).
# --------------------------------------------------------------------------


def _service_restart_script() -> str:
    """One cmd.exe line that restarts the service; needs administrator rights."""
    net = _system_tool("net.exe")
    taskkill = _system_tool("taskkill.exe")
    images = " ".join(f"/IM {image}" for image in SERVICE_IMAGES)
    return (
        f"{net} stop {SERVICE_NAME} & "
        f"{taskkill} /F {images} & "
        f"{net} start {SERVICE_NAME}"
    )


def _task_xml(user_id: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>SZU 网络守护：校园网重连后重启 UU 远程后台服务</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(_system_tool("cmd.exe"))}</Command>
      <Arguments>{escape(f'/d /c "{_service_restart_script()}"')}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def elevated_task_exists() -> bool:
    if sys.platform != "win32":
        return False
    code, _ = _run([_system_tool("schtasks.exe"), "/Query", "/TN", ELEVATED_TASK_NAME])
    return code == 0


def _run_as_administrator(executable: str, parameters: str) -> int:
    """Run a program elevated (one UAC prompt) and return its exit code."""
    if is_elevated():
        code, _ = _run([executable, *_split_parameters(parameters)])
        return code

    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = parameters
    info.nShow = 0  # SW_HIDE
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == _ERROR_CANCELLED:
            raise PermissionError("已取消管理员授权")
        raise ctypes.WinError(error)
    if not info.hProcess:
        raise OSError("无法获取提权进程的执行结果")
    try:
        kernel32.WaitForSingleObject(info.hProcess, 60_000)
        exit_code = wintypes.DWORD(1)
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code))
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def _split_parameters(parameters: str) -> list[str]:
    result: list[str] = []
    current = ""
    quoted = False
    for character in parameters:
        if character == '"':
            quoted = not quoted
        elif character == " " and not quoted:
            if current:
                result.append(current)
                current = ""
        else:
            current += character
    if current:
        result.append(current)
    return result


def register_elevated_task() -> None:
    """Create the elevated restart task.  Shows one UAC prompt."""
    if sys.platform != "win32":
        raise OSError("完整重启授权仅支持 Windows")
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    if not user:
        raise OSError("无法确定当前 Windows 用户")
    user_id = f"{domain}\\{user}" if domain else user

    handle, temporary_name = tempfile.mkstemp(suffix=".xml", prefix="szu-guardian-")
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(_task_xml(user_id), encoding="utf-16")
        code = _run_as_administrator(
            _system_tool("schtasks.exe"),
            f'/Create /TN "{ELEVATED_TASK_NAME}" /XML "{temporary}" /F',
        )
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    if code != 0 or not elevated_task_exists():
        raise OSError(f"创建计划任务失败（schtasks 返回 {code}）")


def unregister_elevated_task() -> None:
    """Remove the elevated restart task.  Shows one UAC prompt."""
    if not elevated_task_exists():
        return
    code = _run_as_administrator(
        _system_tool("schtasks.exe"),
        f'/Delete /TN "{ELEVATED_TASK_NAME}" /F',
    )
    if code != 0 or elevated_task_exists():
        raise OSError(f"删除计划任务失败（schtasks 返回 {code}）")


def describe_status() -> str:
    """One line for the settings UI describing what a restart will cover."""
    if sys.platform != "win32":
        return "仅支持 Windows"
    if find_launch_command() is None and query_service() is None:
        return "未检测到 UU 远程"
    if query_service() is None:
        return "将重启主程序（未发现后台服务）"
    if is_elevated():
        return "完整重启：主程序 + 后台服务（当前已是管理员）"
    if elevated_task_exists():
        return "完整重启：主程序 + 后台服务（已授权）"
    return "仅重启主程序；后台服务需先授权完整重启"


# --------------------------------------------------------------------------
# The restart itself.
# --------------------------------------------------------------------------


def _kill_image(image: str) -> bool:
    """Force-stop every process with this image name.  True if none remain."""
    code, _ = _run([_system_tool("taskkill.exe"), "/F", "/T", "/IM", image])
    return code in (0, _TASKKILL_NOT_FOUND)


def _image_running(image: str) -> bool:
    code, output = _run(
        [
            _system_tool("tasklist.exe"),
            "/FI",
            f"IMAGENAME eq {image}",
            "/FO",
            "CSV",
            "/NH",
        ]
    )
    return code == 0 and image.lower() in output.lower()


def _wait_until(
    condition: Callable[[], bool],
    timeout: float,
    sleeper: Callable[[float], None],
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        sleeper(POLL_SECONDS)


def _restart_service(
    service: ServiceState | None,
    sleeper: Callable[[float], None],
) -> tuple[str, str]:
    """Restart the background service.

    Returns ``(status, detail)`` with status ``restarted``, ``skipped`` or
    ``failed``.
    """
    if service is None:
        return "skipped", "未发现后台服务"

    def restarted() -> bool:
        current = query_service()
        return bool(current and current.running and current.pid != service.pid)

    if is_elevated():
        _run([_system_tool("cmd.exe"), "/d", "/c", _service_restart_script()])
        if _wait_until(restarted, 10.0, sleeper):
            return "restarted", ""
        return "failed", "后台服务未能重新启动"

    if not elevated_task_exists():
        return "skipped", "未授权完整重启"

    code, _ = _run([_system_tool("schtasks.exe"), "/Run", "/TN", ELEVATED_TASK_NAME])
    if code != 0:
        return "failed", "无法触发已授权的重启任务，请重新授权完整重启"
    if _wait_until(restarted, SERVICE_RESTART_TIMEOUT_SECONDS, sleeper):
        return "restarted", ""
    return (
        "failed",
        f"后台服务未在 {SERVICE_RESTART_TIMEOUT_SECONDS:g} 秒内完成重启",
    )


def restart_uu_remote(
    progress_callback: ProgressCallback | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> RestartResult:
    """Stop UU Remote and start it again.  Never raises for expected failures."""
    if sys.platform != "win32":
        return RestartResult(False, "重启 UU 远程仅支持 Windows", "warning")

    command = find_launch_command()
    service = query_service()
    if command is None and service is None:
        return RestartResult(False, "未检测到 UU 远程，已跳过重启", "warning")

    if progress_callback:
        progress_callback("正在重启 UU 远程…")

    client_stopped = _kill_image(CLIENT_IMAGE)
    service_status, service_detail = _restart_service(service, sleeper)

    if command is None:
        return RestartResult(
            False,
            "已结束 UU 远程，但未找到主程序路径，无法重新启动",
            "warning",
        )
    if not client_stopped and _image_running(CLIENT_IMAGE):
        return RestartResult(
            False,
            "无法结束 UU 远程主程序（可能以管理员身份运行），未执行重启",
            "warning",
        )

    sleeper(1.0)
    try:
        _launch(command)
    except OSError as exc:
        return RestartResult(False, f"UU 远程主程序启动失败：{exc}", "warning")
    if not _wait_until(
        lambda: _image_running(CLIENT_IMAGE),
        CLIENT_START_TIMEOUT_SECONDS,
        sleeper,
    ):
        return RestartResult(
            False,
            "已发出启动命令，但未检测到 UU 远程主程序运行",
            "warning",
        )

    if service_status == "restarted":
        return RestartResult(True, "UU 远程已重启（主程序 + 后台服务）")
    if service_status == "failed":
        return RestartResult(
            False,
            f"UU 远程主程序已重启，但后台服务重启失败：{service_detail}",
            "warning",
        )
    return RestartResult(
        True,
        f"UU 远程主程序已重启（{service_detail}，后台服务未重启）",
    )


# --- Online check -----------------------------------------------------------
# UU Remote exposes no status API.  When it is online, GameViewerServer.exe
# keeps TLS connections to NetEase servers (seen as several ESTABLISHED TCP
# connections to port 443).  "No such connection for a minute after a
# restart" is therefore used as the sign that it did not come back.
ONLINE_IMAGE = "GameViewerServer.exe"
ONLINE_TIMEOUT_SECONDS = 60.0
ONLINE_POLL_SECONDS = 3.0


def _is_external(address: str) -> bool:
    import ipaddress

    host = address.rsplit(":", 1)[0].strip("[]")
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return False
    if parsed.is_loopback or parsed.is_unspecified or parsed.is_link_local:
        return False
    # 198.18.0.0/15 is a proxy's fake-IP range: traffic still leaves the PC.
    if parsed in ipaddress.ip_network("198.18.0.0/15"):
        return True
    return not parsed.is_private


def _image_pids(image: str) -> set[int]:
    code, output = _run(
        [_system_tool("tasklist.exe"), "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"]
    )
    pids = set()
    if code != 0:
        return pids
    for line in output.splitlines():
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) > 1 and parts[0].strip('"').lower() == image.lower():
            try:
                pids.add(int(parts[1].strip('"')))
            except ValueError:
                continue
    return pids


def parse_established(netstat_output: str, pids: set[int]) -> int:
    """Count ESTABLISHED TCP connections to outside hosts owned by ``pids``."""
    count = 0
    for line in netstat_output.splitlines():
        fields = line.split()
        if len(fields) != 5 or fields[0].upper() != "TCP" or fields[3].upper() != "ESTABLISHED":
            continue
        try:
            pid = int(fields[4])
        except ValueError:
            continue
        if pid in pids and _is_external(fields[2]):
            count += 1
    return count


def online_connections() -> int | None:
    """External connections of UU's server process; None when it isn't running."""
    pids = _image_pids(ONLINE_IMAGE)
    if not pids:
        return None
    code, output = _run([_system_tool("netstat.exe"), "-ano", "-p", "TCP"])
    if code != 0:
        return None
    return parse_established(output, pids)


def wait_until_online(
    sleeper: Callable[[float], None] = time.sleep,
    timeout: float = ONLINE_TIMEOUT_SECONDS,
    probe: Callable[[], int | None] = online_connections,
) -> int | None:
    """Wait for UU to reach its servers; return the connection count (0 = not
    online in time, None = cannot tell because the process never appeared)."""
    deadline = time.monotonic() + timeout
    seen_process = False
    while True:
        count = probe()
        if count:
            return count
        seen_process = seen_process or count is not None
        if time.monotonic() >= deadline:
            return 0 if seen_process else None
        sleeper(ONLINE_POLL_SECONDS)


def restart_and_verify(
    progress_callback: ProgressCallback | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    restarter: Callable[..., RestartResult] | None = None,
    waiter: Callable[..., int | None] | None = None,
) -> RestartResult:
    """Restart UU Remote, confirm it is online, and retry once if it is not."""
    restarter = restarter or restart_uu_remote
    waiter = waiter or wait_until_online
    result = restarter(progress_callback, sleeper)
    for attempt in (1, 2):
        if not result.success:
            return result
        if progress_callback:
            progress_callback("正在确认 UU 远程是否已上线…")
        connections = waiter(sleeper)
        if connections:
            suffix = "，已确认上线" if attempt == 1 else "，第二次重启后已确认上线"
            return RestartResult(True, f"{result.message}{suffix}（{connections} 条服务器连接）")
        if connections is None:
            return RestartResult(True, f"{result.message}（未找到 {ONLINE_IMAGE}，无法确认是否上线）", "warning")
        if attempt == 1:
            if progress_callback:
                progress_callback(f"UU 远程 {ONLINE_TIMEOUT_SECONDS:g} 秒内没有连上服务器，再重启一次…")
            result = restarter(progress_callback, sleeper)
    return RestartResult(
        False,
        f"UU 远程重启两次后仍未连上服务器，请手动检查（{result.message}）",
        "warning",
    )
