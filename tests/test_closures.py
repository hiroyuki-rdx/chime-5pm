"""休館日の暦（``chime.closures``）のテスト。

予定への反映（``Scheduler.events_for_date`` など）は ``tests/test_scheduler.py``。
ここでは、設定の読み取りと「どんな値でも例外にしない」ことを確かめる。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from chime.closures import Closure, ClosureCalendar
from tests.support import logs_enabled

TZ = ZoneInfo("Asia/Tokyo")


class _Lines(logging.Handler):
    """記録された行を ``ERROR:chime.closures:…`` の形で集める。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines = []

    def emit(self, record) -> None:
        self.lines.append("{0}:{1}:{2}".format(record.levelname, record.name, record.getMessage()))


def build(settings):
    """``ClosureCalendar`` を作り、作るときに残ったログの行と一緒に返す（外へは漏らさない）。"""
    logger = logging.getLogger("chime.closures")
    handler = _Lines()
    propagate = logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        with logs_enabled():
            calendar = ClosureCalendar(settings)
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
    return calendar, handler.lines


class ClosureTest(unittest.TestCase):
    def test_name_defaults_to_empty_and_the_value_is_frozen(self):
        closure = Closure(date(2026, 11, 2), date(2026, 11, 2))
        self.assertEqual(closure.name, "")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            closure.name = "学園祭"

    def test_covers_both_ends(self):
        closure = Closure(date(2026, 12, 28), date(2027, 1, 4), "年末年始")
        self.assertFalse(closure.covers(date(2026, 12, 27)))
        self.assertTrue(closure.covers(date(2026, 12, 28)))
        self.assertTrue(closure.covers(date(2027, 1, 1)))
        self.assertTrue(closure.covers(date(2027, 1, 4)))
        self.assertFalse(closure.covers(date(2027, 1, 5)))

    def test_reason_is_prefixed_with_the_name_only_when_there_is_one(self):
        self.assertEqual(Closure(date(2026, 11, 2), date(2026, 11, 2), "学園祭").reason,
                         "休館: 学園祭")
        self.assertEqual(Closure(date(2026, 11, 2), date(2026, 11, 2)).reason, "休館")


class NoClosuresTest(unittest.TestCase):
    def test_none_and_empty_settings_mean_no_closures_and_no_problems(self):
        for settings in (None, {}, {"dates": [], "ranges": []},
                         {"dates": None, "ranges": None}):
            with self.subTest(settings=settings):
                calendar, logged = build(settings)
                self.assertEqual(calendar.closures, [])
                self.assertEqual(calendar.problems, [])
                self.assertEqual(logged, [])
                self.assertIsNone(calendar.closed_reason(date(2026, 11, 2)))

    def test_unknown_keys_are_ignored(self):
        calendar, logged = build({"dates": ["2026-11-02"], "note": "メモ"})
        self.assertEqual(len(calendar.closures), 1)
        self.assertEqual((calendar.problems, logged), ([], []))


