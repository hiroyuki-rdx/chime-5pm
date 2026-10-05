"""再生状態の永続化のテスト。"""

from __future__ import annotations

import json
import logging
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

    def test_is_fired_matches_only_the_recorded_key_and_day(self):
        state = State(self.path)
        state.mark_fired("closing", "2026-08-26")
        self.assertTrue(state.is_fired("closing", "2026-08-26"))
        self.assertFalse(state.is_fired("closing", "2026-08-25"))
        self.assertFalse(state.is_fired("hourly:10", "2026-08-26"))

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

    def test_non_utf8_file_is_ignored_and_the_service_carries_on(self):
        # UTF-8 として読めない状態ファイルで、起動時に落ちて systemd の再起動を
        # 繰り返してはいけない。初期化して続行し、次の保存で正しい形に直る。
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write('{"last_fired": {"closing": "2026-08-26"}}'.replace(
                "closing", "閉館").encode("shift_jis"))
        state = State(self.path)
        self.assertEqual(state.recent_quotes(), [])
        self.assertFalse(state.is_fired("閉館", "2026-08-26"))
        state.mark_fired("closing", "2026-08-27")
        self.assertTrue(State(self.path).is_fired("closing", "2026-08-27"))

    def test_utf8_bom_file_is_readable(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + json.dumps(
                {"last_fired": {"closing": "2026-08-26"}, "recent_quotes": ["おつかれさま"]},
                ensure_ascii=False).encode("utf-8"))
        state = State(self.path)
        self.assertTrue(state.is_fired("closing", "2026-08-26"))
        self.assertEqual(state.recent_quotes(), ["おつかれさま"])

    def test_unreadable_file_is_reported_with_guidance(self):
        # 初期化して続行するだけでなく、なぜ初期化されたかを WARNING で残す。
        # tests/__init__.py がログを抑制しているため、tests/test_sequence.py と
        # 同じ手順でこのテストの間だけ一時的に解除する。
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write("閉館".encode("shift_jis"))
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("chime.state", level="WARNING") as captured:
                State(self.path)
        finally:
            logging.disable(logging.CRITICAL)
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertIn("初期化します", message)
        self.assertIn(self.path, message)
        self.assertIn("UTF-8", message)

    def test_broken_json_is_reported_with_the_position(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"last_fired": {}\n"recent_quotes": []}')
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("chime.state", level="WARNING") as captured:
                State(self.path)
        finally:
            logging.disable(logging.CRITICAL)
        self.assertIn("2 行 1 文字目", captured.records[0].getMessage())

    def test_missing_file_is_not_a_warning(self):
        # 初回起動では状態ファイルが無いのが普通。警告は出さない。
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("chime.state", level="WARNING") as captured:
                logging.getLogger("chime.state").warning("dummy")
                State(self.path)
        finally:
            logging.disable(logging.CRITICAL)
        self.assertEqual(len(captured.records), 1)

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

    def test_save_failure_is_logged_and_does_not_stop_the_broadcast(self):
        # 保存先を作れなくても、放送は続ける（記録がメモリ上に残るだけ）。
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("ファイルなのでディレクトリを作れない")
        state = State(os.path.join(blocker, "state.json"))
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("chime.state", level="ERROR") as captured:
                state.mark_fired("closing", "2026-08-26")
        finally:
            logging.disable(logging.CRITICAL)
        self.assertIn("状態ファイルを保存できません", captured.records[0].getMessage())
        self.assertTrue(state.is_fired("closing", "2026-08-26"))

    def test_no_temporary_file_is_left_behind(self):
        State(self.path).mark_fired("closing", "2026-08-26")
        directory = os.path.dirname(self.path)
        self.assertEqual([name for name in os.listdir(directory) if name.endswith(".tmp")], [])


class ReadOnlyStateTest(unittest.TestCase):
    """``read_only=True`` の State は、読み込むだけでファイルに一切書かない。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = os.path.join(self.tmp.name, "cache")
        self.path = os.path.join(self.directory, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_writable_by_default(self):
        self.assertFalse(State(self.path).read_only)
        self.assertTrue(State(self.path, read_only=True).read_only)

    def test_does_not_create_the_file_or_the_directory(self):
        state = State(self.path, read_only=True)
        state.mark_fired("hourly:10", "2026-08-26")
        state.remember_quote("おつかれさまです。")
        state.save()
        self.assertFalse(os.path.exists(self.directory))

    def test_records_stay_in_memory(self):
        state = State(self.path, read_only=True)
        state.mark_fired("hourly:10", "2026-08-26")
        state.remember_quote("おつかれさまです。")
        self.assertTrue(state.is_fired("hourly:10", "2026-08-26"))
        self.assertEqual(state.recent_quotes(), ["おつかれさまです。"])

    def test_records_are_not_visible_to_the_next_start(self):
        State(self.path, read_only=True).mark_fired("hourly:10", "2026-08-26")
        self.assertFalse(State(self.path).is_fired("hourly:10", "2026-08-26"))

    def test_leaves_an_existing_file_untouched(self):
        State(self.path).mark_fired("closing", "2026-08-25")
        with open(self.path, "rb") as handle:
            before = handle.read()

        state = State(self.path, read_only=True)
        state.mark_fired("closing", "2026-08-26")
        state.remember_quote("おつかれさまです。")

        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual([name for name in os.listdir(self.directory)], ["state.json"])

    def test_still_reads_the_existing_file(self):
        State(self.path).mark_fired("closing", "2026-08-25")
        state = State(self.path, read_only=True)
        self.assertTrue(state.is_fired("closing", "2026-08-25"))

    def test_does_not_overwrite_an_unreadable_file(self):
        # 読めなかったファイルを、動作確認の実行が「初期化して上書き」してはいけない。
        os.makedirs(self.directory)
        broken = "閉館".encode("shift_jis")
        with open(self.path, "wb") as handle:
            handle.write(broken)
        State(self.path, read_only=True).mark_fired("closing", "2026-08-26")
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), broken)


if __name__ == "__main__":
    unittest.main()
