"""放送の履歴（``cache/history.jsonl``）のテスト。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from tests.support import logs_enabled

from chime.history import HISTORY_VERSION, KEEP_LINES, MAX_LINES, History, make_entry

TZ = ZoneInfo("Asia/Tokyo")
NOON = datetime(2026, 8, 26, 12, 0, tzinfo=TZ)


def entry(number: int = 0, **overrides) -> dict:
    """見分けやすい履歴 1 件（``number`` を ``warnings`` に入れる）。"""
    values = dict(at=NOON, key="hourly:12", kind="hourly", played=3, total=3,
                  warnings=["通し番号 {0}".format(number)])
    values.update(overrides)
    return make_entry(**values)


class MakeEntryTest(unittest.TestCase):
    def test_a_complete_broadcast(self):
        self.assertEqual(
            make_entry(at=NOON, key="hourly:12", kind="hourly", played=3, total=3),
            {"v": 1, "at": "2026-08-26T12:00:00+09:00", "day": "2026-08-26",
             "key": "hourly:12", "kind": "hourly", "result": "ok",
             "played": 3, "total": 3, "silent": [], "missing": [], "warnings": [],
             "degraded": False})

    def test_fields_are_in_a_stable_order(self):
        self.assertEqual(list(entry()), ["v", "at", "day", "key", "kind", "result", "played",
                                         "total", "silent", "missing", "warnings", "degraded"])
        self.assertEqual(list(entry(error="x"))[-1], "error")

    def test_version_constant(self):
        self.assertEqual(HISTORY_VERSION, 1)
        self.assertEqual(entry()["v"], HISTORY_VERSION)

    def test_at_has_a_utc_offset_and_no_fraction(self):
        moment = datetime(2026, 8, 26, 16, 57, 3, 123456, tzinfo=TZ)
        self.assertEqual(entry(at=moment)["at"], "2026-08-26T16:57:03+09:00")

    def test_day_is_the_local_date(self):
        # UTC では前日（08-25 15:30）でも、日付は設定のタイムゾーンの日付。
        moment = datetime(2026, 8, 26, 0, 30, tzinfo=TZ)
        self.assertEqual(entry(at=moment)["day"], "2026-08-26")
        self.assertEqual(entry(at=moment)["at"], "2026-08-26T00:30:00+09:00")

    def test_result_ok(self):
        self.assertEqual(entry(played=4, total=4)["result"], "ok")

    def test_result_partial_when_some_parts_were_not_played(self):
        self.assertEqual(entry(played=2, total=3)["result"], "partial")

    def test_result_partial_when_a_part_was_silent(self):
        made = entry(played=3, total=3, silent=["時報の音声（hourly:12）"])
        self.assertEqual(made["result"], "partial")
        self.assertEqual(made["silent"], ["時報の音声（hourly:12）"])

    def test_result_partial_when_a_required_part_could_not_be_added(self):
        # 欠けた部品は total に数えられない。played == total でも「すべて鳴った」ではない。
        made = entry(played=1, total=1, missing=["閉館アナウンス"])
        self.assertEqual(made["result"], "partial")
        self.assertEqual(made["missing"], ["閉館アナウンス"])

    def test_missing_is_empty_by_default_and_does_not_change_an_ok_result(self):
        made = entry(played=3, total=3)
        self.assertEqual(made["missing"], [])
        self.assertEqual(made["result"], "ok")
        self.assertEqual(entry(played=3, total=3, missing=[])["result"], "ok")

    def test_result_failed_when_nothing_was_played(self):
        self.assertEqual(entry(played=0, total=3)["result"], "failed")
        self.assertEqual(entry(played=0, total=0)["result"], "failed")

    def test_failed_and_error_take_precedence_over_missing(self):
        self.assertEqual(entry(played=0, total=0, missing=["蛍の光"])["result"], "failed")
        self.assertEqual(entry(played=1, total=1, missing=["蛍の光"], error="x")["result"], "error")

    def test_failed_takes_precedence_over_partial(self):
        self.assertEqual(entry(played=0, total=3, silent=["a"])["result"], "failed")

    def test_result_error_takes_precedence_over_everything(self):
        made = entry(played=0, total=3, silent=["a"], error="OSError: 再生できません")
        self.assertEqual(made["result"], "error")
        self.assertEqual(made["error"], "OSError: 再生できません")
        self.assertEqual(entry(played=3, total=3, error="途中で例外")["result"], "error")

    def test_error_key_only_when_given(self):
        self.assertNotIn("error", entry())
        self.assertNotIn("error", entry(error=None))
        self.assertIn("error", entry(error="x"))

    def test_empty_error_text_is_still_an_error(self):
        # str(例外) が空でも、例外で終わったことは記録する。
        made = entry(error="")
        self.assertEqual(made["result"], "error")
        self.assertEqual(made["error"], "")

    def test_degraded_is_recorded_as_a_bool(self):
        self.assertIs(entry(degraded=True)["degraded"], True)
        self.assertIs(entry()["degraded"], False)
        self.assertIs(entry(degraded=1)["degraded"], True)

    def test_silent_and_warnings_are_copied_into_lists(self):
        silent = ("a", "b")
        warnings = ["w1", "w2"]
        made = make_entry(at=NOON, key="closing", kind="closing", played=1, total=3,
                          silent=silent, warnings=warnings)
        self.assertEqual(made["silent"], ["a", "b"])
        warnings.append("後から足した")
        self.assertEqual(made["warnings"], ["w1", "w2"])

    def test_missing_is_copied_into_a_list(self):
        missing = ("閉館アナウンス", "蛍の光")
        self.assertEqual(entry(missing=missing)["missing"], ["閉館アナウンス", "蛍の光"])
        names = ["時報音"]
        made = entry(missing=names)
        names.append("後から足した")
        self.assertEqual(made["missing"], ["時報音"])

    def test_a_single_string_is_one_item_not_characters(self):
        made = entry(silent="音にならなかった部品", warnings="警告", missing="蛍の光")
        self.assertEqual(made["silent"], ["音にならなかった部品"])
        self.assertEqual(made["warnings"], ["警告"])
        self.assertEqual(made["missing"], ["蛍の光"])

    def test_entry_is_json_serializable(self):
        made = entry(silent=["閉館の放送"], error="例外")
        self.assertEqual(json.loads(json.dumps(made, ensure_ascii=False)), made)

    def test_arguments_are_keyword_only(self):
        with self.assertRaises(TypeError):
            make_entry(NOON, "hourly:12", "hourly", 3, 3)  # type: ignore[misc]


class HistoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = os.path.join(self.tmp.name, "cache")
        self.path = os.path.join(self.directory, "history.jsonl")

    def read_bytes(self) -> bytes:
        with open(self.path, "rb") as handle:
            return handle.read()

    def read_lines(self) -> list:
        return self.read_bytes().decode("utf-8").split("\n")[:-1]

    def numbers(self, entries) -> list:
        """``entry(number)`` で作った履歴の通し番号を取り出す。"""
        return [int(item["warnings"][0].split()[-1]) for item in entries]


class AppendTest(HistoryTestCase):
    def test_constants(self):
        self.assertEqual(MAX_LINES, 600)
        self.assertEqual(KEEP_LINES, 500)
        log = History(self.path)
        self.assertEqual((log.max_lines, log.keep_lines), (600, 500))

    def test_creating_the_object_touches_nothing(self):
        History(self.path)
        self.assertFalse(os.path.exists(self.directory))

    def test_creates_the_parent_directory_and_the_file(self):
        self.assertTrue(History(self.path).append(entry()))
        self.assertTrue(os.path.isfile(self.path))

    def test_writes_one_json_line_per_entry(self):
        log = History(self.path)
        first, second = entry(1), entry(2, key="closing", kind="closing")
        self.assertTrue(log.append(first))
        self.assertTrue(log.append(second))
        lines = self.read_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(line) for line in lines], [first, second])

    def test_japanese_is_not_escaped(self):
        History(self.path).append(entry(silent=["閉館の放送"]))
        raw = self.read_bytes()
        self.assertIn("閉館の放送".encode("utf-8"), raw)
        self.assertNotIn(b"\\u", raw)

    def test_newlines_inside_text_stay_on_one_line(self):
        log = History(self.path)
        log.append(entry(error="1 行目\n2 行目\r\n3 行目   4 行目"))
        self.assertEqual(len(self.read_lines()), 1)
        self.assertEqual(log.recent()[0]["error"], "1 行目\n2 行目\r\n3 行目   4 行目")

    def test_appends_to_an_existing_file(self):
        History(self.path).append(entry(1))
        History(self.path).append(entry(2))
        self.assertEqual(self.numbers(History(self.path).recent(10)), [2, 1])

    def test_a_relative_path_without_a_directory(self):
        old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, old)
        self.assertTrue(History("history.jsonl").append(entry()))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "history.jsonl")))

    def test_no_temporary_file_is_left_behind(self):
        log = History(self.path, max_lines=3, keep_lines=2)
        for number in range(8):
            log.append(entry(number))
        self.assertEqual([name for name in os.listdir(self.directory)
                          if name != "history.jsonl"], [])


class AppendFailureTest(HistoryTestCase):
    """書けなくても例外は出さず、``False`` と WARNING を返す。"""

    def append_logged(self, log, value):
        with logs_enabled(), self.assertLogs("chime.history", level="WARNING") as captured:
            result = log.append(value)
        self.assertEqual(len(captured.records), 1)
        return result, captured.records[0].getMessage()

    def test_directory_cannot_be_created(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("ファイルなのでディレクトリを作れない")
        log = History(os.path.join(blocker, "history.jsonl"))
        result, message = self.append_logged(log, entry())
        self.assertIs(result, False)
        self.assertIn("放送の履歴を書けません", message)
        self.assertIn(log.path, message)

    def test_path_is_a_directory(self):
        os.makedirs(self.path)
        result, message = self.append_logged(History(self.path), entry())
        self.assertIs(result, False)
        self.assertIn(self.path, message)

    def test_unserializable_value(self):
        result, message = self.append_logged(History(self.path), {"v": 1, "x": object()})
        self.assertIs(result, False)
        self.assertIn("object", message)
        self.assertFalse(os.path.exists(self.path))

    def test_circular_reference(self):
        loop = {"v": 1}
        loop["self"] = loop
        result, _ = self.append_logged(History(self.path), loop)
        self.assertIs(result, False)

    def test_nan_is_refused(self):
        # NaN は JSON として正しくないので、他の道具が読めなくなる行は書かない。
        result, _ = self.append_logged(History(self.path), {"v": 1, "x": float("nan")})
        self.assertIs(result, False)
        self.assertFalse(os.path.exists(self.path))

    def test_text_that_cannot_be_encoded(self):
        result, _ = self.append_logged(History(self.path), entry(error="\ud800"))
        self.assertIs(result, False)

    def test_not_a_mapping(self):
        for value in (None, "文字列", ["リスト"], 42):
            with self.subTest(value=value):
                result, _ = self.append_logged(History(self.path), value)
                self.assertIs(result, False)
        self.assertFalse(os.path.exists(self.path))

    def test_too_deeply_nested(self):
        # 入れ子の深さで RecursionError になる限界は、Python の版で違う（3.12 以降は
        # C の JSON エンコーダーが別の限界を持ち、5000 段でも通る）。限界に頼らず、
        # エンコーダーが RecursionError を出したことにして確かめる。
        with mock.patch("chime.history.json.dumps", side_effect=RecursionError("深すぎる")):
            result, message = self.append_logged(History(self.path), entry())
        self.assertIs(result, False)
        self.assertIn("放送の履歴を書けません", message)
        self.assertFalse(os.path.exists(self.path))

    def test_a_really_deep_structure_never_raises(self):
        # 本物の深い入れ子。書けるか書けないかは版しだいなので、どちらでもよいが、
        # 例外は出さず、書けなかったなら何も書かない（書けたなら 1 行だけ）。
        nested = current = {"v": 1}
        for _ in range(200000):
            current["x"] = {}
            current = current["x"]
        result = History(self.path).append(nested)
        self.assertIsInstance(result, bool)
        self.assertEqual(os.path.exists(self.path), result)
        if result:
            self.assertEqual(len(self.read_lines()), 1)

    def test_a_failed_append_leaves_the_earlier_lines(self):
        log = History(self.path)
        log.append(entry(1))
        with logs_enabled(), self.assertLogs("chime.history", level="WARNING"):
            log.append({"v": 1, "x": object()})
        self.assertEqual(self.numbers(log.recent()), [1])

    def test_the_next_append_works_after_a_failure(self):
        log = History(self.path)
        with logs_enabled(), self.assertLogs("chime.history", level="WARNING"):
            log.append(None)
        self.assertTrue(log.append(entry(1)))
        self.assertEqual(self.numbers(log.recent()), [1])


class TornLineTest(HistoryTestCase):
    """電源断などで最後の行が途中で切れていても、次の行を巻き込まない。"""

    def write_raw(self, data: bytes) -> None:
        os.makedirs(self.directory, exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(data)

    def test_new_entry_is_not_glued_to_a_torn_line(self):
        good = json.dumps(entry(1), ensure_ascii=False)
        self.write_raw((good + "\n" + good[:40]).encode("utf-8"))
        log = History(self.path)
        self.assertTrue(log.append(entry(2)))
        self.assertEqual(self.numbers(log.recent(10)), [2, 1])

    def test_a_complete_last_line_without_a_newline(self):
        self.write_raw(json.dumps(entry(1), ensure_ascii=False).encode("utf-8"))
        log = History(self.path)
        log.append(entry(2))
        self.assertEqual(self.numbers(log.recent(10)), [2, 1])
        self.assertEqual(len(self.read_lines()), 2)

    def test_no_blank_line_is_added_when_the_file_ends_properly(self):
        log = History(self.path)
        log.append(entry(1))
        log.append(entry(2))
        self.assertEqual(self.read_bytes().count(b"\n"), 2)
        self.assertNotIn(b"\n\n", self.read_bytes())

    def test_an_empty_existing_file(self):
        self.write_raw(b"")
        self.assertTrue(History(self.path).append(entry(1)))
        self.assertEqual(self.read_bytes().count(b"\n"), 1)
        self.assertFalse(self.read_bytes().startswith(b"\n"))


class TrimTest(HistoryTestCase):
    def test_stays_as_is_up_to_max_lines(self):
        log = History(self.path, max_lines=5, keep_lines=3)
        for number in range(5):
            log.append(entry(number))
        self.assertEqual(self.numbers(log.recent(100)), [4, 3, 2, 1, 0])

    def test_over_max_lines_keeps_only_the_newest_keep_lines(self):
        log = History(self.path, max_lines=5, keep_lines=3)
        for number in range(6):
            log.append(entry(number))
        self.assertEqual(self.numbers(log.recent(100)), [5, 4, 3])
        self.assertEqual(len(self.read_lines()), 3)

    def test_keeps_growing_again_after_a_trim(self):
        log = History(self.path, max_lines=5, keep_lines=3)
        for number in range(8):
            log.append(entry(number))
        self.assertEqual(self.numbers(log.recent(100)), [7, 6, 5, 4, 3])
        log.append(entry(8))
        self.assertEqual(self.numbers(log.recent(100)), [8, 7, 6])

    def test_default_limits(self):
        os.makedirs(self.directory)
        with open(self.path, "w", encoding="utf-8") as handle:
            for number in range(MAX_LINES):
                handle.write(json.dumps(entry(number), ensure_ascii=False) + "\n")
        log = History(self.path)
        self.assertEqual(len(self.read_lines()), MAX_LINES)
        log.append(entry(MAX_LINES))
        lines = self.read_lines()
        self.assertEqual(len(lines), KEEP_LINES)
        self.assertEqual(self.numbers([json.loads(lines[0])]), [MAX_LINES + 1 - KEEP_LINES])
        self.assertEqual(self.numbers(log.recent(1)), [MAX_LINES])

    def test_file_ends_with_a_newline_after_a_trim(self):
        log = History(self.path, max_lines=3, keep_lines=2)
        for number in range(4):
            log.append(entry(number))
        self.assertTrue(self.read_bytes().endswith(b"\n"))
        self.assertNotIn(b"\n\n", self.read_bytes())

    def test_blank_lines_are_dropped_by_a_trim(self):
        os.makedirs(self.directory)
        with open(self.path, "w", encoding="utf-8") as handle:
            for number in range(4):
                handle.write(json.dumps(entry(number)) + "\n\n   \n")
        log = History(self.path, max_lines=4, keep_lines=3)
        log.append(entry(4))
        self.assertEqual(self.numbers(log.recent(100)), [4, 3, 2])
        self.assertEqual(len(self.read_lines()), 3)

    def test_keep_lines_larger_than_max_lines_is_capped(self):
        log = History(self.path, max_lines=3, keep_lines=10)
        self.assertEqual((log.max_lines, log.keep_lines), (3, 3))
        for number in range(5):
            log.append(entry(number))
        self.assertEqual(self.numbers(log.recent(100)), [4, 3, 2])

    def test_nonsense_limits_are_raised_to_one(self):
        log = History(self.path, max_lines=0, keep_lines=0)
        self.assertEqual((log.max_lines, log.keep_lines), (1, 1))
        log.append(entry(1))
        log.append(entry(2))
        self.assertEqual(self.numbers(log.recent(100)), [2])

    def test_trim_failure_keeps_the_original_and_still_reports_success(self):
        log = History(self.path, max_lines=3, keep_lines=2)
        for number in range(3):
            log.append(entry(number))
        with mock.patch("chime.history.os.replace", side_effect=OSError("置き換えられません")):
            with logs_enabled(), self.assertLogs("chime.history", level="WARNING") as captured:
                result = log.append(entry(3))
        self.assertIs(result, True)
        self.assertIn("整理できません", captured.records[0].getMessage())
        # 元のファイルはそのまま（追記までは済んでいる）、一時ファイルも残らない。
        self.assertEqual(self.numbers(log.recent(100)), [3, 2, 1, 0])
        self.assertEqual(os.listdir(self.directory), ["history.jsonl"])


class RecentTest(HistoryTestCase):
    def write_lines(self, *lines) -> None:
        os.makedirs(self.directory, exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(b"".join(
                (line if isinstance(line, bytes) else line.encode("utf-8")) + b"\n"
                for line in lines))

    def dump(self, number: int, **changes) -> str:
        item = entry(number)
        item.update(changes)
        return json.dumps(item, ensure_ascii=False)

    def test_missing_file(self):
        self.assertEqual(History(self.path).recent(), [])
        self.assertFalse(os.path.exists(self.directory))

    def test_empty_file(self):
        os.makedirs(self.directory)
        open(self.path, "w").close()
        self.assertEqual(History(self.path).recent(), [])

    def test_newest_first(self):
        log = History(self.path)
        for number in range(4):
            log.append(entry(number))
        self.assertEqual(self.numbers(log.recent(10)), [3, 2, 1, 0])

    def test_default_limit_is_eight(self):
        log = History(self.path)
        for number in range(12):
            log.append(entry(number))
        self.assertEqual(self.numbers(log.recent()), [11, 10, 9, 8, 7, 6, 5, 4])

    def test_limit(self):
        log = History(self.path)
        for number in range(5):
            log.append(entry(number))
        self.assertEqual(self.numbers(log.recent(2)), [4, 3])
        self.assertEqual(self.numbers(log.recent(1)), [4])
        self.assertEqual(len(log.recent(99)), 5)

    def test_zero_or_negative_limit(self):
        log = History(self.path)
        log.append(entry())
        self.assertEqual(log.recent(0), [])
        self.assertEqual(log.recent(-3), [])

    def test_round_trip_returns_what_was_written(self):
        made = entry(1, silent=["閉館の放送"], degraded=True, error="例外")
        log = History(self.path)
        log.append(made)
        self.assertEqual(log.recent(), [made])

    def test_blank_lines_are_skipped(self):
        self.write_lines(self.dump(1), "", "   ", self.dump(2), "")
        self.assertEqual(self.numbers(History(self.path).recent()), [2, 1])

    def test_broken_lines_are_skipped(self):
        self.write_lines(self.dump(1), "これは JSON ではありません", self.dump(2)[:30],
                         '{"v": 1,', "}{", self.dump(3))
        self.assertEqual(self.numbers(History(self.path).recent()), [3, 1])

    def test_a_line_without_missing_is_read_as_it_is(self):
        # missing は後から足した項目（HISTORY_VERSION は 1 のまま）。足す前の行も読める。
        old = entry(1)
        del old["missing"]
        self.write_lines(json.dumps(old, ensure_ascii=False), self.dump(2))
        found = History(self.path).recent()
        self.assertEqual(self.numbers(found), [2, 1])
        self.assertNotIn("missing", found[1])
        self.assertEqual(found[0]["missing"], [])

    def test_lines_that_are_not_objects_are_skipped(self):
        self.write_lines(self.dump(1), "[1, 2]", '"文字列"', "42", "null", "true", self.dump(2))
        self.assertEqual(self.numbers(History(self.path).recent()), [2, 1])

    def test_unknown_versions_are_skipped(self):
        self.write_lines(self.dump(1), self.dump(2, v=2), self.dump(3, v=0),
                         self.dump(4, v="1"), self.dump(5, v=True), self.dump(6, v=1.5),
                         self.dump(7, v=None), self.dump(8, v=[1]), self.dump(9))
        self.assertEqual(self.numbers(History(self.path).recent(100)), [9, 1])

    def test_missing_version_is_skipped(self):
        without = entry(2)
        del without["v"]
        self.write_lines(self.dump(1), json.dumps(without), self.dump(3))
        self.assertEqual(self.numbers(History(self.path).recent()), [3, 1])

    def test_lines_that_are_not_utf8_are_skipped(self):
        self.write_lines(self.dump(1), self.dump(2).replace("通し番号", "閉館").encode("shift_jis"),
                         b"\xff\xfe\x00", self.dump(3))
        self.assertEqual(self.numbers(History(self.path).recent()), [3, 1])

    def test_deeply_nested_garbage_is_skipped(self):
        self.write_lines(self.dump(1), "[" * 100000, self.dump(2))
        self.assertEqual(self.numbers(History(self.path).recent()), [2, 1])

    def test_a_line_that_overflows_the_json_decoder_is_skipped(self):
        # 本物の深い入れ子は、版によって RecursionError か JSONDecodeError かが違う。
        # どちらの経路でも飛ばせるよう、RecursionError そのものも確かめる。
        self.write_lines(self.dump(1), self.dump(2), self.dump(3))
        real_loads = json.loads

        def loads(text, *args, **kwargs):
            if "通し番号 2" in text:
                raise RecursionError("深すぎる")
            return real_loads(text, *args, **kwargs)

        with mock.patch("chime.history.json.loads", loads):
            self.assertEqual(self.numbers(History(self.path).recent()), [3, 1])

    def test_skipped_lines_do_not_use_up_the_limit(self):
        self.write_lines(self.dump(1), self.dump(2), "壊れた行", "", self.dump(3, v=9), "[]")
        self.assertEqual(self.numbers(History(self.path).recent(2)), [2, 1])

    def test_file_with_only_garbage(self):
        self.write_lines("壊れた行", "[]", "", "{")
        self.assertEqual(History(self.path).recent(), [])

    def test_windows_line_endings(self):
        os.makedirs(self.directory)
        with open(self.path, "wb") as handle:
            handle.write((self.dump(1) + "\r\n" + self.dump(2) + "\r\n").encode("utf-8"))
        self.assertEqual(self.numbers(History(self.path).recent()), [2, 1])

    def test_a_bare_carriage_return_does_not_split_a_line(self):
        # 行の区切りは \n だけ。\r だけでは割らない（割ると、2 件が 1 行にくっついた
        # 壊れた行を、きれいな 2 件として読んでしまう）。
        os.makedirs(self.directory)
        first, second = entry(1), entry(2)
        with open(self.path, "wb") as handle:
            handle.write(json.dumps(first).encode("utf-8") + b"\r"
                         + json.dumps(second).encode("utf-8") + b"\n")
        self.assertEqual(History(self.path).recent(), [])

    def test_unicode_line_separators_do_not_split_an_entry(self):
        self.write_lines(self.dump(1, error="前 後 さらに\x85続き"), self.dump(2))
        found = History(self.path).recent()
        self.assertEqual(self.numbers(found), [2, 1])
        self.assertEqual(found[1]["error"], "前 後 さらに\x85続き")

    def test_unreadable_path_returns_nothing_and_warns(self):
        os.makedirs(self.path)
        with logs_enabled(), self.assertLogs("chime.history", level="WARNING") as captured:
            found = History(self.path).recent()
        self.assertEqual(found, [])
        self.assertIn("放送の履歴を読めません", captured.records[0].getMessage())
        self.assertIn(self.path, captured.records[0].getMessage())

    def test_parent_is_a_file(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("ファイル")
        with logs_enabled(), self.assertLogs("chime.history", level="WARNING"):
            self.assertEqual(History(os.path.join(blocker, "history.jsonl")).recent(), [])

    def test_reading_never_changes_the_file(self):
        self.write_lines(self.dump(1), "壊れた行", "", self.dump(2))
        before = self.read_bytes()
        History(self.path).recent()
        self.assertEqual(self.read_bytes(), before)
        self.assertEqual(os.listdir(self.directory), ["history.jsonl"])

    def test_missing_file_is_not_a_warning(self):
        with logs_enabled(), self.assertLogs("chime.history", level="WARNING") as captured:
            logging.getLogger("chime.history").warning("dummy")
            History(self.path).recent()
        self.assertEqual(len(captured.records), 1)


if __name__ == "__main__":
    unittest.main()