class DatesTest(unittest.TestCase):
    def test_plain_string_date(self):
        calendar, _ = build({"dates": ["2026-11-02"]})
        self.assertEqual(calendar.closures,
                         [Closure(date(2026, 11, 2), date(2026, 11, 2), "")])
        self.assertEqual(calendar.closed_reason(date(2026, 11, 2)), "休館")
        self.assertIsNone(calendar.closed_reason(date(2026, 11, 1)))
        self.assertIsNone(calendar.closed_reason(date(2026, 11, 3)))

    def test_named_date(self):
        calendar, _ = build({"dates": [{"date": "2026-11-02", "name": "学園祭"}]})
        self.assertEqual(calendar.closed_reason(date(2026, 11, 2)), "休館: 学園祭")

    def test_dict_without_a_name_is_unnamed(self):
        for entry in ({"date": "2026-11-02"}, {"date": "2026-11-02", "name": None},
                      {"date": "2026-11-02", "name": ""}, {"date": "2026-11-02", "name": "  "}):
            with self.subTest(entry=entry):
                calendar, _ = build({"dates": [entry]})
                self.assertEqual(calendar.closed_reason(date(2026, 11, 2)), "休館")

    def test_names_are_kept_on_one_line(self):
        calendar, _ = build({"dates": [{"date": "2026-11-02", "name": " 学園祭\n  準備日\t"}]})
        self.assertEqual(calendar.closed_reason(date(2026, 11, 2)), "休館: 学園祭 準備日")

    def test_mixed_forms_keep_their_order(self):
        calendar, _ = build({"dates": ["2026-11-02", {"date": "2026-11-03", "name": "振替"}]})
        self.assertEqual([closure.first for closure in calendar.closures],
                         [date(2026, 11, 2), date(2026, 11, 3)])

    def test_leap_day_is_accepted_only_in_a_leap_year(self):
        calendar, _ = build({"dates": ["2028-02-29"]})
        self.assertEqual(calendar.closed_reason(date(2028, 2, 29)), "休館")
        calendar, _ = build({"dates": ["2027-02-29"]})
        self.assertEqual(calendar.closures, [])
        self.assertEqual(len(calendar.problems), 1)

    def test_a_datetime_is_treated_as_its_date(self):
        calendar, _ = build({"dates": ["2026-11-02"]})
        self.assertEqual(calendar.closed_reason(datetime(2026, 11, 2, 23, 59, tzinfo=TZ)), "休館")
        self.assertIsNone(calendar.closed_reason(datetime(2026, 11, 3, 0, 0, tzinfo=TZ)))

    def test_the_first_matching_entry_wins(self):
        calendar, _ = build({"dates": [{"date": "2026-11-02", "name": "先"},
                                       {"date": "2026-11-02", "name": "後"}]})
        self.assertEqual(calendar.closed_reason(date(2026, 11, 2)), "休館: 先")


class RangesTest(unittest.TestCase):
    RANGE = {"ranges": [{"from": "2026-12-28", "to": "2027-01-04", "name": "年末年始"}]}

    def test_both_ends_are_included(self):
        calendar, _ = build(self.RANGE)
        self.assertIsNone(calendar.closed_reason(date(2026, 12, 27)))
        for day in (date(2026, 12, 28), date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 4)):
            with self.subTest(day=day):
                self.assertEqual(calendar.closed_reason(day), "休館: 年末年始")
        self.assertIsNone(calendar.closed_reason(date(2027, 1, 5)))

    def test_unnamed_range(self):
        calendar, _ = build({"ranges": [{"from": "2026-12-28", "to": "2026-12-29"}]})
        self.assertEqual(calendar.closed_reason(date(2026, 12, 29)), "休館")

    def test_a_one_day_range(self):
        calendar, _ = build({"ranges": [{"from": "2026-11-02", "to": "2026-11-02"}]})
        self.assertEqual(calendar.closed_reason(date(2026, 11, 2)), "休館")
        self.assertIsNone(calendar.closed_reason(date(2026, 11, 3)))
        self.assertEqual(calendar.problems, [])

    def test_the_whole_calendar_as_one_range_is_not_expanded(self):
        started = time.monotonic()
        calendar, _ = build({"ranges": [{"from": "0001-01-01", "to": "9999-12-31"}]})
        self.assertEqual(calendar.closed_reason(date(2026, 11, 2)), "休館")
        self.assertEqual(calendar.closed_reason(date.min), "休館")
        self.assertEqual(calendar.closed_reason(date.max), "休館")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_dates_come_before_ranges_and_each_keeps_the_written_order(self):
        calendar, _ = build({
            "ranges": [{"from": "2026-11-01", "to": "2026-11-10", "name": "長い"},
                       {"from": "2026-11-05", "to": "2026-11-05", "name": "短い"}],
            "dates": [{"date": "2026-11-07", "name": "単日"}],
        })
        self.assertEqual([closure.name for closure in calendar.closures], ["単日", "長い", "短い"])
        self.assertEqual(calendar.closed_reason(date(2026, 11, 7)), "休館: 単日")
        # 範囲どうしは書いた順。短いほうが後ろでも、先に書いた「長い」が当たる。
        self.assertEqual(calendar.closed_reason(date(2026, 11, 5)), "休館: 長い")

    def test_overlapping_ranges_fall_through_to_the_second(self):
        calendar, _ = build({"ranges": [{"from": "2026-11-01", "to": "2026-11-03", "name": "A"},
                                        {"from": "2026-11-02", "to": "2026-11-06", "name": "B"}]})
        self.assertEqual(calendar.closed_reason(date(2026, 11, 3)), "休館: A")
        self.assertEqual(calendar.closed_reason(date(2026, 11, 4)), "休館: B")


