import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from szu_guardian.runtime_environment import sanitize_external_environment

class RuntimeEnvironmentTests(unittest.TestCase):
    def test_external_programs_do_not_inherit_bundled_qt_plugins_or_dlls(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle=Path(temp)/'_MEI123'
            external=Path(temp)/'user-tools'
            values={'QT_PLUGIN_PATH':str(bundle/'PySide6'/'plugins'), 'QML2_IMPORT_PATH':str(bundle/'qml'), 'PATH':os.pathsep.join([str(bundle),str(external)]), 'UNCHANGED':'value'}
            with patch.dict(os.environ,values,clear=True):
                sanitize_external_environment(str(bundle))
                self.assertNotIn('QT_PLUGIN_PATH',os.environ)
                self.assertNotIn('QML2_IMPORT_PATH',os.environ)
                self.assertEqual(os.environ['PATH'],str(external))
                self.assertEqual(os.environ['UNCHANGED'],'value')
    def test_source_run_leaves_environment_unchanged(self):
        with patch.dict(os.environ,{'PATH':'something','QT_PLUGIN_PATH':'elsewhere'},clear=True):
            before=dict(os.environ); sanitize_external_environment(None); self.assertEqual(dict(os.environ),before)
