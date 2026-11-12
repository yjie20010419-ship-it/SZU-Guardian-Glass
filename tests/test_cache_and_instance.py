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
            target = Path(temp) / "AppCache" / "SZU-Guardian"
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

            removed, freed = cache_dir.cleanup_stale([root], current)

            self.assertEqual(removed, 1)
            self.assertGreaterEqual(freed, 2048)
            self.assertFalse(stale.exists())
            self.assertTrue(current.exists())
            self.assertTrue(other.exists())
            self.assertTrue(unrelated.exists())

    def test_default_dir_is_project_folder(self):
        path = cache_dir.default_cache_dir()
        self.assertTrue(path.is_absolute())
        self.assertIn(path.name, (cache_dir.PROJECT_FOLDER, "cache"))


class LocalLogReadTests(unittest.TestCase):
    def test_read_day_parses_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            log = LocalLog(Path(temp))
            log.write("网络连接正常", "success")
            log.write("检测到校园网认证已失效", "warning")
            entries = log.read_day()
            self.assertEqual([m for _, m in entries], ["网络连接正常", "检测到校园网认证已失效"])
            self.assertEqual(entries[0][0].date(), dt.date.today())


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
