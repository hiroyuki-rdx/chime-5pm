"""「今日だけ止める」の記録（chime/pause.py）のテスト。"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from tests.support import logs_enabled

from chime.jsonfile import read_json
from chime.pause import MAX_PAUSE_BYTES, MAX_PAUSE_DAYS, PAUSE_VERSION, Pause, PauseStore

JST = timezone(timedelta(hours=9))
TODAY = date(2026, 10, 13)
#: ``valid_data`` から取り除く項目の目印。
DROP = object()


class PauseTestCase(unittest.TestCase):
    """一時フォルダの ``cache/pause.json`` と、動かせる「今日」を用意する。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = os.path.join(self.tmp.name, "cache")
        self.path = os.path.join(self.directory, "pause.json")
        self.today = TODAY
        self.store = PauseStore(self.path, lambda: self.today)

    # -- 補助 -----------------------------------------------------------
    def valid_data(self, **overrides):
        """読める記録の辞書（``DROP`` を渡した項目は取り除く）。"""
        data = {"v": 1, "first_day": "2026-10-13", "last_day": "2026-10-15",
                "set_at": "2026-10-13T07:30:00+09:00", "reason": "学園祭の準備"}
        data.update(overrides)
        return {key: value for key, value in data.items() if value is not DROP}

    def write_raw(self, content):
        """``content``（文字列かバイト列）をそのままファイルに書く。"""
        os.makedirs(self.directory, exist_ok=True)
        if isinstance(content, bytes):
            with open(self.path, "wb") as handle:
                handle.write(content)
        else:
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(content)

    def write_data(self, **overrides):
        self.write_raw(json.dumps(self.valid_data(**overrides), ensure_ascii=False))

    def read_saved(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def day(self, offset):
        """今日から ``offset`` 日あとの日付。"""
        return self.today + timedelta(days=offset)


class ConstantsTest(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(PAUSE_VERSION, 1)
        self.assertEqual(MAX_PAUSE_DAYS, 31)
        self.assertEqual(MAX_PAUSE_BYTES, 1024 * 1024)


class PauseValueTest(unittest.TestCase):
    def make(self):
        return Pause(first_day=date(2026, 10, 13), last_day=date(2026, 10, 15),
                     set_at=datetime(2026, 10, 13, 7, 30, tzinfo=JST), reason="学園祭")

    def test_is_immutable(self):
        pause = self.make()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            pause.last_day = date(2026, 10, 20)  # type: ignore[misc]

    def test_covers_both_ends_inclusive(self):
        pause = self.make()
        self.assertFalse(pause.covers(date(2026, 10, 12)))
        self.assertTrue(pause.covers(date(2026, 10, 13)))
        self.assertTrue(pause.covers(date(2026, 10, 14)))
        self.assertTrue(pause.covers(date(2026, 10, 15)))
        self.assertFalse(pause.covers(date(2026, 10, 16)))


class SetAndCurrentTest(PauseTestCase):
    def test_no_file_means_no_pause(self):
        self.assertIsNone(self.store.current())
        self.assertFalse(self.store.covers(self.today))

    def test_missing_file_is_not_a_warning(self):
        # 休止の記録が無いのが普通。警告は出さない。
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            logging.getLogger("chime.pause").warning("dummy")
            self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

    def test_round_trip(self):
        now = datetime(2026, 10, 13, 7, 30, 12, tzinfo=JST)
        made = self.store.set(self.day(2), "学園祭の準備", now=now)
        self.assertEqual(made, Pause(first_day=TODAY, last_day=self.day(2),
                                     set_at=now, reason="学園祭の準備"))
        self.assertEqual(self.store.current(), made)
        # 別のプロセス（常駐側）が同じファイルを読んでも同じ。
        self.assertEqual(PauseStore(self.path, lambda: self.today).current(), made)

    def test_saved_file_has_the_documented_shape(self):
        now = datetime(2026, 10, 13, 7, 30, 12, tzinfo=JST)
        self.store.set(self.day(2), "学園祭の準備", now=now)
        self.assertEqual(self.read_saved(), {
            "v": 1, "first_day": "2026-10-13", "last_day": "2026-10-15",
            "set_at": "2026-10-13T07:30:12+09:00", "reason": "学園祭の準備"})

    def test_reason_defaults_to_empty(self):
        made = self.store.set(self.day(0))
        self.assertEqual(made.reason, "")
        self.assertEqual(self.read_saved()["reason"], "")

    def test_creates_the_parent_directory(self):
        self.assertFalse(os.path.exists(self.directory))
        self.store.set(self.day(0))
        self.assertTrue(os.path.isfile(self.path))

    def test_one_day_pause_covers_today_only(self):
        self.store.set(self.day(0))
        self.assertTrue(self.store.covers(self.today))
        self.assertFalse(self.store.covers(self.day(1)))
        self.assertFalse(self.store.covers(self.day(-1)))

    def test_covers_follows_the_pause_period(self):
        self.store.set(self.day(2))
        self.assertFalse(self.store.covers(self.day(-1)))
        self.assertTrue(self.store.covers(self.day(0)))
        self.assertTrue(self.store.covers(self.day(1)))
        self.assertTrue(self.store.covers(self.day(2)))
        self.assertFalse(self.store.covers(self.day(3)))

    def test_set_replaces_an_existing_pause(self):
        self.store.set(self.day(5), "最初")
        made = self.store.set(self.day(1), "短くした")
        self.assertEqual(self.store.current(), made)
        self.assertEqual(made.last_day, self.day(1))
        self.assertEqual(made.reason, "短くした")

    def test_first_day_is_always_today(self):
        self.today = date(2026, 11, 2)
        made = self.store.set(date(2026, 11, 4))
        self.assertEqual(made.first_day, date(2026, 11, 2))

    def test_no_temporary_file_is_left_behind(self):
        self.store.set(self.day(1), "確認")
        self.assertEqual(os.listdir(self.directory), ["pause.json"])

    def test_write_failure_is_raised_for_the_caller_to_report(self):
        # 書けなかったことを黙って「止めた」ことにしない（呼び出し側が利用者に伝える）。
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("ファイルなのでディレクトリを作れない")
        store = PauseStore(os.path.join(blocker, "pause.json"), lambda: self.today)
        with self.assertRaises(OSError):
            store.set(self.day(1))
        self.assertIsNone(store.current())


class SetAtTest(PauseTestCase):
    def test_default_set_at_has_a_utc_offset(self):
        made = self.store.set(self.day(0))
        self.assertIsNotNone(made.set_at.utcoffset())
        # ファイルの文字列にもオフセットが付き、読み戻した値と一致する。
        saved = self.read_saved()["set_at"]
        self.assertIsNotNone(datetime.fromisoformat(saved).utcoffset())
        self.assertEqual(self.store.current().set_at, made.set_at)

    def test_given_offset_is_kept(self):
        now = datetime(2026, 10, 13, 7, 30, tzinfo=JST)
        made = self.store.set(self.day(0), now=now)
        self.assertEqual(made.set_at.utcoffset(), timedelta(hours=9))
        self.assertEqual(self.read_saved()["set_at"], "2026-10-13T07:30:00+09:00")
        self.assertEqual(self.store.current().set_at.utcoffset(), timedelta(hours=9))

    def test_naive_now_is_made_aware_without_moving_the_clock_time(self):
        naive = datetime(2026, 10, 13, 12, 0, 0)
        made = self.store.set(self.day(0), now=naive)
        self.assertIsNotNone(made.set_at.utcoffset())
        self.assertEqual(made.set_at.replace(tzinfo=None), naive)
        self.assertEqual(self.store.current().set_at, made.set_at)

    def test_microseconds_are_dropped_so_the_file_and_the_result_agree(self):
        now = datetime(2026, 10, 13, 7, 30, 12, 345678, tzinfo=JST)
        made = self.store.set(self.day(0), now=now)
        self.assertEqual(made.set_at, datetime(2026, 10, 13, 7, 30, 12, tzinfo=JST))
        self.assertEqual(self.store.current(), made)

    def test_a_file_written_by_hand_without_an_offset_is_still_aware(self):
        self.write_data(set_at="2026-10-13T07:30:00")
        pause = self.store.current()
        self.assertIsNotNone(pause)
        self.assertIsNotNone(pause.set_at.utcoffset())


class StrictOldFromisoformat(datetime):
    """``fromisoformat`` が末尾の ``Z`` を受け付けない版（Python 3.10 以前）の ``datetime`` の代わり。"""

    @classmethod
    def fromisoformat(cls, text):
        if text[-1:] in ("Z", "z"):
            raise ValueError("Invalid isoformat string: {0!r}".format(text))
        return datetime.fromisoformat(text)


class SetAtReadingTest(PauseTestCase):
    """``set_at`` は参考の項目。末尾の ``Z`` はどの版でも読め、読めなくても記録は使える。"""

    UTC = timezone.utc

    def test_a_trailing_z_means_utc(self):
        for text in ("2026-10-13T07:30:00Z", "2026-10-13T07:30:00z", "2026-10-13T07:30:00.250000Z"):
            with self.subTest(text):
                self.write_data(set_at=text)
                pause = self.store.current()
                self.assertIsNotNone(pause)
                self.assertEqual(pause.set_at.utcoffset(), timedelta(0))
                self.assertEqual(pause.set_at.replace(microsecond=0),
                                 datetime(2026, 10, 13, 7, 30, tzinfo=self.UTC))

    def test_a_trailing_z_is_read_even_where_fromisoformat_rejects_it(self):
        # Python 3.10 以前の ``fromisoformat`` は ``Z`` を受け付けない。どの版でも同じに読む。
        with mock.patch("chime.pause.datetime", StrictOldFromisoformat):
            with self.assertRaises(ValueError):    # 代わりが、古い版と同じに断ることの確認
                StrictOldFromisoformat.fromisoformat("2026-10-13T07:30:00Z")
            self.write_data(set_at="2026-10-14T00:00:00Z")
            pause = self.store.current()
        self.assertIsNotNone(pause)
        self.assertEqual(pause.set_at, datetime(2026, 10, 14, 0, 0, tzinfo=self.UTC))
        self.assertTrue(pause.covers(self.today))

    def test_an_unreadable_set_at_is_unknown_and_the_pause_still_works(self):
        for label, value in (("無い", DROP), ("null", None), ("数", 12345), ("真偽値", True), ("リスト", ["今朝"]),
                             ("空の文字", ""), ("書き方が違う", "今朝"), ("Z だけ", "Z"),
                             ("月が範囲外", "2026-13-45T07:30:00+09:00"), ("時が範囲外", "2026-10-13T25:61:00"),
                             ("ずれが範囲外", "2026-10-13T07:30:00+99:00")):
            with self.subTest(label):
                self.write_data(set_at=value)
                with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
                    logging.getLogger("chime.pause").warning("dummy")
                    pause = self.store.current()
                    covered = self.store.covers(self.today)
                self.assertEqual(len(captured.records), 1, captured.output)   # 警告は出ない（dummy だけ）
                self.assertEqual(pause, Pause(first_day=TODAY, last_day=self.day(2), set_at=None,
                                              reason="学園祭の準備"))
                self.assertTrue(covered)

    def test_a_pause_with_an_unknown_set_at_is_replaced_by_set(self):
        self.write_data(set_at="今朝")
        now = datetime(2026, 10, 13, 8, 0, tzinfo=JST)
        made = self.store.set(self.day(1), "続ける", now=now)
        self.assertEqual(self.store.current(), made)
        self.assertEqual(made.set_at, now)

    def test_a_time_that_cannot_be_made_local_is_unknown(self):
        # ずれの無い日時は、この機械の時刻にして読む。それができなくても（範囲外の年など）、記録は使える。
        class Unconvertible(datetime):
            error = OverflowError("date value out of range")

            def astimezone(self, tz=None):
                raise self.error

        class Naive(datetime):
            @classmethod
            def fromisoformat(cls, text):
                moment = datetime.fromisoformat(text)
                return Unconvertible(moment.year, moment.month, moment.day, moment.hour, moment.minute)

        for error in (OverflowError("date value out of range"), OSError("localtime failed"),
                      ValueError("year is out of range")):
            with self.subTest(type(error).__name__), mock.patch.object(Unconvertible, "error", error), \
                    mock.patch("chime.pause.datetime", Naive):
                self.write_data(set_at="2026-10-13T07:30:00")
                pause = self.store.current()
                self.assertIsNotNone(pause)
                self.assertIsNone(pause.set_at)
                self.assertTrue(pause.covers(self.today))


class LimitsTest(PauseTestCase):
    def test_longest_allowed_pause_is_31_days_counting_today(self):
        made = self.store.set(self.day(MAX_PAUSE_DAYS - 1))
        self.assertEqual((made.last_day - made.first_day).days + 1, 31)
        self.assertEqual(self.store.current(), made)

    def test_32_days_is_rejected_without_touching_the_file(self):
        with self.assertRaises(ValueError) as caught:
            self.store.set(self.day(MAX_PAUSE_DAYS))
        self.assertIn("31", str(caught.exception))
        self.assertFalse(os.path.exists(self.path))

    def test_a_rejected_request_keeps_the_existing_pause(self):
        kept = self.store.set(self.day(2), "そのまま")
        with self.assertRaises(ValueError):
            self.store.set(self.day(100))
        with self.assertRaises(ValueError):
            self.store.set(self.day(-1))
        self.assertEqual(self.store.current(), kept)

    def test_yesterday_is_rejected_with_a_japanese_message(self):
        with self.assertRaises(ValueError) as caught:
            self.store.set(self.day(-1))
        message = str(caught.exception)
        self.assertIn("2026-10-12", message)
        self.assertIn("過去", message)
        self.assertFalse(os.path.exists(self.path))

    def test_far_past_and_far_future_are_rejected(self):
        for last_day in (date(2000, 1, 1), date.max):
            with self.subTest(last_day=last_day):
                with self.assertRaises(ValueError):
                    self.store.set(last_day)

    def test_a_reason_that_cannot_be_written_as_utf8_is_rejected(self):
        # コマンドラインの引数に文字コードの壊れた文字が混ざると、サロゲートになる。
        with self.assertRaises(ValueError) as caught:
            self.store.set(self.day(0), "壊れた\udcff文字")
        self.assertIn("理由", str(caught.exception))
        self.assertFalse(os.path.exists(self.path))

    def test_limit_applies_when_reading_a_file_edited_by_hand(self):
        # 31 日を超える記録は、消し忘れの「止めっぱなし」になりかねないので使わない。
        self.write_data(last_day="2026-11-13")
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING"):
            self.assertIsNone(self.store.current())
        self.write_data(last_day="2026-11-12")
        self.assertIsNotNone(self.store.current())


class ExpiryTest(PauseTestCase):
    def test_the_last_day_is_still_paused_and_the_next_day_is_not(self):
        self.store.set(self.day(2), "三日間")
        self.today = self.day(2)
        self.assertIsNotNone(self.store.current())
        self.assertTrue(self.store.covers(self.today))
        self.today = self.day(3)
        self.assertIsNone(self.store.current())
        self.assertFalse(self.store.covers(self.today))

    def test_expiry_is_not_a_warning_and_leaves_the_file_alone(self):
        self.store.set(self.day(0))
        self.today = self.day(10)
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            logging.getLogger("chime.pause").warning("dummy")
            self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)
        self.assertTrue(os.path.exists(self.path))

    def test_a_pause_that_has_not_started_yet_is_kept_but_covers_nothing_today(self):
        self.write_data(first_day="2026-10-20", last_day="2026-10-22")
        pause = self.store.current()
        self.assertEqual(pause.first_day, date(2026, 10, 20))
        self.assertFalse(self.store.covers(self.today))
        self.assertTrue(self.store.covers(date(2026, 10, 21)))


class UnusableFileTest(PauseTestCase):
    """壊れた・読めない・知らない版の記録は、止めずに「休止なし」として扱う。"""

    def bad_contents(self):
        """(説明, ファイルの中身, WARNING に含まれるはずの言葉) の一覧。"""
        valid = self.valid_data
        return [
            ("空のファイル", "", "JSON の書き方"),
            ("JSON ではない文字", "これは JSON ではありません", "JSON の書き方"),
            ("途中で切れた JSON", '{"v": 1, "first_day": "2026-10-13", "last_', "JSON の書き方"),
            ("Shift_JIS", "休止".encode("shift_jis"), "UTF-8"),
            ("バイナリ", bytes(range(256)), "UTF-8"),
            ("最上位がリスト", "[1, 2, 3]", "最上位"),
            ("最上位が null", "null", "最上位"),
            ("最上位が文字", '"止める"', "最上位"),
            ("深すぎる入れ子", "[" * 100000, "入れ子"),
            ("別の版 (v=2)", json.dumps(valid(v=2)), "知らない版"),
            ("版が無い", json.dumps(valid(v=DROP)), "知らない版"),
            ("版が文字", json.dumps(valid(v="1")), "知らない版"),
            ("版が真偽値", json.dumps(valid(v=True)), "知らない版"),
            ("版が小数", json.dumps(valid(v=1.0)), "知らない版"),
            ("開始日が無い", json.dumps(valid(first_day=DROP)), "first_day"),
            ("終了日が無い", json.dumps(valid(last_day=DROP)), "last_day"),
            ("終了日が数", json.dumps(valid(last_day=20261015)), "last_day"),
            ("終了日が null", json.dumps(valid(last_day=None)), "last_day"),
            ("終了日の書き方が違う", json.dumps(valid(last_day="2026/10/15")), "last_day"),
            ("終了日が全角の数字", json.dumps(valid(last_day="２０２６-１０-１５")), "last_day"),
            ("終了日が基本形式", json.dumps(valid(last_day="20261015")), "last_day"),
            ("存在しない日付", json.dumps(valid(last_day="2026-02-30")), "カレンダーにない"),
            ("終了日が開始日より前", json.dumps(valid(last_day="2026-10-12")), "より前"),
            ("理由が文字ではない", json.dumps(valid(reason=["学園祭"])), "reason"),
        ]

    def test_every_unusable_file_means_no_pause_with_one_warning(self):
        for label, content, expected in self.bad_contents():
            with self.subTest(label):
                store = PauseStore(self.path, lambda: self.today)
                self.write_raw(content)
                with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
                    self.assertIsNone(store.current())
                    self.assertFalse(store.covers(self.today))
                self.assertEqual(len(captured.records), 1, captured.output)
                message = captured.records[0].getMessage()
                self.assertIn(self.path, message)
                self.assertIn("通常どおり", message)
                self.assertIn(expected, message)
                # 想定内の壊れ方は、理由を説明できる（「想定外」にしない）。
                self.assertNotIn("想定外", message)

    def test_a_directory_in_place_of_the_file_is_unreadable_not_fatal(self):
        os.makedirs(self.path)
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

    def test_a_missing_reason_is_fine(self):
        self.write_data(reason=DROP)
        self.assertEqual(self.store.current().reason, "")

    def test_utf8_bom_file_is_readable(self):
        # メモ帳の「UTF-8（BOM 付き）」で手直ししても読める。
        self.write_raw(b"\xef\xbb\xbf" + json.dumps(
            self.valid_data(), ensure_ascii=False).encode("utf-8"))
        self.assertEqual(self.store.current().reason, "学園祭の準備")

    def test_the_warning_is_logged_once_per_change(self):
        # 常駐側は毎回 current() を呼ぶので、同じ壊れ方を延々と記録しない。
        self.write_raw("これは JSON ではありません")
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            for _ in range(5):
                self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

        # 書き換えられて別の壊れ方をしたら、もう 1 回。
        self.write_raw("[1, 2, 3]")
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            for _ in range(5):
                self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

    def test_fixing_the_file_and_breaking_it_again_warns_again(self):
        self.write_raw("壊れている")
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

        self.store.set(self.day(1))
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            logging.getLogger("chime.pause").warning("dummy")
            self.assertIsNotNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

        self.write_raw("壊れている")
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

    def test_removing_the_broken_file_and_breaking_it_again_warns_again(self):
        self.write_raw("壊れている")
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING"):
            self.assertIsNone(self.store.current())
        os.remove(self.path)
        self.assertIsNone(self.store.current())
        self.write_raw("壊れている")
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

    def test_a_rewrite_with_the_same_mistake_warns_again(self):
        # 壊れた記録を、同じ壊れ方のまま書き直したら（更新時刻が変わる）、あらためて 1 度警告する。
        # ``current()`` の説明は「同じ状態のあいだは 1 度だけ。ファイルが書き換わるとまた 1 度残す」。
        self.write_raw("壊れている")
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(self.store.current())
            self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

        info = os.stat(self.path)
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 5 * 10 ** 9))   # 中身は同じ・更新時刻だけ変わる
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(self.store.current())
            self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

    def test_a_repeat_of_the_same_problem_after_recovery_warns_again(self):
        # ファイルの状態の目印が偶然同じ（更新時刻の粗い機械で、すぐ書き直した場合など）
        # でも、直って（または消えて）からまた壊れたら、あらためて 1 度残す。
        broken = "壊れている"
        with mock.patch("chime.pause._signature", return_value=(1, 2, 3)):
            for recovery in (lambda: self.store.set(self.day(1)), lambda: os.remove(self.path)):
                self.store = PauseStore(self.path, lambda: self.today)
                self.write_raw(broken)
                with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
                    self.assertIsNone(self.store.current())
                    self.assertIsNone(self.store.current())
                self.assertEqual(len(captured.records), 1)

                recovery()
                self.store.current()

                self.write_raw(broken)
                with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
                    self.assertIsNone(self.store.current())
                    self.assertIsNone(self.store.current())
                self.assertEqual(len(captured.records), 1)

    def test_a_broken_file_can_be_replaced_by_set(self):
        self.write_raw("壊れている")
        made = self.store.set(self.day(1), "上書き")
        self.assertEqual(self.store.current(), made)

    def test_an_unexpected_error_still_means_no_pause(self):
        # 想定外の例外でも、休止の記録のせいで放送を止めない。
        self.store.set(self.day(1))
        with mock.patch("chime.pause.read_json", side_effect=RuntimeError("想定外")), \
                logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(self.store.current())
            self.assertFalse(self.store.covers(self.today))
        self.assertGreaterEqual(len(captured.records), 1)
        self.assertIn("通常どおり", captured.records[0].getMessage())


