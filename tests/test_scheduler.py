"""スケジューリングのテスト。"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from chime.config import DEFAULT_CONFIG
from chime.scheduler import Scheduler, format_events

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
        self.assertEqual((closing[0].hour, closing[0].minute), (16, 57))

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