# (説明, 設定, 使えない項目の何番目か, 説明に含まれる言葉)
BAD_ENTRIES = (
    ("スラッシュ区切り", {"dates": ["2026/11/02"]}, 1, "YYYY-MM-DD"),
    ("月が 1 桁", {"dates": ["2026-1-2"]}, 1, "YYYY-MM-DD"),
    ("区切りなし", {"dates": ["20261102"]}, 1, "YYYY-MM-DD"),
    ("週の表記", {"dates": ["2026-W45-1"]}, 1, "YYYY-MM-DD"),
    ("前後の空白", {"dates": [" 2026-11-02"]}, 1, "YYYY-MM-DD"),
    ("末尾の改行", {"dates": ["2026-11-02\n"]}, 1, "YYYY-MM-DD"),
    ("全角の数字", {"dates": ["２０２６-１１-０２"]}, 1, "YYYY-MM-DD"),
    ("時刻つき", {"dates": ["2026-11-02T10:00"]}, 1, "YYYY-MM-DD"),
    ("空文字", {"dates": [""]}, 1, "YYYY-MM-DD"),
    ("ない日付（2 月 30 日）", {"dates": ["2026-02-30"]}, 1, "カレンダーにない"),
    ("ない日付（13 月）", {"dates": ["2026-13-01"]}, 1, "カレンダーにない"),
    ("0000 年", {"dates": ["0000-01-01"]}, 1, "カレンダーにない"),
    ("数値", {"dates": [20261102]}, 1, "書いてください"),
    ("真偽値", {"dates": [True]}, 1, "書いてください"),
    ("null", {"dates": [None]}, 1, "書いてください"),
    ("入れ子の一覧", {"dates": [["2026-11-02"]]}, 1, "書いてください"),
    ("date キーがない", {"dates": [{"name": "学園祭"}]}, 1, "\"date\" がありません"),
    ("date が文字でない", {"dates": [{"date": 20261102}]}, 1, "date は"),
    ("date が null", {"dates": [{"date": None}]}, 1, "date は"),
    ("date の書き方が違う", {"dates": [{"date": "11/02"}]}, 1, "date の書き方"),
    ("name が文字でない", {"dates": [{"date": "2026-11-02", "name": 7}]}, 1, "name は"),
    ("name が一覧", {"dates": [{"date": "2026-11-02", "name": ["学園祭"]}]}, 1, "name は"),
    ("ranges が文字列の項目", {"ranges": ["2026-12-28"]}, 1, "書いてください"),
    ("ranges が null の項目", {"ranges": [None]}, 1, "書いてください"),
    ("from がない", {"ranges": [{"to": "2026-12-28"}]}, 1, "\"from\" がありません"),
    ("to がない", {"ranges": [{"from": "2026-12-28"}]}, 1, "\"to\" がありません"),
    ("from が文字でない", {"ranges": [{"from": 1, "to": "2026-12-28"}]}, 1, "from は"),
    ("to の書き方が違う", {"ranges": [{"from": "2026-12-28", "to": "2027/01/04"}]}, 1, "to の書き方"),
    ("from が to より後", {"ranges": [{"from": "2027-01-04", "to": "2026-12-28"}]}, 1, "より後"),
    ("range の name が数値", {"ranges": [{"from": "2026-12-28", "to": "2026-12-29", "name": 1}]},
     1, "name は"),
)