class ClockFailureTest(PauseTestCase):
    def test_a_failing_clock_means_no_pause_not_a_crash(self):
        self.store.set(self.day(1))

        def broken_clock():
            raise RuntimeError("時計が読めない")

        store = PauseStore(self.path, broken_clock)
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(store.current())
            self.assertFalse(store.covers(self.today))
        self.assertEqual(len(captured.records), 1)


class ClearTest(PauseTestCase):
    def test_missing_file_returns_false_without_raising(self):
        self.assertFalse(self.store.clear())

    def test_missing_directory_returns_false_without_raising(self):
        self.assertFalse(os.path.exists(self.directory))
        self.assertFalse(self.store.clear())

    def test_clear_removes_the_pause(self):
        self.store.set(self.day(2))
        self.assertTrue(self.store.clear())
        self.assertFalse(os.path.exists(self.path))
        self.assertIsNone(self.store.current())
        self.assertFalse(self.store.covers(self.today))

    def test_second_clear_returns_false(self):
        self.store.set(self.day(2))
        self.assertTrue(self.store.clear())
        self.assertFalse(self.store.clear())

    def test_clear_also_removes_a_leftover_broken_or_expired_file(self):
        self.write_raw("壊れている")
        self.assertTrue(self.store.clear())
        self.assertFalse(os.path.exists(self.path))

        self.store.set(self.day(0))
        self.today = self.day(5)
        self.assertTrue(self.store.clear())
        self.assertFalse(os.path.exists(self.path))

    def test_a_pause_can_be_set_again_after_clear(self):
        self.store.set(self.day(2))
        self.store.clear()
        made = self.store.set(self.day(1), "もう一度")
        self.assertEqual(self.store.current(), made)

    def test_other_failures_are_not_hidden(self):
        # 消せなかったのに「解除した」と答えない（ファイルの代わりにフォルダがある）。
        os.makedirs(self.path)
        with self.assertRaises(OSError):
            self.store.clear()


