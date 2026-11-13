import sys
import unittest
from unittest.mock import patch

from szu_guardian import uu_remote
from szu_guardian.uu_remote import RestartResult, ServiceState


LAUNCH = [r"D:\UUyc\GameViewer\GameViewer.exe", "--auto-run"]
RUNNING = 4


class _Machine:
    """A fake Windows box: records commands and never touches the real one."""

    def __init__(self, service_pid=100, client_running=True):
        self.commands = []
        self.launched = []
        self.service_pid = service_pid
        self.client_running = client_running
        self.task_restarts_service = True
        self.task_run_code = 0
        self.kill_code = 0

    def run(self, arguments, timeout=90.0):
        self.commands.append(arguments)
        tool = arguments[0].lower()
        if tool.endswith("taskkill.exe"):
            if uu_remote.CLIENT_IMAGE in arguments and self.kill_code == 0:
                self.client_running = False
            return self.kill_code, ""
        if tool.endswith("tasklist.exe"):
            return 0, '"GameViewer.exe","1"' if self.client_running else "INFO: none"
        if tool.endswith("schtasks.exe") and "/Run" in arguments:
            if self.task_run_code == 0 and self.task_restarts_service:
                self.service_pid += 1
            return self.task_run_code, ""
        if tool.endswith("cmd.exe"):
            self.service_pid += 1
            return 0, ""
        return 0, ""

    def launch(self, command):
        self.launched.append(command)
        self.client_running = True

    def query_service(self, name=uu_remote.SERVICE_NAME):
        if self.service_pid is None:
            return None
        return ServiceState(RUNNING, self.service_pid)

    def ran(self, tool, *fragments):
        return any(
            command[0].lower().endswith(tool)
            and all(fragment in command for fragment in fragments)
            for command in self.commands
        )


