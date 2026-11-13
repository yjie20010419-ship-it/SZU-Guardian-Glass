import datetime as dt
import tempfile
import unittest
from pathlib import Path

from szu_guardian import run_marker
from szu_guardian.outage_stats import compute_day


class RunMarkerTests(unittest.TestCase):

    def test_clean_exit_is_not_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            self.assertFalse(run_marker.begin(folder))
            run_marker.end(folder)
            self.assertFalse((folder / run_marker.MARKER).exists())
            self.assertFalse(run_marker.begin(folder))

    def test_killed_run_is_reported_next_start(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            run_marker.begin(folder)  # process killed: end() never ran
            self.assertTrue(run_marker.begin(folder))

    def test_message_states_the_unguarded_gap(self):
        last = dt.datetime(2026, 11, 13, 3, 54)
        now = dt.datetime(2026, 11, 13, 11, 2)
        text = run_marker.unclean_message(last, now)
        self.assertIn('没有正常退出', text)
        self.assertIn('03:54', text)
        self.assertIn('7 小时 8 分钟', text)
        self.assertIn('11-12 23:50', run_marker.unclean_message(dt.datetime(2026, 11, 12, 23, 50), now))
        self.assertNotIn('记录在', run_marker.unclean_message(None, now))

    def test_notice_is_not_counted_as_a_network_outage(self):
        now = dt.datetime(2026, 11, 13, 11, 2)
        line = run_marker.unclean_message(dt.datetime(2026, 11, 13, 3, 54), now)
        stats = compute_day(now.date(), [(now, 'WARNING', line)])
        self.assertEqual(stats.count, 0)

    def test_unwritable_folder_does_not_raise(self):
        with tempfile.TemporaryDirectory() as temp:
            blocker = Path(temp) / 'file'
            blocker.write_text('x')
            self.assertFalse(run_marker.begin(blocker))  # parent is a file
            run_marker.end(blocker)


if __name__ == '__main__':
    unittest.main()