class ChangedTest(PauseTestCase):
    def test_first_call_records_a_baseline_and_returns_false(self):
        self.assertFalse(self.store.changed())

    def test_first_call_returns_false_even_when_a_pause_already_exists(self):
        self.store.set(self.day(1))
        fresh = PauseStore(self.path, lambda: self.today)
        self.assertFalse(fresh.changed())
        self.assertFalse(fresh.changed())

    def test_unchanged_file_is_not_a_change(self):
        self.store.set(self.day(1))
        self.assertFalse(self.store.changed())
        for _ in range(3):
            self.assertFalse(self.store.changed())

    def test_set_is_a_change_exactly_once(self):
        self.assertFalse(self.store.changed())
        self.store.set(self.day(1), "休止")
        self.assertTrue(self.store.changed())
        self.assertFalse(self.store.changed())

    def test_clear_is_a_change_exactly_once(self):
        self.store.set(self.day(1))
        self.assertFalse(self.store.changed())
        self.assertTrue(self.store.clear())
        self.assertTrue(self.store.changed())
        self.assertFalse(self.store.changed())

    def test_replacing_with_a_file_of_the_same_size_is_a_change(self):
        # 文字を 1 つ書き換えるだけの手直しでは、大きさは変わらない。更新時刻まで
        # 同じにしても、置き換えで inode が変わるので気づける。
        stamp = 1_700_000_000_000_000_000
        self.write_data(reason="あ")
        os.utime(self.path, ns=(stamp, stamp))
        self.assertFalse(self.store.changed())
        before = os.stat(self.path)

        replacement = self.path + ".new"
        with open(replacement, "w", encoding="utf-8") as handle:
            json.dump(self.valid_data(reason="い"), handle, ensure_ascii=False)
        os.utime(replacement, ns=(stamp, stamp))
        os.replace(replacement, self.path)
        after = os.stat(self.path)
        self.assertEqual((after.st_size, after.st_mtime_ns),
                         (before.st_size, before.st_mtime_ns))
        self.assertNotEqual(after.st_ino, before.st_ino)
        self.assertTrue(self.store.changed())
        self.assertFalse(self.store.changed())

    def test_rewriting_in_place_with_the_same_size_is_a_change_by_mtime(self):
        self.write_data(reason="あ")
        self.assertFalse(self.store.changed())
        before = os.stat(self.path)
        with open(self.path, "r+", encoding="utf-8") as handle:
            text = handle.read().replace("あ", "い")
            handle.seek(0)
            handle.write(text)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
        after = os.stat(self.path)
        self.assertEqual((after.st_size, after.st_ino), (before.st_size, before.st_ino))
        self.assertTrue(self.store.changed())

    def test_a_different_size_is_a_change(self):
        self.write_data(reason="あ")
        self.assertFalse(self.store.changed())
        before = os.stat(self.path)
        self.write_data(reason="あいうえお")
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertTrue(self.store.changed())

    def test_a_missing_file_stays_unchanged(self):
        self.assertFalse(self.store.changed())
        self.assertFalse(self.store.changed())

    def test_stat_errors_count_as_missing(self):
        self.store.set(self.day(1))
        self.assertFalse(self.store.changed())
        with mock.patch("chime.pause.os.stat", side_effect=PermissionError("許可がない")):
            self.assertTrue(self.store.changed())     # あった → 見えない
            self.assertFalse(self.store.changed())    # 見えないまま
        self.assertTrue(self.store.changed())         # 見えない → あった

    def test_stat_error_from_the_start_is_a_missing_baseline(self):
        with mock.patch("chime.pause.os.stat", side_effect=OSError("読めない")):
            self.assertFalse(self.store.changed())
            self.assertFalse(self.store.changed())

    def test_reading_the_pause_does_not_reset_the_baseline(self):
        self.assertFalse(self.store.changed())
        self.store.set(self.day(1))
        self.assertIsNotNone(self.store.current())
        self.assertTrue(self.store.changed())

    def test_each_store_keeps_its_own_baseline(self):
        other = PauseStore(self.path, lambda: self.today)
        self.assertFalse(self.store.changed())
        self.assertFalse(other.changed())
        self.store.set(self.day(1))
        self.assertTrue(other.changed())
        self.assertTrue(self.store.changed())
        self.assertFalse(other.changed())


