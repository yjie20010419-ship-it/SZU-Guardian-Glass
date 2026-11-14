import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import datetime as dt
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from szu_guardian import cache_dir
from szu_guardian.local_log import LocalLog
from szu_guardian.single_instance import SingleInstance


def make_unpack(parent: Path, name: str, guardian: bool = True) -> Path:
    folder = parent / name
    (folder / ("szu_guardian" if guardian else "other_app")).mkdir(parents=True)
    (folder / "python3.dll").write_bytes(b"x" * 2048)
    return folder


class CacheDirTests(unittest.TestCase):
    def test_validate_rejects_bad_paths_and_creates_good_ones(self):
        for bad in ("", "relative\\path", "%TEMP%\\x"):
            with self.assertRaises(ValueError):
                cache_dir.validate_cache_dir(bad)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "Apps" / "Cache" / "SZU-Guardian"
            self.assertEqual(cache_dir.validate_cache_dir(f'"{target}"'), target)
            self.assertTrue(target.is_dir())

    def test_cleanup_removes_only_stale_guardian_unpacks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stale = make_unpack(root, "_MEI1111")
            current = make_unpack(root, "_MEI2222")
            other = make_unpack(root, "_MEI3333", guardian=False)
            unrelated = root / "notes"
            unrelated.mkdir()

            removed, freed = cache_dir.cleanup_stale([root], current, min_age=0)

            self.assertEqual(removed, 1)
            self.assertGreaterEqual(freed, 2048)
            self.assertFalse(stale.exists())
            self.assertTrue(current.exists())
            self.assertTrue(other.exists())
            self.assertTrue(unrelated.exists())

    def test_cleanup_spares_folders_of_a_copy_that_is_still_starting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            starting = make_unpack(root, "_MEI4444")
            current = make_unpack(root, "_MEI2222")

            removed, _ = cache_dir.cleanup_stale([root], current)

            self.assertEqual(removed, 0)
            self.assertTrue(starting.exists())

            # Once it is old enough it counts as a leftover.
            with patch.object(cache_dir.time, "time", return_value=time.time() + 3600):
                removed, _ = cache_dir.cleanup_stale([root], current)
            self.assertEqual(removed, 1)
            self.assertFalse(starting.exists())

    def test_default_dir_is_project_folder(self):
        path = cache_dir.default_cache_dir()
        self.assertTrue(path.is_absolute())
        self.assertIn(path.name, (cache_dir.CACHE_FOLDER_NAME, "cache"))

    def test_project_first_layout(self):
        with patch.object(cache_dir, "_is_fixed_drive", return_value=True):
            self.assertEqual(cache_dir.default_cache_dir(), Path("E:/Apps/SZU-Guardian/Cache"))
            from szu_guardian import data_dir
            from szu_guardian.local_log import default_log_directory

            self.assertEqual(data_dir.default_data_dir(), Path("E:/Apps/SZU-Guardian/Config"))
            self.assertEqual(default_log_directory(), Path("E:/Apps/SZU-Guardian/Logs"))
        self.assertEqual(cache_dir.old_layout_dir("Cache"), Path("E:/Apps/Cache/SZU-Guardian"))

    def test_registered_old_cache_default_switches_to_new_layout(self):
        written = []
        with (
            patch.object(cache_dir, "registered_cache_dir", return_value=Path("E:/Apps/Cache/SZU-Guardian")),
            patch.object(cache_dir, "default_cache_dir", return_value=Path("E:/Apps/SZU-Guardian/Cache")),
            patch.object(cache_dir, "set_cache_dir", side_effect=lambda value: written.append(Path(value)) or Path(value)),
        ):
            self.assertEqual(cache_dir.ensure_registered(), Path("E:/Apps/SZU-Guardian/Cache"))
        self.assertEqual(written, [Path("E:/Apps/SZU-Guardian/Cache")])

    def test_without_a_usable_e_drive_the_profile_is_used(self):
        with patch.object(cache_dir, "_is_fixed_drive", return_value=False):
            self.assertEqual(cache_dir.default_cache_dir(), cache_dir.local_cache_dir())

    def test_unwritable_default_falls_back_to_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            good = Path(temp) / "profile-cache"
            registered = []

            def fake_set(value):
                if Path(value) != good:
                    raise ValueError("无法写入该文件夹")
                registered.append(Path(value))
                return Path(value)

            with (
                patch.object(cache_dir, "registered_cache_dir", return_value=None),
                patch.object(cache_dir, "default_cache_dir", return_value=Path("E:/locked")),
                patch.object(cache_dir, "local_cache_dir", return_value=good),
                patch.object(cache_dir, "set_cache_dir", side_effect=fake_set),
            ):
                self.assertEqual(cache_dir.ensure_registered(), good)
            self.assertEqual(registered, [good])


