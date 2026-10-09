"""スケジューリングのテスト。"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from chime.config import DEFAULT_CONFIG
from chime.scheduler import Scheduler, format_events, hourly_hours

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


if __name__ == "__main__":
    unittest.main()
