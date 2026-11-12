import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from szu_guardian import data_dir
from szu_guardian.storage import ConfigStore, default_config_path


class DataDirTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.saved_env = os.environ.pop(data_dir.ENV_NAME, None)
        self.registry = {}
        patches = [
            patch.object(data_dir, "read_user_env", side_effect=lambda name: self.registry.get(name)),
            patch.object(data_dir, "write_user_env", side_effect=self._write_env),
            patch.object(data_dir, "legacy_data_dir", return_value=self.root / "LocalAppData" / "SZUNetworkGuardian"),
            patch.object(data_dir, "default_data_dir", return_value=self.root / "E" / "Apps" / "Config" / "SZU-Guardian"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        os.environ.pop(data_dir.ENV_NAME, None)
        if self.saved_env is not None:
            os.environ[data_dir.ENV_NAME] = self.saved_env
        self.temp.cleanup()

    def _write_env(self, name, value):
        self.registry[name] = value
        os.environ[name] = value

    def test_first_start_moves_old_settings_to_the_default_and_remembers_it(self):
        legacy = data_dir.legacy_data_dir()
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text("{}", encoding="utf-8")
        (legacy / "appearance.json").write_text("{}", encoding="utf-8")

        target, moved = data_dir.ensure_data_dir()

        self.assertEqual(target, data_dir.default_data_dir())
        self.assertEqual(sorted(moved), ["appearance.json", "config.json"])
        self.assertTrue((target / "config.json").exists())
        self.assertFalse(legacy.exists())  # emptied, so removed
        self.assertEqual(self.registry[data_dir.ENV_NAME], str(target))
        self.assertEqual(default_config_path(), target / "config.json")

    def test_user_choice_wins_and_files_follow(self):
        first, _ = data_dir.ensure_data_dir()
        ConfigStore(first / "config.json").path.write_text('{"username": "u"}', encoding="utf-8")
        chosen = self.root / "D" / "MySettings"

        target, moved = data_dir.set_data_dir(str(chosen), first)

        self.assertEqual(target, chosen)
        self.assertEqual(moved, ["config.json"])
        self.assertEqual(data_dir.current_data_dir(), chosen)
        self.assertEqual(data_dir.ensure_data_dir()[0], chosen)  # next start agrees

    def test_old_type_first_layout_moves_to_project_folder(self):
        old = self.root / "E" / "Apps" / "Config" / "SZU-Guardian"
        old.mkdir(parents=True)
        (old / "config.json").write_text("{}", encoding="utf-8")
        self.registry[data_dir.ENV_NAME] = str(old)  # what v1.0.2 saved
        new = self.root / "E" / "Apps" / "SZU-Guardian" / "Config"

        with (
            patch.object(data_dir, "old_layout_dir", return_value=old),
            patch.object(data_dir, "default_data_dir", return_value=new),
            patch.object(
                data_dir,
                "remove_old_layout",
                side_effect=lambda kind: (data_dir.remove_if_empty(old), data_dir.remove_if_empty(old.parent)),
            ),
        ):
            target, moved = data_dir.ensure_data_dir()

        self.assertEqual(target, new)
        self.assertEqual(moved, ["config.json"])
        self.assertTrue((new / "config.json").exists())
        self.assertFalse(old.parent.exists())  # E:\Apps\Config removed
        self.assertEqual(self.registry[data_dir.ENV_NAME], str(new))

    def test_existing_file_at_target_is_kept(self):
        source, target = self.root / "a", self.root / "b"
        source.mkdir()
        target.mkdir()
        (source / "config.json").write_text("old", encoding="utf-8")
        (target / "config.json").write_text("in use", encoding="utf-8")

        self.assertEqual(data_dir.migrate_data(source, target), [])
        self.assertEqual((target / "config.json").read_text(encoding="utf-8"), "in use")
        self.assertTrue((source / "config.json").exists())  # nothing lost


if __name__ == "__main__":
    unittest.main()