class BoundedReadTest(PauseTestCase):
    """開くと固まる・遅れるファイルは、開かずに「止めていない」にする（常駐ループを止めない）。

    名前付きパイプは、書き手が現れるまで ``open`` が戻らない。巨大なファイルは、時報の直前の
    確認を遅らせる。どちらも ``os.stat`` で見分けて、開かない。
    """

    def spy_on_reading(self):
        """``read_json`` の代わり。呼ばれた場所を記録し、呼ばれたら失敗にする（開かせない）。"""
        opened = []

        def refuse(path):
            opened.append(path)
            raise AssertionError("開いてはいけないファイルを開こうとした: {0}".format(path))

        patcher = mock.patch("chime.pause.read_json", refuse)
        patcher.start()
        self.addCleanup(patcher.stop)
        return opened

    def assert_not_opened_and_reported_once(self, store, opened, word):
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            for _ in range(3):
                self.assertIsNone(store.current())
                self.assertFalse(store.covers(self.today))
        self.assertEqual(opened, [])
        self.assertEqual(len(captured.records), 1, captured.output)
        message = captured.records[0].getMessage()
        self.assertIn(store.path, message)
        self.assertIn(word, message)
        self.assertIn("通常どおり", message)
        self.assertNotIn("想定外", message)

    def test_the_size_limit_is_one_mebibyte(self):
        self.assertEqual(MAX_PAUSE_BYTES, 1048576)

    def test_a_folder_is_not_opened_and_is_reported_once(self):
        os.makedirs(self.path)
        opened = self.spy_on_reading()
        self.assert_not_opened_and_reported_once(self.store, opened, "普通のファイルではない")

    def test_a_named_pipe_is_not_opened_and_is_reported_once(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("この環境には mkfifo がない")
        os.makedirs(self.directory)
        os.mkfifo(self.path)
        opened = self.spy_on_reading()
        self.assert_not_opened_and_reported_once(self.store, opened, "普通のファイルではない")

    def test_a_named_pipe_does_not_block_the_caller(self):
        # 本物の名前付きパイプで。開く側は書き手が現れるまで戻らないので、別スレッドで呼び、
        # 退行したら（固まったら）時間内に戻らないことで失敗にする。
        if not hasattr(os, "mkfifo"):
            self.skipTest("この環境には mkfifo がない")
        os.makedirs(self.directory)
        os.mkfifo(self.path)
        results = []
        worker = threading.Thread(
            target=lambda: results.append((self.store.current(), self.store.covers(self.today),
                                           self.store.changed())), daemon=True)
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING"):
            worker.start()
            worker.join(2)            # 正しく動けば一瞬で戻る。固まったら 2 秒で失敗にする
            if worker.is_alive():
                # 固まっている open を書き手になって外してから、失敗にする（スレッドを残さないため）。
                with open(self.path, "wb", buffering=0) as writer:
                    writer.write(b"{}")
                worker.join(1)
                self.fail("名前付きパイプの休止の記録で、読み込みが固まった")
        self.assertEqual(results, [(None, False, False)])

    def test_a_device_is_not_opened(self):
        if not os.path.exists("/dev/null"):
            self.skipTest("/dev/null がない")
        store = PauseStore("/dev/null", lambda: self.today)
        opened = self.spy_on_reading()
        self.assert_not_opened_and_reported_once(store, opened, "普通のファイルではない")

    def test_a_link_to_a_device_is_not_opened(self):
        # /dev/zero は開けるが、終わりなく読める。リンク先で判断する。
        if not (os.path.exists("/dev/zero") and hasattr(os, "symlink")):
            self.skipTest("/dev/zero かシンボリックリンクがない")
        os.makedirs(self.directory)
        os.symlink("/dev/zero", self.path)
        opened = self.spy_on_reading()
        self.assert_not_opened_and_reported_once(self.store, opened, "普通のファイルではない")

    def test_a_link_to_a_regular_file_is_still_honoured(self):
        if not hasattr(os, "symlink"):
            self.skipTest("シンボリックリンクがない")
        real = PauseStore(os.path.join(self.tmp.name, "elsewhere.json"), lambda: self.today)
        made = real.set(self.day(1), "リンク先")
        os.makedirs(self.directory)
        os.symlink(real.path, self.path)
        self.assertEqual(self.store.current(), made)

    def test_a_file_over_the_limit_is_not_read_and_is_reported_once(self):
        body = json.dumps(self.valid_data())
        self.write_raw(body + " " * (MAX_PAUSE_BYTES + 1 - len(body)))
        self.assertEqual(os.path.getsize(self.path), MAX_PAUSE_BYTES + 1)
        opened = self.spy_on_reading()
        self.assert_not_opened_and_reported_once(self.store, opened, "大きすぎる")
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(PauseStore(self.path, lambda: self.today).current())
        message = captured.records[0].getMessage()
        self.assertIn(str(MAX_PAUSE_BYTES + 1), message)
        self.assertIn(str(MAX_PAUSE_BYTES), message)

    def test_a_file_exactly_at_the_limit_is_still_read(self):
        body = json.dumps(self.valid_data(), ensure_ascii=False)
        padded = body.encode("utf-8")
        padded += b" " * (MAX_PAUSE_BYTES - len(padded))
        self.write_raw(padded)
        self.assertEqual(os.path.getsize(self.path), MAX_PAUSE_BYTES)
        pause = self.store.current()
        self.assertIsNotNone(pause)
        self.assertEqual(pause.reason, "学園祭の準備")

    def test_a_long_hand_written_reason_is_still_read(self):
        # 手で書き足した長い理由（100 KB）は、上限より十分小さく、読める。
        self.write_data(reason="あ" * 32000)
        self.assertGreater(os.path.getsize(self.path), 90 * 1024)
        self.assertEqual(len(self.store.current().reason), 32000)

    def test_a_real_record_is_tiny_compared_with_the_limit(self):
        self.store.set(self.day(MAX_PAUSE_DAYS - 1), "学園祭の準備" * 10)
        self.assertLess(os.path.getsize(self.path), MAX_PAUSE_BYTES // 100)

    def test_recovering_from_an_unreadable_kind_works_and_breaking_again_warns_again(self):
        os.makedirs(self.path)
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

        os.rmdir(self.path)
        made = self.store.set(self.day(1), "直った")
        self.assertEqual(self.store.current(), made)

        os.remove(self.path)
        os.makedirs(self.path)
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(self.store.current())
        self.assertEqual(len(captured.records), 1)

    def test_a_stat_failure_other_than_missing_is_left_to_the_reader(self):
        # os.stat で調べられないとき（権限など）は、種類や大きさでは判断せず、読む側に任せる。
        self.store.set(self.day(1))
        with mock.patch("chime.pause.os.stat", side_effect=PermissionError("許可がない")), \
                mock.patch("chime.pause.read_json", wraps=read_json) as reading:
            self.assertIsNotNone(self.store.current())
        self.assertEqual(reading.call_count, 1)


class ReadersNeverRaiseTest(PauseTestCase):
    """読む側（``current``・``covers``・``changed``）は、どんな場所でも例外を出さない。"""

    def awkward_paths(self):
        yield "空の場所", ""
        yield "NUL 文字", os.path.join(self.directory, "pa\0use.json")
        yield "変換できない文字", os.path.join(self.directory, "\ud800.json")
        yield "長すぎる名前", os.path.join(self.directory, "x" * 5000)
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("ファイル")
        yield "ファイルの下", os.path.join(blocker, "pause.json")

    def test_every_reader_returns_quietly_for_an_awkward_place(self):
        for label, path in self.awkward_paths():
            with self.subTest(label):
                store = PauseStore(path, lambda: self.today)
                with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
                    logging.getLogger("chime.pause").warning("dummy")
                    self.assertIsNone(store.current())
                    self.assertFalse(store.covers(self.today))
                    self.assertFalse(store.changed())    # 基準を覚えるだけ
                    self.assertFalse(store.changed())    # 見えないまま
                for record in captured.records[1:]:
                    self.assertIn("通常どおり", record.getMessage())
                    self.assertNotIn("想定外", record.getMessage())

    def test_a_place_with_a_nul_character_is_explained_not_blamed_on_a_bug(self):
        store = PauseStore(os.path.join(self.directory, "pa\0use.json"), lambda: self.today)
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            self.assertIsNone(store.current())
            self.assertIsNone(store.current())
        self.assertEqual(len(captured.records), 1)
        self.assertIn("場所の書き方", captured.records[0].getMessage())

    def test_changed_catches_a_value_error_from_stat_and_treats_it_as_missing(self):
        # os.stat は、場所に NUL 文字などがあると OSError ではなく ValueError を出す。
        self.store.set(self.day(1))
        self.assertFalse(self.store.changed())
        with mock.patch("chime.pause.os.stat", side_effect=ValueError("embedded null byte")):
            self.assertTrue(self.store.changed())     # あった → 見えない
            self.assertFalse(self.store.changed())    # 見えないまま
        self.assertTrue(self.store.changed())         # 見えない → あった

    def test_changed_catches_a_unicode_error_from_stat_too(self):
        error = UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
        with mock.patch("chime.pause.os.stat", side_effect=error):
            self.assertFalse(self.store.changed())
            self.assertFalse(self.store.changed())

    def test_a_rewrite_within_the_same_second_is_a_change(self):
        # 同じ大きさ・同じ inode（その場の書き換え）で、更新時刻が 100 ミリ秒だけ進んだ場合。
        # 更新時刻を秒に丸めると見逃す。
        os.makedirs(self.directory)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        base = 1_800_000_000 * 10 ** 9 + 100_000_000
        os.utime(self.path, ns=(base, base))
        self.assertFalse(self.store.changed())     # 基準
        self.assertFalse(self.store.changed())     # 触っていない
        os.utime(self.path, ns=(base + 100_000_000, base + 100_000_000))
        self.assertTrue(self.store.changed())
        self.assertFalse(self.store.changed())     # 基準が進んだ


if __name__ == "__main__":
    unittest.main()
