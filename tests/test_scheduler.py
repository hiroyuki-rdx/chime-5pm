"""スケジューリングのテスト。"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from chime.config import DEFAULT_CONFIG
from chime.scheduler import (MAX_LOOKAHEAD_DAYS, Scheduler, format_events, format_schedule,
                             hourly_hours)
from tests.support import logs_enabled

TZ = ZoneInfo("Asia/Tokyo")
SCHEDULE = DEFAULT_CONFIG["schedule"]

# 2026-08-26 は水曜日、2026-08-29 は土曜日、2026-08-31 は月曜日。
WEDNESDAY = datetime(2026, 8, 26, 9, 0, tzinfo=TZ)
SATURDAY = datetime(2026, 8, 29, 9, 0, tzinfo=TZ)


def make_scheduler(settings=None, lead=3.0) -> Scheduler:
    return Scheduler(settings or SCHEDULE, TZ, lead)


class EventsForDateTest(unittest.TestCase):
    def test_weekday_has_seven_hourly_and_one_closing(self):
        events = make_scheduler().events_for_date(WEDNESDAY.date())
        hourly = [event for event in events if event.kind == "hourly"]
        closing = [event for event in events if event.kind == "closing"]
        self.assertEqual([event.hour for event in hourly], [10, 11, 12, 13, 14, 15, 16])
        self.assertEqual(len(closing), 1)
        self.assertEqual((closing[0].at.hour, closing[0].at.minute), (16, 57))

    def test_weekend_has_no_events(self):
        self.assertEqual(make_scheduler().events_for_date(SATURDAY.date()), [])

    def test_hourly_play_at_is_lead_seconds_early(self):
        events = make_scheduler().events_for_date(WEDNESDAY.date())
        ten = next(event for event in events if event.hour == 10 and event.kind == "hourly")
        self.assertEqual(ten.at, datetime(2026, 8, 26, 10, 0, tzinfo=TZ))
        self.assertEqual(ten.play_at, datetime(2026, 8, 26, 9, 59, 57, tzinfo=TZ))

    def test_closing_has_no_lead(self):
        events = make_scheduler().events_for_date(WEDNESDAY.date())
        closing = next(event for event in events if event.kind == "closing")
        self.assertEqual(closing.play_at, closing.at)

    def test_skip_hours(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], skip_hours=[12, 13]),
            "closing": dict(SCHEDULE["closing"], enabled=False),
        }
        events = make_scheduler(settings).events_for_date(WEDNESDAY.date())
        self.assertEqual([event.hour for event in events], [10, 11, 14, 15, 16])

    def test_disabled_sections(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], enabled=False),
            "closing": dict(SCHEDULE["closing"], enabled=True),
        }
        events = make_scheduler(settings).events_for_date(WEDNESDAY.date())
        self.assertEqual([event.kind for event in events], ["closing"])

    def test_custom_weekdays_include_saturday(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], weekdays=[5]),
            "closing": dict(SCHEDULE["closing"], enabled=False),
        }
        events = make_scheduler(settings).events_for_date(SATURDAY.date())
        self.assertEqual(len(events), 7)

    def test_events_are_sorted_by_play_at(self):
        events = make_scheduler().events_for_date(WEDNESDAY.date())
        self.assertEqual(events, sorted(events, key=lambda event: event.play_at))

    def test_day_key_is_event_date(self):
        events = make_scheduler().events_for_date(WEDNESDAY.date())
        self.assertEqual(events[0].day, "2026-08-26")


class EventsForDateRulesTest(unittest.TestCase):
    """``events_for_date`` の細かい規則（時報・閉館放送の組み立てを分けても保つもの）。"""

    def test_event_keys_are_hourly_with_zero_padded_hour_and_closing(self):
        events = make_scheduler().events_for_date(WEDNESDAY.date())
        self.assertEqual([event.key for event in events],
                         ["hourly:10", "hourly:11", "hourly:12", "hourly:13",
                          "hourly:14", "hourly:15", "hourly:16", "closing"])

    def test_single_digit_hour_key_is_zero_padded(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], start_hour=9, end_hour=9),
            "closing": dict(SCHEDULE["closing"], enabled=False),
        }
        events = make_scheduler(settings).events_for_date(WEDNESDAY.date())
        self.assertEqual([event.key for event in events], ["hourly:09"])

    def test_hourly_minute_moves_both_at_and_play_at(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], start_hour=10, end_hour=10, minute=30),
            "closing": dict(SCHEDULE["closing"], enabled=False),
        }
        event = make_scheduler(settings).events_for_date(WEDNESDAY.date())[0]
        self.assertEqual(event.at, datetime(2026, 8, 26, 10, 30, tzinfo=TZ))
        self.assertEqual(event.play_at, datetime(2026, 8, 26, 10, 29, 57, tzinfo=TZ))

    def test_hours_outside_0_to_23_are_dropped(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], start_hour=22, end_hour=25),
            "closing": dict(SCHEDULE["closing"], enabled=False),
        }
        events = make_scheduler(settings).events_for_date(WEDNESDAY.date())
        self.assertEqual([event.hour for event in events], [22, 23])
        settings["hourly"] = dict(SCHEDULE["hourly"], start_hour=-2, end_hour=1)
        events = make_scheduler(settings).events_for_date(WEDNESDAY.date())
        self.assertEqual([event.hour for event in events], [0, 1])

    def test_skip_hours_may_be_null(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], skip_hours=None),
            "closing": dict(SCHEDULE["closing"], enabled=False),
        }
        events = make_scheduler(settings).events_for_date(WEDNESDAY.date())
        self.assertEqual(len(events), 7)

    def test_missing_keys_use_the_defaults(self):
        # enabled・start_hour・end_hour・minute・hour などは省略でき、既定は
        # 時報 10〜16 時の正時、閉館放送 16:57。曜日だけは省略すると稼働日なし。
        settings = {"hourly": {"weekdays": [2]}, "closing": {"weekdays": [2]}}
        events = make_scheduler(settings).events_for_date(WEDNESDAY.date())
        self.assertEqual([event.key for event in events],
                         ["hourly:10", "hourly:11", "hourly:12", "hourly:13",
                          "hourly:14", "hourly:15", "hourly:16", "closing"])
        self.assertEqual((events[0].at.hour, events[0].at.minute), (10, 0))
        self.assertEqual((events[-1].at.hour, events[-1].at.minute), (16, 57))
        self.assertEqual(make_scheduler({"hourly": {}, "closing": {}})
                         .events_for_date(WEDNESDAY.date()), [])

    def test_null_or_missing_sections_mean_no_events(self):
        self.assertEqual(make_scheduler({"hourly": None, "closing": None})
                         .events_for_date(WEDNESDAY.date()), [])
        # 空の設定は `settings or SCHEDULE` で既定に戻るため、別のキーだけを持たせる。
        self.assertEqual(make_scheduler({"prepare_lead_seconds": 45})
                         .events_for_date(WEDNESDAY.date()), [])

    def test_disabled_section_does_not_read_its_weekdays(self):
        # 無効な節の weekdays は読まない（壊れた値があっても例外にしない）。
        settings = {
            "hourly": {"enabled": False, "weekdays": 5},
            "closing": {"enabled": False, "weekdays": 5},
        }
        self.assertEqual(make_scheduler(settings).events_for_date(WEDNESDAY.date()), [])

    def test_enabled_section_does_read_its_weekdays(self):
        settings = {"hourly": {"enabled": True, "weekdays": 5}, "closing": {"enabled": False}}
        with self.assertRaises(TypeError):
            make_scheduler(settings).events_for_date(WEDNESDAY.date())

    def test_each_section_has_its_own_weekdays(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], weekdays=[2]),
            "closing": dict(SCHEDULE["closing"], weekdays=[5]),
        }
        scheduler = make_scheduler(settings)
        self.assertEqual({event.kind for event in scheduler.events_for_date(WEDNESDAY.date())},
                         {"hourly"})
        self.assertEqual([event.kind for event in scheduler.events_for_date(SATURDAY.date())],
                         ["closing"])

    def test_closing_hour_and_minute_are_configurable(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], enabled=False),
            "closing": dict(SCHEDULE["closing"], hour=17, minute=5),
        }
        event = make_scheduler(settings).events_for_date(WEDNESDAY.date())[0]
        self.assertEqual((event.key, event.hour), ("closing", 17))
        self.assertEqual(event.at, datetime(2026, 8, 26, 17, 5, tzinfo=TZ))

    def test_hourly_uses_the_pip_lead_and_closing_uses_none(self):
        events = make_scheduler(lead=5.0).events_for_date(WEDNESDAY.date())
        for event in events:
            expected = 5.0 if event.kind == "hourly" else 0.0
            self.assertEqual((event.at - event.play_at).total_seconds(), expected)
            self.assertEqual((event.play_at - event.prepare_at).total_seconds(), 45.0)

    def test_equal_play_at_keeps_hourly_before_closing(self):
        # 同時刻なら安定ソートで時報が先、閉館放送が後（組み立ての順を保つ）。
        settings = {
            "hourly": dict(SCHEDULE["hourly"], start_hour=10, end_hour=11),
            "closing": dict(SCHEDULE["closing"], hour=10, minute=0),
        }
        events = make_scheduler(settings, lead=0.0).events_for_date(WEDNESDAY.date())
        self.assertEqual([event.key for event in events],
                         ["hourly:10", "closing", "hourly:11"])

    def test_closing_before_an_hourly_is_sorted_by_play_at(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], start_hour=10, end_hour=11),
            "closing": dict(SCHEDULE["closing"], hour=10, minute=30),
        }
        events = make_scheduler(settings).events_for_date(WEDNESDAY.date())
        self.assertEqual([event.key for event in events],
                         ["hourly:10", "closing", "hourly:11"])


class HourlyHoursTest(unittest.TestCase):
    """``hourly_hours``: 作り置きの文言を数える側（``chime.phrases``）も使う共有の範囲。"""

    def test_defaults_when_the_keys_are_missing(self):
        self.assertEqual(list(hourly_hours({})), [10, 11, 12, 13, 14, 15, 16])

    def test_matches_the_default_config(self):
        self.assertEqual(list(hourly_hours(SCHEDULE["hourly"])), [10, 11, 12, 13, 14, 15, 16])

    def test_is_a_range_with_both_ends_included(self):
        hours = hourly_hours({"start_hour": 9, "end_hour": 12})
        self.assertIsInstance(hours, range)
        self.assertEqual(list(hours), [9, 10, 11, 12])

    def test_only_one_end_may_be_given(self):
        self.assertEqual(list(hourly_hours({"start_hour": 14})), [14, 15, 16])
        self.assertEqual(list(hourly_hours({"end_hour": 12})), [10, 11, 12])

    def test_a_single_hour_range(self):
        self.assertEqual(list(hourly_hours({"start_hour": 13, "end_hour": 13})), [13])

    def test_an_inverted_range_is_empty(self):
        self.assertEqual(list(hourly_hours({"start_hour": 16, "end_hour": 10})), [])

    def test_values_are_converted_with_int(self):
        self.assertEqual(list(hourly_hours({"start_hour": "9", "end_hour": 10.0})), [9, 10])

    def test_skip_hours_are_not_applied(self):
        hourly = dict(SCHEDULE["hourly"], skip_hours=[12, 13])
        self.assertEqual(list(hourly_hours(hourly)), [10, 11, 12, 13, 14, 15, 16])

    def test_hours_outside_0_to_23_are_not_filtered(self):
        self.assertEqual(list(hourly_hours({"start_hour": 22, "end_hour": 25})),
                         [22, 23, 24, 25])
        self.assertEqual(list(hourly_hours({"start_hour": -2, "end_hour": 1})),
                         [-2, -1, 0, 1])

    def test_enabled_and_weekdays_are_not_consulted(self):
        hourly = {"enabled": False, "weekdays": []}
        self.assertEqual(list(hourly_hours(hourly)), [10, 11, 12, 13, 14, 15, 16])


class UpcomingTest(unittest.TestCase):
    def test_skips_the_weekend(self):
        friday_evening = datetime(2026, 8, 28, 18, 0, tzinfo=TZ)
        events = make_scheduler().upcoming(friday_evening, limit=1)
        self.assertEqual(events[0].at, datetime(2026, 8, 31, 10, 0, tzinfo=TZ))

    def test_returns_requested_count(self):
        self.assertEqual(len(make_scheduler().upcoming(WEDNESDAY, limit=12)), 12)

    def test_only_future_events(self):
        noon = datetime(2026, 8, 26, 12, 30, tzinfo=TZ)
        events = make_scheduler().upcoming(noon, limit=3)
        self.assertTrue(all(event.play_at >= noon for event in events))
        self.assertEqual(events[0].hour, 13)


class NextEventTest(unittest.TestCase):
    def test_returns_next_future_event(self):
        event = make_scheduler().next_event(WEDNESDAY)
        self.assertEqual(event.at, datetime(2026, 8, 26, 10, 0, tzinfo=TZ))

    def test_catches_up_within_grace(self):
        """再起動などで数十秒出遅れても、その回は取りこぼさない。"""
        late = datetime(2026, 8, 26, 10, 0, 30, tzinfo=TZ)
        event = make_scheduler().next_event(late)
        self.assertEqual(event.at, datetime(2026, 8, 26, 10, 0, tzinfo=TZ))

    def test_skips_when_beyond_grace(self):
        too_late = datetime(2026, 8, 26, 10, 5, 0, tzinfo=TZ)
        event = make_scheduler().next_event(too_late)
        self.assertEqual(event.at, datetime(2026, 8, 26, 11, 0, tzinfo=TZ))

    def test_does_not_replay_a_fired_event(self):
        late = datetime(2026, 8, 26, 10, 0, 30, tzinfo=TZ)
        fired = {("hourly:10", "2026-08-26")}
        event = make_scheduler().next_event(
            late, is_fired=lambda candidate: (candidate.key, candidate.day) in fired)
        self.assertEqual(event.at, datetime(2026, 8, 26, 11, 0, tzinfo=TZ))

    def test_catch_up_looks_back_across_midnight(self):
        """日付をまたいだ直後でも前日分の取りこぼしを拾える。"""
        settings = {
            "hourly": dict(SCHEDULE["hourly"], start_hour=23, end_hour=23),
            "closing": dict(SCHEDULE["closing"], enabled=False),
            "catchup_grace_seconds": 7200,
        }
        just_after_midnight = datetime(2026, 8, 27, 0, 0, 10, tzinfo=TZ)
        event = make_scheduler(settings).next_event(just_after_midnight)
        self.assertEqual(event.at, datetime(2026, 8, 26, 23, 0, tzinfo=TZ))

    def test_catch_up_looks_back_more_than_one_day_for_large_grace(self):
        """catchup_grace_seconds が 1 日を超える設定でも、その日数分は遡って拾う。"""
        settings = {
            "hourly": dict(SCHEDULE["hourly"], start_hour=23, end_hour=23, weekdays=[2]),
            "closing": dict(SCHEDULE["closing"], enabled=False),
            # 2026-08-26(水) 23:00 のみが該当日。約 47 時間の猶予を与える。
            "catchup_grace_seconds": 170000,
        }
        two_days_later = datetime(2026, 8, 28, 10, 0, 0, tzinfo=TZ)
        event = make_scheduler(settings).next_event(two_days_later)
        self.assertIsNotNone(event)
        self.assertEqual(event.at, datetime(2026, 8, 26, 23, 0, tzinfo=TZ))

    def test_returns_none_when_nothing_scheduled(self):
        settings = {
            "hourly": dict(SCHEDULE["hourly"], enabled=False),
            "closing": dict(SCHEDULE["closing"], enabled=False),
        }
        self.assertIsNone(make_scheduler(settings).next_event(WEDNESDAY))


class LeadConfigurationTest(unittest.TestCase):
    def test_lead_defaults_to_time_signal_length(self):
        self.assertEqual(make_scheduler(lead=4.5).pip_lead, 4.5)

    def test_explicit_lead_overrides(self):
        settings = dict(SCHEDULE, pip_lead_seconds=1.5)
        self.assertEqual(make_scheduler(settings, lead=4.5).pip_lead, 1.5)


class SleepUntilTest(unittest.TestCase):
    def test_returns_immediately_for_past_target(self):
        scheduler = make_scheduler()
        self.assertTrue(scheduler.sleep_until(scheduler.now() - timedelta(seconds=5)))

    def test_stop_event_aborts_waiting(self):
        scheduler = make_scheduler()
        stop = threading.Event()
        stop.set()
        self.assertFalse(
            scheduler.sleep_until(scheduler.now() + timedelta(hours=1), stop))

    def test_precise_wait_is_accurate(self):
        scheduler = make_scheduler()
        target = scheduler.now() + timedelta(milliseconds=120)
        self.assertTrue(scheduler.sleep_until(target, precise=True))
        overshoot = (scheduler.now() - target).total_seconds()
        self.assertGreaterEqual(overshoot, 0.0)
        self.assertLess(overshoot, 0.2)

    def test_stop_event_aborts_final_precise_adjustment(self):
        """残り 0.25 秒未満の最終調整中でも停止要求を見逃さない。"""
        scheduler = make_scheduler()
        stop = threading.Event()
        target = scheduler.now() + timedelta(milliseconds=200)
        timer = threading.Timer(0.05, stop.set)
        timer.start()
        try:
            started = time.monotonic()
            result = scheduler.sleep_until(target, stop, precise=True)
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
        self.assertFalse(result)
        self.assertLess(elapsed, 0.15)

    def test_clock_is_injectable(self):
        moments = iter([WEDNESDAY, WEDNESDAY + timedelta(hours=2)])
        scheduler = Scheduler(SCHEDULE, TZ, 3.0, clock=lambda: next(moments))
        self.assertEqual(scheduler.now(), WEDNESDAY)
        self.assertTrue(scheduler.sleep_until(WEDNESDAY + timedelta(hours=1)))


class FormatEventsTest(unittest.TestCase):
    def test_empty(self):
        self.assertIn("ありません", format_events([]))

    def test_lists_events(self):
        text = format_events(make_scheduler().upcoming(WEDNESDAY, limit=2))
        self.assertIn("時報 2026-08-26 10:00:00", text)
        self.assertIn("09:59:57", text)


# -- 休館日（schedule.closures） ---------------------------------------------------
FRIDAY_EVENING = datetime(2026, 8, 28, 18, 0, tzinfo=TZ)


def with_closures(closures, **overrides):
    """既定の予定に休館日（と、ほかの上書き）を足した設定。"""
    return dict(SCHEDULE, closures=closures, **overrides)


def fields(events):
    """イベントを、比べやすい（キー・種類・正時・再生開始）の組にする。"""
    return [(event.key, event.kind, event.at, event.play_at) for event in events]


def default_schedule_v610(start, count):
    """v6.1.0 の既定の予定（平日 10〜16 時の正時と 16:57）を、設定を読まずに書き下したもの。"""
    rows = []
    day = start.date()
    while len(rows) < count:
        if day.weekday() < 5:
            for hour in range(10, 17):
                at = datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
                rows.append(("hourly:{0:02d}".format(hour), "hourly", at, at - timedelta(seconds=3)))
            at = datetime(day.year, day.month, day.day, 16, 57, tzinfo=TZ)
            rows.append(("closing", "closing", at, at))
        day += timedelta(days=1)
        rows = [row for row in rows if row[3] >= start]
    return rows[:count]


class DefaultScheduleUnchangedTest(unittest.TestCase):
    """休館日のない設定では、予定は v6.1.0 のまま（放送の安全）。"""

    STARTS = (
        WEDNESDAY,
        datetime(2026, 8, 26, 9, 59, 57, tzinfo=TZ),   # ちょうど再生開始の時刻は含める
        datetime(2026, 8, 26, 12, 30, tzinfo=TZ),
        datetime(2026, 8, 28, 16, 57, tzinfo=TZ),      # 閉館放送の時刻ちょうど
        FRIDAY_EVENING,
        SATURDAY,
        datetime(2026, 8, 31, 0, 0, tzinfo=TZ),
        datetime(2026, 12, 31, 23, 59, tzinfo=TZ),
    )

    def test_the_default_config_has_no_closures(self):
        self.assertEqual(SCHEDULE["closures"], {"dates": [], "ranges": []})
        scheduler = make_scheduler()
        self.assertEqual(scheduler.closures.closures, [])
        self.assertEqual(scheduler.closures.problems, [])

    def test_upcoming_forty_matches_the_v610_schedule(self):
        for start in self.STARTS:
            with self.subTest(start=start):
                self.assertEqual(fields(make_scheduler().upcoming(start, 40)),
                                 default_schedule_v610(start, 40))

    def test_empty_missing_and_null_closures_change_nothing(self):
        plain = {key: value for key, value in SCHEDULE.items() if key != "closures"}
        variants = (plain, with_closures(None), with_closures({}),
                    with_closures({"dates": [], "ranges": []}),
                    with_closures({"dates": None, "ranges": None}))
        for settings in variants:
            for start in self.STARTS:
                with self.subTest(settings=settings.get("closures"), start=start):
                    self.assertEqual(fields(make_scheduler(settings).upcoming(start, 40)),
                                     default_schedule_v610(start, 40))

    def test_events_for_sixty_days_and_next_event_are_equal_with_and_without_closures(self):
        plain = {key: value for key, value in SCHEDULE.items() if key != "closures"}
        before, after = make_scheduler(plain), make_scheduler()
        for offset in range(60):
            day = WEDNESDAY.date() + timedelta(days=offset)
            self.assertEqual(after.events_for_date(day), before.events_for_date(day))
            self.assertIsNone(after.skip_reason(day))
        for start in self.STARTS:
            self.assertEqual(after.next_event(start), before.next_event(start))

    def test_unusable_closure_entries_do_not_change_the_schedule(self):
        """書き方の正しくない項目は無視される（その日は鳴る）。ERROR は 1 項目 1 回だけ。"""
        settings = with_closures({"dates": ["2026/08/26", 5, None, {"date": "あした"}],
                                  "ranges": [{"from": "2026-08-31", "to": "2026-08-26"}, "x"]})
        with logs_enabled(), self.assertLogs("chime.closures", level="ERROR") as captured:
            scheduler = make_scheduler(settings)
        self.assertEqual(len(captured.records), 6)
        self.assertEqual(len(scheduler.closures.problems), 6)
        for start in self.STARTS:
            self.assertEqual(fields(scheduler.upcoming(start, 40)),
                             default_schedule_v610(start, 40))
        self.assertEqual(len(scheduler.events_for_date(WEDNESDAY.date())), 8)

    def test_a_non_object_closures_section_does_not_change_the_schedule(self):
        for closures in ("2026-08-26", ["2026-08-26"], 5, True):
            with self.subTest(closures=closures):
                with logs_enabled(), self.assertLogs("chime.closures", level="ERROR"):
                    scheduler = make_scheduler(with_closures(closures))
                self.assertEqual(fields(scheduler.upcoming(WEDNESDAY, 40)),
                                 default_schedule_v610(WEDNESDAY, 40))

    def test_the_lookahead_covers_a_year(self):
        self.assertEqual(MAX_LOOKAHEAD_DAYS, 400)


class ClosureEventsTest(unittest.TestCase):
    """休館日（単日・期間）は、時報も閉館放送も鳴らさない。"""

    def test_a_single_closed_date_has_no_events_and_its_neighbours_do(self):
        scheduler = make_scheduler(with_closures({"dates": ["2026-08-26"]}))
        self.assertEqual(scheduler.events_for_date(date(2026, 8, 26)), [])
        self.assertEqual(len(scheduler.events_for_date(date(2026, 8, 25))), 8)
        self.assertEqual(len(scheduler.events_for_date(date(2026, 8, 27))), 8)

    def test_a_range_includes_both_ends(self):
        scheduler = make_scheduler(with_closures(
            {"ranges": [{"from": "2026-08-26", "to": "2026-08-28", "name": "学園祭"}]}))
        self.assertEqual(len(scheduler.events_for_date(date(2026, 8, 25))), 8)
        for day in (date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)):
            with self.subTest(day=day):
                self.assertEqual(scheduler.events_for_date(day), [])
        self.assertEqual(len(scheduler.events_for_date(date(2026, 8, 31))), 8)

    def test_next_event_and_upcoming_skip_the_closed_days(self):
        scheduler = make_scheduler(with_closures({"dates": ["2026-08-26", "2026-08-27"]}))
        self.assertEqual(scheduler.next_event(WEDNESDAY).at,
                         datetime(2026, 8, 28, 10, 0, tzinfo=TZ))
        self.assertEqual({event.day for event in scheduler.upcoming(WEDNESDAY, 9)},
                         {"2026-08-28", "2026-08-31"})

    def test_a_closure_that_started_today_stops_the_catch_up_too(self):
        scheduler = make_scheduler(with_closures({"dates": ["2026-08-26"]}))
        late = datetime(2026, 8, 26, 10, 0, 30, tzinfo=TZ)
        self.assertEqual(scheduler.next_event(late).at, datetime(2026, 8, 27, 10, 0, tzinfo=TZ))

    def test_a_closure_never_adds_events_to_a_day_the_rules_skip(self):
        scheduler = make_scheduler(with_closures({"dates": ["2026-08-29"]}))
        self.assertEqual(scheduler.events_for_date(SATURDAY.date()), [])

    def test_a_closure_long_enough_to_hide_the_next_event_for_fifty_days(self):
        # 2026-08-26(水) から 50 日間（〜2026-10-14）休む。以前の探索は 30 日までだった。
        scheduler = make_scheduler(with_closures(
            {"ranges": [{"from": "2026-08-26", "to": "2026-10-14", "name": "改修工事"}]}))
        self.assertEqual((date(2026, 10, 14) - WEDNESDAY.date()).days + 1, 50)
        event = scheduler.next_event(WEDNESDAY)
        self.assertEqual(event.at, datetime(2026, 10, 15, 10, 0, tzinfo=TZ))
        self.assertEqual(scheduler.upcoming(WEDNESDAY, limit=1)[0].at, event.at)
        self.assertEqual(len(scheduler.upcoming(WEDNESDAY, limit=20)), 20)

    def test_the_search_reaches_exactly_the_last_day_of_the_lookahead(self):
        last = WEDNESDAY.date() + timedelta(days=MAX_LOOKAHEAD_DAYS - 1)
        self.assertLess(last.weekday(), 5)
        before_last = (last - timedelta(days=1)).isoformat()
        found = make_scheduler(with_closures(
            {"ranges": [{"from": WEDNESDAY.date().isoformat(), "to": before_last}]}))
        self.assertEqual(found.next_event(WEDNESDAY).at,
                         datetime(last.year, last.month, last.day, 10, 0, tzinfo=TZ))
        hidden = make_scheduler(with_closures(
            {"ranges": [{"from": WEDNESDAY.date().isoformat(), "to": last.isoformat()}]}))
        self.assertIsNone(hidden.next_event(WEDNESDAY))
        self.assertEqual(hidden.upcoming(WEDNESDAY, 5), [])

    def test_closed_days_are_not_expanded_into_events(self):
        scheduler = make_scheduler(with_closures(
            {"ranges": [{"from": "2026-08-26", "to": "2027-09-28"}]}))
        original = Scheduler._rule_events
        with mock.patch.object(Scheduler, "_rule_events", autospec=True,
                               side_effect=original) as rule_events:
            event = scheduler.next_event(WEDNESDAY)
        self.assertEqual(event.at, datetime(2027, 9, 29, 10, 0, tzinfo=TZ))
        self.assertEqual(rule_events.call_count, 1)

    def test_the_calendar_is_exposed_with_its_problems(self):
        with logs_enabled(), self.assertLogs("chime.closures", level="ERROR"):
            scheduler = make_scheduler(with_closures({"dates": ["2026-08-26", "bad"]}))
        self.assertEqual(len(scheduler.closures.closures), 1)
        self.assertEqual(len(scheduler.closures.problems), 1)
        self.assertEqual(scheduler.events_for_date(date(2026, 8, 26)), [])


class SkipReasonTest(unittest.TestCase):
    CLOSURES = {"dates": [{"date": "2026-08-26", "name": "学園祭"}, "2026-08-27"],
                "ranges": [{"from": "2026-08-29", "to": "2026-08-31", "name": "棚卸し"}]}

    def test_the_reason_names_the_closure(self):
        scheduler = make_scheduler(with_closures(self.CLOSURES))
        self.assertEqual(scheduler.skip_reason(date(2026, 8, 26)), "休館: 学園祭")

    def test_an_unnamed_closure_is_just_closed(self):
        scheduler = make_scheduler(with_closures(self.CLOSURES))
        self.assertEqual(scheduler.skip_reason(date(2026, 8, 27)), "休館")

    def test_open_days_have_no_reason(self):
        scheduler = make_scheduler(with_closures(self.CLOSURES))
        self.assertIsNone(scheduler.skip_reason(date(2026, 8, 25)))
        self.assertIsNone(scheduler.skip_reason(date(2026, 8, 28)))
        self.assertIsNone(make_scheduler().skip_reason(WEDNESDAY.date()))

    def test_days_the_rules_would_not_chime_on_have_no_reason(self):
        scheduler = make_scheduler(with_closures(self.CLOSURES))
        # 土曜（8/29）・日曜（8/30）は期間の中でも、もともと鳴らない日。月曜（8/31）だけが休館で鳴らない日。
        self.assertIsNone(scheduler.skip_reason(date(2026, 8, 29)))
        self.assertIsNone(scheduler.skip_reason(date(2026, 8, 30)))
        self.assertEqual(scheduler.skip_reason(date(2026, 8, 31)), "休館: 棚卸し")

    def test_a_reason_needs_a_section_that_is_on(self):
        closures = {"dates": ["2026-08-26"]}
        both_off = with_closures(closures, hourly=dict(SCHEDULE["hourly"], enabled=False),
                                 closing=dict(SCHEDULE["closing"], enabled=False))
        self.assertIsNone(make_scheduler(both_off).skip_reason(date(2026, 8, 26)))
        only_closing = with_closures(closures, hourly=dict(SCHEDULE["hourly"], enabled=False))
        self.assertEqual(make_scheduler(only_closing).skip_reason(date(2026, 8, 26)), "休館")
        no_hours = with_closures(closures, hourly=dict(SCHEDULE["hourly"], start_hour=16, end_hour=10),
                                 closing=dict(SCHEDULE["closing"], enabled=False))
        self.assertIsNone(make_scheduler(no_hours).skip_reason(date(2026, 8, 26)))

    def test_a_reason_follows_each_sections_own_weekdays(self):
        closures = {"dates": ["2026-08-26", "2026-08-29"]}
        settings = with_closures(closures, hourly=dict(SCHEDULE["hourly"], weekdays=[2]),
                                 closing=dict(SCHEDULE["closing"], weekdays=[5]))
        scheduler = make_scheduler(settings)
        self.assertEqual(scheduler.skip_reason(date(2026, 8, 26)), "休館")   # 水: 時報
        self.assertEqual(scheduler.skip_reason(date(2026, 8, 29)), "休館")   # 土: 閉館放送
        self.assertIsNone(scheduler.skip_reason(date(2026, 8, 27)))

    def test_a_skipped_day_is_exactly_a_day_that_loses_its_events(self):
        scheduler = make_scheduler(with_closures(self.CLOSURES))
        plain = make_scheduler()
        for offset in range(20):
            day = WEDNESDAY.date() + timedelta(days=offset)
            lost = bool(plain.events_for_date(day)) and not scheduler.events_for_date(day)
            self.assertEqual(scheduler.skip_reason(day) is not None, lost, day)
            self.assertEqual(scheduler.skip_closure(day) is not None, lost, day)

    def test_the_closure_behind_a_skipped_day_is_the_one_the_reason_comes_from(self):
        scheduler = make_scheduler(with_closures(self.CLOSURES))
        for offset in range(20):
            day = WEDNESDAY.date() + timedelta(days=offset)
            closure = scheduler.skip_closure(day)
            self.assertEqual(None if closure is None else closure.reason,
                             scheduler.skip_reason(day), day)
        self.assertEqual(scheduler.skip_closure(date(2026, 8, 26)).name, "学園祭")
        self.assertEqual(scheduler.skip_closure(date(2026, 8, 27)).name, "")
        self.assertEqual(scheduler.skip_closure(date(2026, 8, 31)).name, "棚卸し")
        self.assertIsNone(scheduler.skip_closure(date(2026, 8, 29)))   # 土曜: もともと鳴らない


class FormatScheduleTest(unittest.TestCase):
    NEW_YEAR = {"ranges": [{"from": "2026-12-28", "to": "2027-01-04", "name": "年末年始"}]}

    def test_without_closures_it_is_exactly_format_events(self):
        scheduler = make_scheduler()
        for start in DefaultScheduleUnchangedTest.STARTS:
            with self.subTest(start=start):
                self.assertEqual(format_schedule(scheduler, start, 10),
                                 format_events(scheduler.upcoming(start, 10)))

    def test_one_line_for_each_skipped_day_before_the_next_event(self):
        scheduler = make_scheduler(with_closures(self.NEW_YEAR))
        start = datetime(2026, 12, 25, 17, 0, tzinfo=TZ)
        self.assertEqual(format_schedule(scheduler, start, 2).splitlines(), [
            "  休館 2026-12-28（月） 年末年始",
            "  休館 2026-12-29（火） 年末年始",
            "  休館 2026-12-30（水） 年末年始",
            "  休館 2026-12-31（木） 年末年始",
            "  休館 2027-01-01（金） 年末年始",
            "  休館 2027-01-04（月） 年末年始",
            "  - 時報 2027-01-05 10:00:00（再生開始 09:59:57）",
            "  - 時報 2027-01-05 11:00:00（再生開始 10:59:57）",
        ])

    def test_weekend_days_inside_a_closure_are_not_listed(self):
        scheduler = make_scheduler(with_closures(self.NEW_YEAR))
        text = format_schedule(scheduler, datetime(2026, 12, 25, 17, 0, tzinfo=TZ), 2)
        for day in ("2026-12-26", "2026-12-27", "2027-01-02", "2027-01-03"):
            self.assertNotIn(day, text)

    def test_an_unnamed_closure_line_ends_with_the_weekday(self):
        scheduler = make_scheduler(with_closures({"dates": ["2026-08-27"]}))
        text = format_schedule(scheduler, WEDNESDAY, 10)
        # 名前がないので、日付と曜日で行を終える（空白や「休館:」を残さない）。
        self.assertEqual([line for line in text.splitlines() if line.startswith("  休館")],
                         ["  休館 2026-08-27（木）"])

    def test_a_skipped_day_sits_between_the_events_around_it(self):
        scheduler = make_scheduler(with_closures(
            {"dates": [{"date": "2026-08-27", "name": "学園祭"}]}))
        lines = format_schedule(scheduler, WEDNESDAY, 10).splitlines()
        events = scheduler.upcoming(WEDNESDAY, 10)
        self.assertEqual(len(lines), 11)
        self.assertEqual(lines[8], "  休館 2026-08-27（木） 学園祭")
        self.assertEqual(lines[:8], format_events(events[:8]).splitlines())
        self.assertEqual(lines[9:], format_events(events[8:]).splitlines())
        self.assertEqual([line for line in lines if not line.startswith("  休館")],
                         format_events(events).splitlines())

    def test_today_is_listed_when_it_is_closed(self):
        scheduler = make_scheduler(with_closures({"dates": ["2026-08-26"]}))
        noon = datetime(2026, 8, 26, 12, 30, tzinfo=TZ)
        lines = format_schedule(scheduler, noon, 1).splitlines()
        self.assertEqual(lines, ["  休館 2026-08-26（水）",
                                 "  - 時報 2026-08-27 10:00:00（再生開始 09:59:57）"])

    def test_closures_after_the_last_listed_event_are_not_shown(self):
        scheduler = make_scheduler(with_closures({"dates": ["2026-08-27"]}))
        text = format_schedule(scheduler, WEDNESDAY, 3)
        self.assertEqual(text, format_events(scheduler.upcoming(WEDNESDAY, 3)))
        self.assertNotIn("休館", text)

    def test_when_the_search_runs_out_every_closed_day_up_to_its_end_is_listed(self):
        last = WEDNESDAY.date() + timedelta(days=MAX_LOOKAHEAD_DAYS - 1)
        hourly = dict(SCHEDULE["hourly"], start_hour=10, end_hour=10, weekdays=[2])
        settings = with_closures({"dates": [last.isoformat()]}, hourly=hourly,
                                 closing=dict(SCHEDULE["closing"], enabled=False))
        scheduler = make_scheduler(settings)
        lines = format_schedule(scheduler, WEDNESDAY, 1000).splitlines()
        self.assertEqual(lines[-1], "  休館 {0}（水）".format(last.isoformat()))
        self.assertEqual(sum(1 for line in lines if line.startswith("  休館")), 1)
        self.assertEqual(len(lines), len(scheduler.upcoming(WEDNESDAY, 1000)) + 1)

    def test_with_no_events_at_all_the_skipped_days_come_first_then_the_notice(self):
        scheduler = make_scheduler(with_closures(
            {"ranges": [{"from": "2026-08-26", "to": "2030-01-01"}]}))
        lines = format_schedule(scheduler, WEDNESDAY, 5).splitlines()
        weekdays = sum(1 for offset in range(MAX_LOOKAHEAD_DAYS)
                       if (WEDNESDAY.date() + timedelta(days=offset)).weekday() < 5)
        self.assertEqual(len(lines), weekdays + 1)
        self.assertTrue(all(line.startswith("  休館 ") for line in lines[:-1]))
        self.assertEqual(lines[-1], format_events([]))

    def test_nothing_scheduled_and_nothing_closed_is_the_plain_notice(self):
        settings = with_closures({"dates": ["2026-08-26"]},
                                 hourly=dict(SCHEDULE["hourly"], enabled=False),
                                 closing=dict(SCHEDULE["closing"], enabled=False))
        self.assertEqual(format_schedule(make_scheduler(settings), WEDNESDAY, 5),
                         format_events([]))

    def test_the_start_defaults_to_now(self):
        scheduler = Scheduler(with_closures({"dates": ["2026-08-27"]}), TZ, 3.0,
                              clock=lambda: WEDNESDAY)
        self.assertEqual(format_schedule(scheduler, limit=10),
                         format_schedule(scheduler, WEDNESDAY, 10))

    def test_a_closed_day_line_comes_before_the_event_that_starts_on_that_day(self):
        # 0 時の時報は前日の 23:59:57 に鳴り始める。その前日が休館日だと、「休館」の行（その日の日付）と
        # 予定の行（再生を始める日の日付）が同じ日になる。休館の行を先に置く（ソートの並びを変えると崩れる）。
        tuesday = datetime(2026, 10, 13, 12, 0, tzinfo=TZ)
        settings = with_closures(
            {"dates": ["2026-10-14"]},   # 水曜が休館
            hourly=dict(SCHEDULE["hourly"], start_hour=0, end_hour=0, skip_hours=[]),
            closing=dict(SCHEDULE["closing"], enabled=False))
        lines = format_schedule(make_scheduler(settings), tuesday, 1).splitlines()
        self.assertEqual(lines, ["  休館 2026-10-14（水）",
                                 "  - 時報 2026-10-15 00:00:00（再生開始 23:59:57）"])

    def test_the_closed_day_row_starts_with_closure_not_with_the_pause_word(self):
        # 現地の操作で一時的に止める「休止」（--pause-today など）と、休館日の行を取り違えない。
        scheduler = make_scheduler(with_closures(
            {"dates": [{"date": "2026-08-27", "name": "学園祭"}, "2026-08-28"]}))
        text = format_schedule(scheduler, WEDNESDAY, 10)
        rows = [line for line in text.splitlines() if not line.startswith("  - ")]
        self.assertEqual(rows, ["  休館 2026-08-27（木） 学園祭", "  休館 2026-08-28（金）"])
        self.assertNotIn("休止", text)

    def test_a_closed_day_row_has_no_reason_prefix_or_trailing_space(self):
        scheduler = make_scheduler(with_closures({"dates": ["2026-08-27"], "ranges": [
            {"from": "2026-09-01", "to": "2026-09-01", "name": "棚卸し"}]}))
        for line in format_schedule(scheduler, WEDNESDAY, 20).splitlines():
            if line.startswith("  休館"):
                self.assertNotIn("休館:", line)
                self.assertEqual(line, line.rstrip())

    def test_format_events_is_unchanged_by_closures(self):
        scheduler = make_scheduler(with_closures({"dates": ["2026-08-27"]}))
        text = format_events(scheduler.upcoming(WEDNESDAY, 10))
        self.assertNotIn("休館", text)
        self.assertEqual(len(text.splitlines()), 10)


#: 偽物の呼び出しを数える上限。本来は数回で終わる待機が終わらない（退行した）ときに、固まらず失敗にする。
CALL_LIMIT = 100


class RunawayWait(BaseException):
    """待機が終わらない（偽物が ``CALL_LIMIT`` 回を超えて呼ばれた）。

    ``Exception`` の子にしない。``sleep_until`` は割り込みの確認の ``Exception`` を飲み込んで
    待機を続けるので、``Exception`` の子だと打ち切りも飲み込まれ、結局固まってしまう。
    """


class SleepUntilInterruptTest(unittest.TestCase):
    """``interrupt``: 待つ間に放送を取りやめる合図。1 回分の待機ごとに確かめる。"""

    class Clock:
        """時刻を進めるだけの偽の時計（``time.sleep`` の代わりに ``sleep`` を差し込む）。

        ``sleep`` が ``CALL_LIMIT`` 回を超えて呼ばれたら ``RunawayWait``（待機が終わらない退行で固まらない）。
        """

        def __init__(self, moment):
            self.moment = moment
            self.sleeps = []

        def now(self):
            return self.moment

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            if len(self.sleeps) > CALL_LIMIT:
                raise RunawayWait("sleep が {0} 回を超えて呼ばれました（待機が終わりません）".format(CALL_LIMIT))
            self.moment += timedelta(seconds=seconds)

    def setUp(self):
        self.clock = self.Clock(WEDNESDAY)
        self.scheduler = Scheduler(dict(SCHEDULE, max_sleep_seconds=30.0), TZ, 3.0,
                                   clock=self.clock.now)
        patcher = mock.patch("chime.scheduler.time.sleep", self.clock.sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait(self, seconds, interrupt, **kwargs):
        return self.scheduler.sleep_until(WEDNESDAY + timedelta(seconds=seconds),
                                          interrupt=interrupt, **kwargs)

    def test_it_is_checked_before_every_chunk_and_stops_the_wait_early(self):
        answers = iter([False, False, True])
        calls = []

        def interrupt():
            calls.append(self.clock.moment)
            return next(answers)

        self.assertFalse(self.wait(100, interrupt))
        self.assertEqual(self.clock.sleeps, [30.0, 30.0])
        self.assertEqual(calls, [WEDNESDAY, WEDNESDAY + timedelta(seconds=30),
                                 WEDNESDAY + timedelta(seconds=60)])

    def test_the_chunk_length_is_unchanged(self):
        self.assertTrue(self.wait(100, lambda: False))
        self.assertEqual(self.clock.sleeps, [30.0, 30.0, 30.0, 10.0])

    def test_without_an_interrupt_the_wait_is_the_same(self):
        self.assertTrue(self.wait(100, None))
        self.assertEqual(self.clock.sleeps, [30.0, 30.0, 30.0, 10.0])

    def test_it_is_checked_once_more_when_the_wait_is_over(self):
        calls = []
        self.assertTrue(self.wait(100, lambda: calls.append(1) or False))
        self.assertEqual(len(calls), 5)   # 4 回の待機の前と、待ち終えた直後

    def test_an_interrupt_that_is_already_true_returns_without_waiting(self):
        self.assertFalse(self.wait(100, lambda: True))
        self.assertEqual(self.clock.sleeps, [])

    def test_an_interrupt_is_honoured_even_when_the_target_has_passed(self):
        self.assertFalse(self.wait(-5, lambda: True))
        self.assertTrue(self.wait(-5, lambda: False))

    def test_the_fake_clock_cuts_off_a_wait_that_never_ends(self):
        # 1 回に 0 秒しか待たない設定では、時計が進まず、待機は終わらない。固まらずに失敗にする。
        stuck = Scheduler(dict(SCHEDULE, max_sleep_seconds=0.0), TZ, 3.0, clock=self.clock.now)
        with self.assertRaises(RunawayWait):
            stuck.sleep_until(WEDNESDAY + timedelta(seconds=100), interrupt=lambda: False)
        self.assertEqual(len(self.clock.sleeps), CALL_LIMIT + 1)

    def test_an_interrupt_is_honoured_even_when_a_precise_wait_has_already_passed(self):
        # 残り 0.25 秒以内だけ確認しない決まりは、「まだ残りがあるとき」に限る。過ぎていれば確かめる。
        calls = []
        self.assertFalse(self.wait(-5, lambda: calls.append(1) or True, precise=True))
        self.assertEqual(calls, [1])

    def test_a_wait_that_is_not_precise_asks_the_interrupt_even_in_the_last_quarter_second(self):
        # ポーンの最終調整（精密な待機の残り 0.25 秒以内）だけが、確認を省く。ふつうの待機は省かない。
        calls = []
        result = self.wait(0.2, lambda: calls.append(self.clock.moment) or True, precise=False)
        self.assertFalse(result)
        self.assertEqual(calls, [WEDNESDAY])    # 目標の時刻より前（残りがあるうち）に確かめている
        self.assertEqual(self.clock.sleeps, [])

    def test_a_stop_request_wins_and_the_interrupt_is_not_asked(self):
        stop = threading.Event()
        stop.set()
        calls = []
        self.assertFalse(self.wait(100, lambda: calls.append(1) or False, stop=stop))
        self.assertEqual(calls, [])

    def test_an_interrupt_that_raises_is_logged_once_and_the_wait_goes_on(self):
        calls = []

        def interrupt():
            calls.append(1)
            raise RuntimeError("pause.json が読めない")

        with logs_enabled(), self.assertLogs("chime.scheduler", level="ERROR") as captured:
            self.assertTrue(self.wait(100, interrupt))
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("割り込みの確認に失敗", captured.output[0])
        self.assertIn("pause.json が読めない", captured.output[0])
        self.assertEqual(self.clock.sleeps, [30.0, 30.0, 30.0, 10.0])

    def test_an_interrupt_that_recovers_still_stops_the_wait(self):
        answers = iter([RuntimeError("一時的"), True])

        def interrupt():
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with logs_enabled(), self.assertLogs("chime.scheduler", level="ERROR"):
            self.assertFalse(self.wait(100, interrupt))
        self.assertEqual(self.clock.sleeps, [30.0])


class SleepUntilInterruptTimingTest(unittest.TestCase):
    """本物の時計で、割り込みが待機を速やかに終わらせ、ポーンの直前を邪魔しないこと。

    1 時間先まで待つので、退行して割り込みや停止要求が効かなくなると、テストは 1 時間固まる。
    割り込みの確認が ``CALL_LIMIT`` 回を超えたら ``RunawayWait`` で打ち切り、失敗にする
    （確認は 0.05 秒ごとなので、固まるかわりに数秒で失敗する）。
    """

    @staticmethod
    def counting(inner):
        """``interrupt`` に渡す関数。``CALL_LIMIT`` 回を超えて呼ばれたら ``RunawayWait``。"""
        calls = []

        def checked():
            calls.append(1)
            if len(calls) > CALL_LIMIT:
                raise RunawayWait("割り込みの確認が {0} 回を超えました（待機が終わりません）".format(CALL_LIMIT))
            return inner()

        return checked, calls

    def test_an_interrupt_that_turns_true_ends_a_long_wait_promptly(self):
        scheduler = make_scheduler(dict(SCHEDULE, max_sleep_seconds=0.05))
        flag = threading.Event()
        interrupt, calls = self.counting(flag.is_set)
        timer = threading.Timer(0.1, flag.set)
        timer.start()
        try:
            started = time.monotonic()
            result = scheduler.sleep_until(scheduler.now() + timedelta(hours=1),
                                           interrupt=interrupt)
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
        self.assertFalse(result)
        self.assertGreaterEqual(elapsed, 0.09)
        self.assertLess(elapsed, 1.0)
        self.assertLess(len(calls), 20)

    def test_the_stop_event_still_ends_the_wait_when_an_interrupt_is_given(self):
        scheduler = make_scheduler(dict(SCHEDULE, max_sleep_seconds=0.05))
        stop = threading.Event()
        interrupt, calls = self.counting(lambda: False)
        timer = threading.Timer(0.05, stop.set)
        timer.start()
        try:
            started = time.monotonic()
            result = scheduler.sleep_until(scheduler.now() + timedelta(hours=1), stop,
                                           interrupt=interrupt)
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
        self.assertFalse(result)
        self.assertLess(elapsed, 1.0)
        self.assertLess(len(calls), 20)

    def test_the_guard_turns_a_wait_that_never_ends_into_a_failure(self):
        # 割り込みが一度も効かない待機（退行）は、固まらずに RunawayWait になる。
        scheduler = make_scheduler(dict(SCHEDULE, max_sleep_seconds=0.001))
        interrupt, calls = self.counting(lambda: False)
        with self.assertRaises(RunawayWait):
            scheduler.sleep_until(scheduler.now() + timedelta(hours=1), interrupt=interrupt)
        self.assertEqual(len(calls), CALL_LIMIT + 1)

    def test_the_interrupt_is_not_asked_during_the_final_precise_adjustment(self):
        scheduler = make_scheduler()
        calls = []
        target = scheduler.now() + timedelta(milliseconds=120)
        self.assertTrue(scheduler.sleep_until(target, precise=True,
                                              interrupt=lambda: calls.append(1) or True))
        self.assertEqual(calls, [])
        self.assertGreaterEqual((scheduler.now() - target).total_seconds(), 0.0)

    def test_a_precise_wait_asks_the_interrupt_before_the_final_adjustment(self):
        scheduler = make_scheduler(dict(SCHEDULE, max_sleep_seconds=0.05))
        calls = []
        target = scheduler.now() + timedelta(milliseconds=600)
        self.assertTrue(scheduler.sleep_until(target, precise=True,
                                              interrupt=lambda: calls.append(1) or False))
        self.assertGreaterEqual(len(calls), 2)


class LookaheadPerformanceTest(unittest.TestCase):
    """探索を 400 日にしても、予定の取得は一瞬で終わる（この機でおよそ 10 ms 以下）。"""

    #: 実測の数倍の余裕を見た上限。遅い CI でも落ちず、日数ぶんの再計算が入ると落ちる値。
    LIMIT_SECONDS = 0.25

    @staticmethod
    def best_of(action, times=5):
        best = None
        for _ in range(times):
            started = time.perf_counter()
            action()
            elapsed = time.perf_counter() - started
            best = elapsed if best is None else min(best, elapsed)
        return best

    def test_default_config(self):
        scheduler = make_scheduler()
        self.assertLess(self.best_of(lambda: scheduler.upcoming(WEDNESDAY, 40)), self.LIMIT_SECONDS)
        self.assertLess(self.best_of(lambda: scheduler.next_event(WEDNESDAY)), self.LIMIT_SECONDS)

    def test_every_event_already_fired_scans_the_whole_lookahead(self):
        scheduler = make_scheduler()
        self.assertIsNone(scheduler.next_event(WEDNESDAY, is_fired=lambda event: True))
        self.assertLess(
            self.best_of(lambda: scheduler.next_event(WEDNESDAY, is_fired=lambda event: True)),
            self.LIMIT_SECONDS)

    def test_nothing_scheduled(self):
        scheduler = make_scheduler(dict(SCHEDULE, hourly=dict(SCHEDULE["hourly"], enabled=False),
                                        closing=dict(SCHEDULE["closing"], enabled=False)))
        self.assertIsNone(scheduler.next_event(WEDNESDAY))
        self.assertLess(self.best_of(lambda: scheduler.next_event(WEDNESDAY)), self.LIMIT_SECONDS)

    def test_a_closure_covering_almost_the_whole_lookahead(self):
        scheduler = make_scheduler(with_closures(
            {"ranges": [{"from": "2026-08-26", "to": "2027-09-28"}]}))
        self.assertEqual(scheduler.next_event(WEDNESDAY).at, datetime(2027, 9, 29, 10, 0, tzinfo=TZ))
        self.assertLess(self.best_of(lambda: scheduler.next_event(WEDNESDAY)), self.LIMIT_SECONDS)
        self.assertLess(self.best_of(lambda: format_schedule(scheduler, WEDNESDAY, 10)),
                        self.LIMIT_SECONDS)

    def test_a_year_of_single_dates(self):
        dates = [(WEDNESDAY.date() + timedelta(days=offset)).isoformat() for offset in range(0, 399, 2)]
        scheduler = make_scheduler(with_closures({"dates": dates}))
        self.assertLess(
            self.best_of(lambda: scheduler.next_event(WEDNESDAY, is_fired=lambda event: True)),
            self.LIMIT_SECONDS)
        self.assertLess(self.best_of(lambda: format_schedule(scheduler, WEDNESDAY, 400)),
                        self.LIMIT_SECONDS)


if __name__ == "__main__":
    unittest.main()