class InvalidEntriesTest(unittest.TestCase):
    def test_each_bad_entry_is_ignored_and_explained(self):
        for label, settings, number, word in BAD_ENTRIES:
            with self.subTest(label):
                calendar, logged = build(settings)
                self.assertEqual(calendar.closures, [])
                self.assertEqual(len(calendar.problems), 1)
                problem = calendar.problems[0]
                key = "dates" if "dates" in settings else "ranges"
                self.assertIn("schedule.closures.{0} の {1} 番目".format(key, number), problem)
                self.assertIn(word, problem)
                self.assertIn("いつもどおり鳴らします", problem)
                self.assertIsNone(calendar.closed_reason(date(2026, 11, 2)))
                self.assertIsNone(calendar.closed_reason(date(2026, 12, 28)))

    def test_each_problem_is_logged_once_at_error(self):
        calendar, logged = build({"dates": ["2026/11/02", "2026-11-03", {"name": "x"}],
                                  "ranges": [{"from": "2027-01-04", "to": "2026-12-28"}]})
        self.assertEqual(len(calendar.problems), 3)
        self.assertEqual(len(logged), 3)
        for problem, line in zip(calendar.problems, logged):
            self.assertTrue(line.startswith("ERROR:chime.closures:"))
            self.assertIn(problem, line)

    def test_the_valid_entries_next_to_bad_ones_still_work(self):
        calendar, _ = build({
            "dates": ["2026-11-02", "2026/11/03", {"date": "2026-11-04", "name": "学園祭"}, None],
            "ranges": [{"from": "2026-12-28", "to": "2027-01-04", "name": "年末年始"},
                       {"from": "2027-02-02", "to": "2027-02-01"}],
        })
        self.assertEqual([closure.first for closure in calendar.closures],
                         [date(2026, 11, 2), date(2026, 11, 4), date(2026, 12, 28)])
        self.assertEqual(calendar.closed_reason(date(2026, 11, 4)), "休館: 学園祭")
        self.assertIsNone(calendar.closed_reason(date(2026, 11, 3)))
        # 番号は、書いた一覧の中での位置（1 始まり）。無視した項目も数える。
        self.assertEqual(len(calendar.problems), 3)
        self.assertIn("dates の 2 番目", calendar.problems[0])
        self.assertIn("dates の 4 番目", calendar.problems[1])
        self.assertIn("ranges の 2 番目", calendar.problems[2])

    def test_long_values_are_shortened_in_the_explanation(self):
        calendar, _ = build({"dates": ["x" * 500]})
        self.assertLess(len(calendar.problems[0]), 300)
        self.assertIn("…", calendar.problems[0])

    def test_a_bad_entry_does_not_hide_the_following_section(self):
        calendar, _ = build({"dates": 5, "ranges": [{"from": "2026-12-28", "to": "2026-12-29"}]})
        self.assertEqual(len(calendar.problems), 1)
        self.assertEqual(calendar.closed_reason(date(2026, 12, 28)), "休館")


class InvalidSectionsTest(unittest.TestCase):
    def test_a_section_that_is_not_an_object_is_ignored_and_explained(self):
        for settings in ([], ["2026-11-02"], "2026-11-02", "", 0, 7, 1.5, True, False):
            with self.subTest(settings=settings):
                calendar, logged = build(settings)
                self.assertEqual(calendar.closures, [])
                self.assertEqual(len(calendar.problems), 1)
                self.assertIn("schedule.closures は", calendar.problems[0])
                self.assertIn("いつもどおり鳴らします", calendar.problems[0])
                self.assertEqual(len(logged), 1)

    def test_dates_and_ranges_that_are_not_lists_are_ignored_and_explained(self):
        for key in ("dates", "ranges"):
            for value in ("2026-11-02", {"date": "2026-11-02"}, 5, True, 1.5, ""):
                with self.subTest(key=key, value=value):
                    calendar, logged = build({key: value})
                    self.assertEqual(calendar.closures, [])
                    self.assertEqual(len(calendar.problems), 1)
                    self.assertIn("schedule.closures.{0} は".format(key), calendar.problems[0])
                    self.assertEqual(len(logged), 1)


