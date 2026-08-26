"""再生状態の永続化のテスト。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from chime.state import MAX_RECENT_QUOTES, State


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "cache", "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_starts_empty(self):
        state = State(self.path)
        self.assertFalse(state.is_fired("hourly:10", "2026-08-26"))
        self.assertEqual(state.recent_quotes(), [])

    def test_mark_fired_persists_across_restart(self):
        State(self.path).mark_fired("hourly:10", "2026-08-26")
        restarted = State(self.path)
        self.assertTrue(restarted.is_fired("hourly:10", "2026-08-26"))

    def test_a_new_day_resets_the_flag(self):
        state = State(self.path)
        state.mark_fired("hourly:10", "2026-08-26")
        self.assertFalse(state.is_fired("hourly:10", "2026-08-27"))

    def test_events_are_tracked_independently(self):
        state = State(self.path)
        state.mark_fired("hourly:10", "2026-08-26")
        self.assertFalse(state.is_fired("hourly:11", "2026-08-26"))
        self.assertFalse(state.is_fired("closing", "2026-08-26"))

    def test_last_fired_returns_the_recorded_day(self):
        state = State(self.path)
        state.mark_fired("closing", "2026-08-26")
        self.assertEqual(state.last_fired("closing"), "2026-08-26")
        self.assertIsNone(state.last_fired("hourly:10"))

    def test_creates_parent_directory(self):
        State(self.path).mark_fired("closing", "2026-08-26")
        self.assertTrue(os.path.exists(self.path))

    def test_quote_history_is_capped(self):
        state = State(self.path)
        for index in range(MAX_RECENT_QUOTES + 10):
            state.remember_quote("ひとこと{0}".format(index))
        recent = state.recent_quotes()
        self.assertEqual(len(recent), MAX_RECENT_QUOTES)
        self.assertEqual(recent[-1], "ひとこと{0}".format(MAX_RECENT_QUOTES + 9))

    def test_empty_quote_is_ignored(self):
        state = State(self.path)
        state.remember_quote("")
        self.assertEqual(state.recent_quotes(), [])

    def test_quote_history_persists(self):
        State(self.path).remember_quote("おつかれさまです。")
        self.assertEqual(State(self.path).recent_quotes(), ["おつかれさまです。"])

    def test_corrupted_file_is_ignored(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("これは JSON ではありません")
        state = State(self.path)
        self.assertEqual(state.recent_quotes(), [])
        self.assertFalse(state.is_fired("closing", "2026-08-26"))

    def test_unexpected_shape_is_ignored(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(["リストは想定外"], handle)
        self.assertEqual(State(self.path).recent_quotes(), [])

    def test_saved_file_is_readable_json(self):
        state = State(self.path)
        state.mark_fired("hourly:12", "2026-08-26")
        with open(self.path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["last_fired"]["hourly:12"], "2026-08-26")

    def test_no_temporary_file_is_left_behind(self):
        State(self.path).mark_fired("closing", "2026-08-26")
        directory = os.path.dirname(self.path)
        self.assertEqual([name for name in os.listdir(directory) if name.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