class RestartUuRemoteTests(unittest.TestCase):
    def setUp(self):
        self.machine = _Machine()
        self.elevated = False
        self.task_exists = False
        self.launch_command = list(LAUNCH)
        patches = (
            patch.object(sys, "platform", "win32"),
            patch.object(uu_remote, "_run", self.machine.run),
            patch.object(uu_remote, "_launch", self.machine.launch),
            patch.object(uu_remote, "query_service", self.machine.query_service),
            patch.object(
                uu_remote, "find_launch_command", lambda: self.launch_command
            ),
            patch.object(uu_remote, "is_elevated", lambda: self.elevated),
            patch.object(uu_remote, "elevated_task_exists", lambda: self.task_exists),
            patch.object(uu_remote, "SERVICE_RESTART_TIMEOUT_SECONDS", 0.0),
            patch.object(uu_remote, "CLIENT_START_TIMEOUT_SECONDS", 0.0),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def restart(self):
        messages = []
        result = uu_remote.restart_uu_remote(messages.append, sleeper=lambda _: None)
        return result, messages

    def test_without_authorisation_only_the_main_program_is_restarted(self):
        result, messages = self.restart()

        self.assertTrue(result.success)
        self.assertIn("主程序已重启", result.message)
        self.assertIn("未授权完整重启", result.message)
        self.assertEqual(messages, ["正在重启 UU 远程…"])
        self.assertTrue(self.machine.ran("taskkill.exe", "/IM", "GameViewer.exe"))
        self.assertFalse(self.machine.ran("schtasks.exe", "/Run"))
        self.assertFalse(self.machine.ran("cmd.exe"))
        self.assertEqual(self.machine.launched, [LAUNCH])
        self.assertEqual(self.machine.service_pid, 100)

    def test_authorised_task_restarts_the_service_too(self):
        self.task_exists = True

        result, _ = self.restart()

        self.assertTrue(result.success)
        self.assertEqual(result.message, "UU 远程已重启（主程序 + 后台服务）")
        self.assertTrue(
            self.machine.ran("schtasks.exe", "/Run", uu_remote.ELEVATED_TASK_NAME)
        )
        self.assertEqual(self.machine.service_pid, 101)
        self.assertEqual(self.machine.launched, [LAUNCH])

    def test_elevated_guardian_restarts_the_service_directly(self):
        self.elevated = True

        result, _ = self.restart()

        self.assertTrue(result.success)
        self.assertIn("后台服务", result.message)
        self.assertTrue(self.machine.ran("cmd.exe", "/c"))
        self.assertFalse(self.machine.ran("schtasks.exe", "/Run"))

    def test_service_that_does_not_come_back_is_reported(self):
        self.task_exists = True
        self.machine.task_restarts_service = False

        result, _ = self.restart()

        self.assertFalse(result.success)
        self.assertEqual(result.level, "warning")
        self.assertIn("后台服务重启失败", result.message)
        self.assertEqual(self.machine.launched, [LAUNCH])

    def test_task_that_cannot_be_triggered_is_reported(self):
        self.task_exists = True
        self.machine.task_run_code = 1

        result, _ = self.restart()

        self.assertFalse(result.success)
        self.assertIn("重新授权", result.message)

    def test_main_program_that_cannot_be_stopped_is_not_started_twice(self):
        self.machine.kill_code = 1

        result, _ = self.restart()

        self.assertFalse(result.success)
        self.assertIn("无法结束", result.message)
        self.assertEqual(self.machine.launched, [])

    def test_main_program_that_does_not_start_is_reported(self):
        self.machine.launch = lambda command: None
        with patch.object(uu_remote, "_launch", self.machine.launch):
            result, _ = self.restart()

        self.assertFalse(result.success)
        self.assertIn("未检测到 UU 远程主程序运行", result.message)

    def test_missing_installation_is_skipped_without_touching_anything(self):
        self.launch_command = None
        self.machine.service_pid = None

        result, messages = self.restart()

        self.assertFalse(result.success)
        self.assertIn("未检测到 UU 远程", result.message)
        self.assertEqual(messages, [])
        self.assertEqual(self.machine.commands, [])
        self.assertEqual(self.machine.launched, [])

    def test_not_running_main_program_is_still_started(self):
        self.machine.client_running = False
        self.machine.kill_code = uu_remote._TASKKILL_NOT_FOUND

        result, _ = self.restart()

        self.assertTrue(result.success)
        self.assertEqual(self.machine.launched, [LAUNCH])


class UuRemoteHelperTests(unittest.TestCase):
    def test_split_quoted_command_line(self):
        self.assertEqual(
            uu_remote._split_command_line(
                '"D:\\UU yc\\GameViewer\\GameViewer.exe" --auto-run'
            ),
            ["D:\\UU yc\\GameViewer\\GameViewer.exe", "--auto-run"],
        )

    def test_split_unquoted_command_line_with_spaces(self):
        self.assertEqual(
            uu_remote._split_command_line(
                "C:\\Program Files\\Netease\\GameViewer\\GameViewerService.exe --service"
            ),
            [
                "C:\\Program Files\\Netease\\GameViewer\\GameViewerService.exe",
                "--service",
            ],
        )

    def test_split_rejects_text_without_an_executable(self):
        self.assertEqual(uu_remote._split_command_line("not a command"), [])

    def test_split_parameters_keeps_quoted_paths_together(self):
        self.assertEqual(
            uu_remote._split_parameters('/Create /TN "A B" /XML "C:\\x y\\t.xml" /F'),
            ["/Create", "/TN", "A B", "/XML", "C:\\x y\\t.xml", "/F"],
        )

    def test_task_only_runs_fixed_system_commands(self):
        with patch.dict("os.environ", {"SystemRoot": r"C:\Windows"}):
            xml = uu_remote._task_xml("PC\\user & co")

        self.assertIn("<UserId>PC\\user &amp; co</UserId>", xml)
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", xml)
        self.assertIn("<Command>C:\\Windows\\System32\\cmd.exe</Command>", xml)
        self.assertIn("net.exe stop GameViewerService &amp; ", xml)
        self.assertIn("net.exe start GameViewerService", xml)
        self.assertNotIn(sys.executable, xml)

    def test_task_xml_is_well_formed(self):
        from xml.etree import ElementTree

        with patch.dict("os.environ", {"SystemRoot": r"C:\Windows"}):
            xml = uu_remote._task_xml("PC\\user")
        root = ElementTree.fromstring(xml.encode("utf-16"))
        namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        arguments = root.find("t:Actions/t:Exec/t:Arguments", namespace).text

        self.assertTrue(arguments.startswith('/d /c "'))
        self.assertEqual(arguments.count(" & "), 2)

    def test_result_defaults_to_success_level(self):
        self.assertEqual(RestartResult(True, "ok").level, "success")


class LaunchDetachTests(unittest.TestCase):
    """UU is started outside the guardian's job when Windows allows it."""

    def test_asks_to_break_away_from_the_guardians_job(self):
        with patch.object(uu_remote.subprocess, "Popen") as popen:
            uu_remote._launch([r"C:\UU\GameViewer.exe"])
        flags = popen.call_args.kwargs["creationflags"]
        self.assertTrue(flags & uu_remote._CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(flags & uu_remote._DETACHED_PROCESS)

    def test_falls_back_when_the_job_forbids_breakaway(self):
        calls = []

        def popen(*args, **kwargs):
            calls.append(kwargs["creationflags"])
            if kwargs["creationflags"] & uu_remote._CREATE_BREAKAWAY_FROM_JOB:
                raise PermissionError(5, "Access is denied")
            return object()

        with patch.object(uu_remote.subprocess, "Popen", side_effect=popen):
            uu_remote._launch([r"C:\UU\GameViewer.exe"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[1] & uu_remote._CREATE_BREAKAWAY_FROM_JOB)

    def test_other_launch_errors_still_surface(self):
        with patch.object(uu_remote.subprocess, "Popen", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                uu_remote._launch([r"C:\UU\GameViewer.exe"])


class InsideJobTests(unittest.TestCase):

    def test_reports_a_boolean(self):
        from szu_guardian.runtime_environment import inside_job

        self.assertIsInstance(inside_job(), bool)


if __name__ == "__main__":
    unittest.main()