class ProblemWordingTest(unittest.TestCase):
    """使えない項目の説明の言い回し（読み手は、設定を手で直す現地の担当者）。"""

    def problem(self, settings):
        calendar, _ = build(settings)
        self.assertEqual(len(calendar.problems), 1, calendar.problems)
        return calendar.problems[0]

    def test_the_japanese_label_for_a_plain_date_follows_directly_without_a_space(self):
        self.assertIn("日付の書き方が正しくありません（\"2026/11/03\"）。YYYY-MM-DD の形で書いてください",
                      self.problem({"dates": ["2026/11/03"]}))
        self.assertIn("日付はカレンダーにない日付です（\"2026-02-30\"）",
                      self.problem({"dates": ["2026-02-30"]}))

    def test_no_japanese_label_is_followed_by_a_stray_space(self):
        for label, settings in (("日付", {"dates": ["2026/11/03"]}),
                                ("日付", {"dates": ["2026-13-01"]}),
                                ("日付", {"dates": [""]})):
            with self.subTest(label=label, settings=settings):
                text = self.problem(settings)
                self.assertNotIn(label + " ", text)
                self.assertNotRegex(text, "日付 [はの]")

    def test_english_keys_keep_the_space_before_the_particle(self):
        # date・from・to は、そのまま設定に書くキー。英字なので、前後を空白で区切って読みやすくする。
        for settings, expected in (
                ({"dates": [{"date": "11/02"}]}, "date の書き方が正しくありません"),
                ({"dates": [{"date": 5}]}, "date は"),
                ({"ranges": [{"from": 1, "to": "2026-12-28"}]}, "from は"),
                ({"ranges": [{"from": "2026-12-28", "to": "2027/01/04"}]}, "to の書き方が正しくありません"),
                ({"dates": [{"date": "2026-02-30"}]}, "date はカレンダーにない日付です")):
            with self.subTest(settings=settings):
                self.assertIn(expected, self.problem(settings))

    def test_a_name_that_is_not_text_gets_a_hint_about_the_name_not_about_the_date(self):
        for settings in ({"dates": [{"date": "2026-11-05", "name": 7}]},
                         {"dates": [{"date": "2026-11-05", "name": ["学園祭"]}]},
                         {"ranges": [{"from": "2026-12-28", "to": "2026-12-29", "name": True}]}):
            with self.subTest(settings=settings):
                text = self.problem(settings)
                self.assertIn("name は", text)
                self.assertIn("\"学園祭\" のように文字で書きます", text)
                self.assertIn("名前が要らなければ name ごと省いてください", text)
                # 日付の書き方の案内を繰り返さない（直す場所は name）。
                self.assertNotIn("YYYY-MM-DD", text)
                self.assertNotIn("2026-11-02", text)

    def test_a_bad_name_still_leaves_the_day_open(self):
        calendar, _ = build({"dates": [{"date": "2026-11-05", "name": 7}]})
        self.assertEqual(calendar.closures, [])
        self.assertIsNone(calendar.closed_reason(date(2026, 11, 5)))

    def test_every_problem_still_says_that_the_day_rings_as_usual(self):
        for label, settings, _number, _word in BAD_ENTRIES:
            with self.subTest(label):
                self.assertIn("いつもどおり鳴らします", self.problem(settings))


class NeverRaisesTest(unittest.TestCase):
    """どんな JSON の値を渡しても、作るときも尋ねるときも例外にしない。"""

    @staticmethod
    def json_values():
        scalars = [None, True, False, 0, 1, -1, 10 ** 400, 1.5, float("nan"), float("inf"),
                   "", " ", "2026-11-02", "2026-02-30", "x" * 10000, "\u0000", "日本語", "😀"]
        nested = [[], {}, [[]], [{}], {"": ""}, {"dates": {}}, {"ranges": {}}]
        deep = []
        for _ in range(200):
            deep = [deep]
        entries = scalars + nested + [deep]
        values = list(entries)
        for entry in entries:
            values.append({"dates": [entry], "ranges": [entry]})
            values.append({"dates": [{"date": entry, "name": entry}],
                           "ranges": [{"from": entry, "to": entry, "name": entry}]})
            values.append({"dates": entry, "ranges": entry})
        return values

    def test_every_json_value_is_accepted(self):
        for value in self.json_values():
            calendar, _ = build(value)
            self.assertIsInstance(calendar.closures, list)
            self.assertIsInstance(calendar.problems, list)
            for day in (date.min, date(2026, 11, 2), date.max):
                reason = calendar.closed_reason(day)
                self.assertTrue(reason is None or isinstance(reason, str))

    def test_values_read_back_from_real_json_text(self):
        text = ('{"dates": ["2026-11-02", 5, null, {"date": "2026-11-03"}, "NaN"],'
                ' "ranges": [{"from": "2026-12-28", "to": NaN}, {"from": "a"}, 3]}')
        calendar, _ = build(json.loads(text))
        self.assertEqual([closure.first for closure in calendar.closures],
                         [date(2026, 11, 2), date(2026, 11, 3)])
        self.assertEqual(len(calendar.problems), 6)

    def test_many_entries_stay_fast_to_look_up(self):
        dates = [date.fromordinal(date(2026, 1, 1).toordinal() + offset).isoformat()
                 for offset in range(3000)]
        calendar, _ = build({"dates": dates})
        started = time.monotonic()
        for offset in range(400):
            calendar.closed_reason(date.fromordinal(date(2026, 1, 1).toordinal() + offset))
        self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