class LocalLogReadTests(unittest.TestCase):
    def test_read_day_parses_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            log = LocalLog(Path(temp))
            log.write("网络连接正常", "success")
            log.write("检测到校园网认证已失效", "warning")
            entries = log.read_day()
            self.assertEqual([m for _, m in entries], ["网络连接正常", "检测到校园网认证已失效"])
            self.assertEqual(entries[0][0].date(), dt.date.today())


class LogLocationTests(unittest.TestCase):
    def test_migration_moves_files_and_merges_same_day_in_time_order(self):
        from szu_guardian.local_log import migrate_logs

        with tempfile.TemporaryDirectory() as temp:
            old, new = Path(temp) / "old", Path(temp) / "new"
            old.mkdir()
            new.mkdir()
            (old / "guardian-2026-11-11.log").write_text("2026-11-11 09:00:00 [INFO] a\n", encoding="utf-8")
            (old / "guardian-2026-11-12.log").write_text(
                "2026-11-12 08:00:00 [INFO] early\n2026-11-12 10:00:00 [INFO] late-old\n", encoding="utf-8"
            )
            (new / "guardian-2026-11-12.log").write_text("2026-11-12 09:00:00 [INFO] middle\n", encoding="utf-8")

            self.assertEqual(migrate_logs(old, new), 2)

            self.assertFalse(old.exists())  # emptied and removed
            self.assertTrue((new / "guardian-2026-11-11.log").exists())
            merged = (new / "guardian-2026-11-12.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual([line.split("] ", 1)[1] for line in merged], ["early", "middle", "late-old"])

    def test_location_is_remembered(self):
        from szu_guardian.local_log import chosen_log_directory, save_log_location

        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            target = data / "MyLogs"
            save_log_location(data, target)
            self.assertEqual(chosen_log_directory(data), target)

    def test_logger_can_move_while_running(self):
        with tempfile.TemporaryDirectory() as temp:
            log = LocalLog(Path(temp) / "a")
            log.write("before")
            self.assertEqual(log.move_to(Path(temp) / "b"), 1)
            log.write("after")
            self.assertEqual([m for _, m in log.read_day()], ["before", "after"])


class SingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_second_launch_reaches_the_first(self):
        key = "SZUGuardianTest-" + uuid.uuid4().hex
        first = SingleInstance(key)
        self.assertFalse(first.notify_existing(timeout_ms=200))
        received = []
        self.assertTrue(first.listen(received.append))

        # The second launch is a separate process, exactly as in real use.
        project = str(Path(__file__).resolve().parents[1])
        script = (
            "import os,sys; os.environ['QT_QPA_PLATFORM']='offscreen'; "
            f"sys.path.insert(0, {project!r}); "
            "from PySide6.QtWidgets import QApplication; app=QApplication([]); "
            "from szu_guardian.single_instance import SingleInstance; "
            f"sys.exit(0 if SingleInstance({key!r}).notify_existing(b'show') else 3)"
        )
        second = subprocess.Popen([sys.executable, "-c", script])
        end = time.time() + 15
        while (second.poll() is None or not received) and time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

        self.assertEqual(second.returncode, 0)
        self.assertEqual(received, [b"show"])
        first.server.close()


if __name__ == "__main__":
    unittest.main()
