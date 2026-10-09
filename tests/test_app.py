"""常駐ループ（ChimeApp）のテスト。"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import socket
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta
from unittest import mock

from tests.support import (SHIPPED_QUOTES, RecordingPlayer, block_network,
                           logs_enabled, make_event, make_wav)

from chime import buildinfo, phrases
from chime.app import ChimeApp, PlayOutcome, describe_exception, exception_text
from chime.audio import Player, PlaybackError
from chime.config import DEFAULT_CONFIG, Config
from chime.history import History
from chime.scheduler import Event, Scheduler
from chime.sequence import PlaybackPlan
from chime.audio import Segment
from chime.state import State
from chime.tts import prerecorded_filename


class BadStr(Exception):
    """``str()`` が例外を出す例外（説明の文字列を作れない）。"""

    def __str__(self):
        raise RuntimeError("__str__ の中で失敗")


class BadStrPlaybackError(PlaybackError):
    """``str()`` が例外を出す ``PlaybackError``。"""

    def __str__(self):
        raise RuntimeError("__str__ の中で失敗")


#: 常駐ループの偽物（予定を返す関数・待機の関数）を呼んでよい回数の上限。
#: 本物のループは止まるまで回り続けるので、直しが戻ったとき（退行したとき）に
#: テストが固まらないよう、これを超えたら打ち切る。
CALL_LIMIT = 20


class RunawayLoop(BaseException):
    """常駐ループが止まらない（偽物が ``CALL_LIMIT`` 回を超えて呼ばれた）。

    ``Exception`` の子にしない。ループは ``except Exception`` で失敗を受けて続ける
    ので、``Exception`` の子だと打ち切りも飲み込まれ、結局固まってしまう。
    """


class CallGuard:
    """偽物の呼び出し回数を名前ごとに数え、``CALL_LIMIT`` を超えたら ``RunawayLoop``。"""

    def __init__(self):
        self.counts = {}

    def tick(self, name):
        """``name`` の呼び出しを 1 回数え、通算の回数を返す。"""
        self.counts[name] = self.counts.get(name, 0) + 1
        if self.counts[name] > CALL_LIMIT:
            raise RunawayLoop("{0} が {1} 回を超えて呼ばれました（ループが止まりません）".format(
                name, CALL_LIMIT))
        return self.counts[name]


class StubBuilder:
    """``SequenceBuilder`` のスタブ。

    ``explode`` なら ``build`` が例外を出す。そのとき ``run_event`` は
    ``build_minimal`` の結果（``minimal_segments``）で鳴らす。``quote`` は
    プランに載せる「選んだひとこと」。``silent`` / ``warnings`` / ``missing`` は、
    プランに載せる「無音になった文言」「警告」「積めなかった必須の部品」。
    ``error`` は、``build`` / ``build_minimal`` が出す例外（省略すれば ``RuntimeError``）。
    """

    def __init__(self, segments, explode=False, quote=None, minimal_segments=None,
                 explode_minimal=False, silent=None, warnings=None, missing=None, error=None):
        self.segments = segments
        self.explode = explode
        self.quote = quote
        self.minimal_segments = minimal_segments if minimal_segments is not None else []
        self.explode_minimal = explode_minimal
        self.silent = silent or []
        self.warnings = warnings or []
        self.missing = missing or []
        self.error = error
        self.built = []
        self.built_minimal = []

    def build(self, event):
        self.built.append(event)
        if self.explode:
            raise self.error or RuntimeError("組み立て失敗")
        return PlaybackPlan(event=event, segments=list(self.segments), quote=self.quote,
                            silent=list(self.silent), warnings=list(self.warnings),
                            missing=list(self.missing))

    def build_minimal(self, event):
        self.built_minimal.append(event)
        if self.explode_minimal:
            raise self.error or RuntimeError("最小プランも組み立て失敗")
        return PlaybackPlan(event=event, segments=list(self.minimal_segments))


class AppTestCase(unittest.TestCase):
    def setUp(self):
        block_network(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "assets"), exist_ok=True)
        self.wav = make_wav(os.path.join(self.root, "assets", "beep.wav"), seconds=0.01)

        config = Config(DEFAULT_CONFIG, base_dir=self.root)
        config.data["audio"]["gap_ms"] = 0
        # 起動時の「作り置きの声」の集計（log_environment）が読むので、同梱のものを指す。
        config.data["quotes"]["file"] = SHIPPED_QUOTES
        self.app = ChimeApp(config, backend="mock")
        self.player = RecordingPlayer()
        self.app._player = self.player
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")])

    def tearDown(self):
        self.tmp.cleanup()

    def past_event(self, seconds_ago: float = 5.0) -> Event:
        moment = self.app.now() - timedelta(seconds=seconds_ago)
        return make_event(moment)


class RunEventTest(AppTestCase):
    def test_plays_and_marks_fired(self):
        event = self.past_event()
        self.app.run_event(event)
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_dry_run_does_not_play(self):
        self.app.dry_run = True
        self.app.run_event(self.past_event())
        self.assertEqual(self.player.played, [])

    def test_stop_request_cancels_playback(self):
        moment = self.app.now() + timedelta(hours=1)
        event = make_event(moment)
        self.app.stop_event.set()
        self.app.run_event(event)
        self.assertEqual(self.player.played, [])
        self.assertFalse(self.app.state.is_fired(event.key, event.day))

    def test_playback_error_does_not_propagate(self):
        self.app.builder = StubBuilder([Segment("/nonexistent.wav", label="欠落")])
        self.app.run_event(self.past_event())  # 例外は握りつぶされる
        self.assertEqual(self.player.played, [])

    def test_the_wait_for_the_play_time_is_precise(self):
        """再生の時刻までの待機は、精密に（``precise=True``）待つ。準備の待機とは別。"""
        event = self.past_event()
        calls = []
        real = self.app.scheduler.sleep_until

        def spying(target, stop=None, precise=False):
            calls.append((target, precise))
            return real(target, stop, precise)

        self.app.scheduler.sleep_until = spying
        self.app.run_event(event)
        self.assertEqual([call for call in calls if call[0] == event.play_at],
                         [(event.play_at, True)])


class RunEventDegradeTest(AppTestCase):
    """組み立ての失敗と、再生後の state の書き込み。"""

    def test_build_failure_plays_the_minimal_plan_and_marks_fired(self):
        minimal = Segment(self.wav, label="時報音だけ")
        self.app.builder = StubBuilder([], explode=True, minimal_segments=[minimal])
        event = self.past_event()
        self.app.run_event(event)
        self.assertEqual(self.app.builder.built_minimal, [event])
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_build_failure_is_logged_with_a_traceback(self):
        self.app.builder = StubBuilder([], explode=True)
        with logs_enabled():
            with self.assertLogs("chime.app", level="ERROR") as captured:
                self.app.run_event(self.past_event())
        self.assertTrue(any("組み立て失敗" in line for line in captured.output))

    def test_build_failure_log_is_an_error_with_a_fixed_message_and_traceback(self):
        self.app.builder = StubBuilder([], explode=True)
        with logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
            self.app.run_event(self.past_event())
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelname, "ERROR")
        self.assertEqual(
            record.getMessage(),
            "再生内容の組み立てに失敗しました（最小の内容で鳴らします）: 組み立て失敗")
        self.assertIsNotNone(record.exc_info)

    def test_a_failing_minimal_plan_escapes_run_event_and_marks_nothing(self):
        # 最小プランの組み立てまで失敗した場合、例外は run_event の外へ出る
        # （常駐ループ側が受け止めて、その回を再生済みにする）。
        self.app.builder = StubBuilder([], explode=True, explode_minimal=True)
        event = self.past_event()
        with self.assertRaises(RuntimeError):
            self.app.run_event(event)
        self.assertEqual(self.app.builder.built_minimal, [event])
        self.assertEqual(self.player.played, [])
        self.assertFalse(self.app.state.is_fired(event.key, event.day))

    def test_a_successful_build_does_not_use_the_minimal_plan(self):
        self.app.run_event(self.past_event())
        self.assertEqual(self.app.builder.built_minimal, [])

    def test_the_quote_is_remembered_after_a_successful_playback(self):
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")],
                                       quote="テストのひとこと")
        self.app.run_event(self.past_event())
        self.assertEqual(self.player.played, [self.wav])
        self.assertEqual(self.app.state.recent_quotes(), ["テストのひとこと"])

    def test_the_quote_is_remembered_before_the_event_is_marked_fired(self):
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")],
                                       quote="テストのひとこと")
        calls = mock.Mock()
        with mock.patch.object(self.app.state, "remember_quote", calls.remember), \
                mock.patch.object(self.app.state, "mark_fired", calls.mark_fired):
            event = self.past_event()
            self.app.run_event(event)
        self.assertEqual(calls.mock_calls, [
            mock.call.remember("テストのひとこと"),
            mock.call.mark_fired(event.key, event.day),
        ])

    def test_the_quote_is_not_remembered_when_playback_fails(self):
        # 鳴らせなかったひとことは「使った」ことにしない。ただし再生済みの
        # 記録（mark_fired）は従来どおり付ける（無限リトライにしない）。
        self.app.builder = StubBuilder([Segment("/nonexistent.wav", label="欠落")],
                                       quote="鳴らなかったひとこと")
        event = self.past_event()
        with mock.patch.object(self.app.state, "remember_quote") as remember:
            self.app.run_event(event)
        remember.assert_not_called()
        self.assertEqual(self.app.state.recent_quotes(), [])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_the_quote_is_not_remembered_when_nothing_was_played(self):
        # セグメントはあるが optional で全部欠けている（play が False を返す）。
        self.app.builder = StubBuilder(
            [Segment("/nonexistent.wav", label="任意", optional=True)],
            quote="鳴らなかったひとこと")
        self.app.run_event(self.past_event())
        self.assertEqual(self.app.state.recent_quotes(), [])

    def test_the_quote_is_not_remembered_when_the_player_raises(self):
        self.app._player = ExplodingPlayer({})
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")],
                                       quote="鳴らなかったひとこと")
        event = self.past_event()
        self.app.run_event(event)
        self.assertEqual(self.app.state.recent_quotes(), [])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_a_plan_without_a_quote_remembers_nothing(self):
        with mock.patch.object(self.app.state, "remember_quote") as remember:
            self.app.run_event(self.past_event())
        remember.assert_not_called()

    def test_the_quote_is_not_remembered_in_dry_run(self):
        self.app.dry_run = True
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")],
                                       quote="テストのひとこと")
        with mock.patch.object(self.app.state, "remember_quote") as remember:
            self.app.run_event(self.past_event())
        remember.assert_not_called()
        self.assertEqual(self.player.played, [])

    def test_the_minimal_plan_has_no_quote_to_remember(self):
        self.app.builder = StubBuilder([], explode=True,
                                       minimal_segments=[Segment(self.wav, label="時報音だけ")])
        self.app.run_event(self.past_event())
        self.assertEqual(self.app.state.recent_quotes(), [])

    def test_a_stop_request_before_playback_remembers_nothing(self):
        moment = self.app.now() + timedelta(hours=1)
        event = make_event(moment)
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")],
                                       quote="テストのひとこと")
        self.app.stop_event.set()
        self.app.run_event(event)
        self.assertEqual(self.app.state.recent_quotes(), [])
        self.assertFalse(self.app.state.is_fired(event.key, event.day))


class PendingEventHelpersTest(AppTestCase):
    """``_is_fired`` / ``_next_pending_event``: 選ぶときも再確認も同じ基準。"""

    def test_is_fired_reflects_the_state(self):
        event = self.past_event()
        self.assertFalse(self.app._is_fired(event))
        self.app.state.mark_fired(event.key, event.day)
        self.assertTrue(self.app._is_fired(event))

    def test_is_fired_is_per_key_and_day(self):
        event = self.past_event()
        self.app.state.mark_fired(event.key, event.day)
        other_hour = make_event(event.at, key="hourly:11", hour=11)
        self.assertFalse(self.app._is_fired(other_hour))

    def test_is_fired_reads_the_state_at_call_time(self):
        event = self.past_event()
        self.app.state.mark_fired(event.key, event.day)
        self.app.state = State(os.path.join(self.root, "swapped_state.json"))
        self.assertFalse(self.app._is_fired(event))

    def test_next_pending_event_passes_is_fired_as_the_only_keyword(self):
        sentinel = object()
        calls = []

        def fake_next(*args, **kwargs):
            calls.append((args, kwargs))
            return sentinel

        self.app.scheduler.next_event = fake_next
        self.assertIs(self.app._next_pending_event(), sentinel)
        self.assertEqual(calls, [((), {"is_fired": self.app._is_fired})])

    def test_next_pending_event_reads_the_scheduler_at_call_time(self):
        event = self.past_event()
        self.app.scheduler = mock.Mock()
        self.app.scheduler.next_event.return_value = event
        self.assertIs(self.app._next_pending_event(), event)
        self.app.scheduler.next_event.assert_called_once_with(is_fired=self.app._is_fired)

    def test_next_pending_event_skips_an_event_already_fired(self):
        wednesday = datetime(2026, 8, 26, 9, 0, tzinfo=self.app.tzinfo)
        self.app.scheduler = Scheduler(DEFAULT_CONFIG["schedule"], self.app.tzinfo, 3.0,
                                       clock=lambda: wednesday)
        first = self.app._next_pending_event()
        self.assertEqual(first.key, "hourly:10")
        self.app.state.mark_fired(first.key, first.day)
        self.assertEqual(self.app._next_pending_event().key, "hourly:11")

    def test_next_pending_event_is_none_when_nothing_is_scheduled(self):
        self.app.scheduler.next_event = lambda is_fired=None: None
        self.assertIsNone(self.app._next_pending_event())


class BuildPlanTest(AppTestCase):
    """``_build_plan``: 組み立てに失敗したら最小のプランに落とす。"""

    def test_returns_the_built_plan_without_the_fallback(self):
        event = self.past_event()
        plan = self.app._build_plan(event)
        self.assertIs(plan.event, event)
        self.assertEqual([segment.path for segment in plan.segments], [self.wav])
        self.assertEqual(self.app.builder.built, [event])
        self.assertEqual(self.app.builder.built_minimal, [])

    def test_returns_the_minimal_plan_when_building_fails(self):
        minimal = Segment(self.wav, label="時報音だけ")
        self.app.builder = StubBuilder([], explode=True, minimal_segments=[minimal])
        event = self.past_event()
        plan = self.app._build_plan(event)
        self.assertEqual(plan.segments, [minimal])
        self.assertIsNone(plan.quote)
        self.assertEqual((self.app.builder.built, self.app.builder.built_minimal),
                         ([event], [event]))

    def test_logs_the_failure_once_as_an_error_with_a_traceback(self):
        self.app.builder = StubBuilder([], explode=True)
        with logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
            self.app._build_plan(self.past_event())
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(
            captured.records[0].getMessage(),
            "再生内容の組み立てに失敗しました（最小の内容で鳴らします）: 組み立て失敗")
        self.assertIsNotNone(captured.records[0].exc_info)

    def test_does_not_log_when_building_succeeds(self):
        with mock.patch("chime.app.logger") as log:
            self.app._build_plan(self.past_event())
        self.assertEqual(log.mock_calls, [])

    def test_a_failing_minimal_plan_propagates(self):
        self.app.builder = StubBuilder([], explode=True, explode_minimal=True)
        with self.assertRaises(RuntimeError) as caught:
            self.app._build_plan(self.past_event())
        self.assertEqual(str(caught.exception), "最小プランも組み立て失敗")

    def test_only_exception_subclasses_are_caught(self):
        class Interrupting(StubBuilder):
            def build(self, event):
                raise KeyboardInterrupt

        self.app.builder = Interrupting([])
        with self.assertRaises(KeyboardInterrupt):
            self.app._build_plan(self.past_event())
        self.assertEqual(self.app.builder.built_minimal, [])


class RunForeverTest(AppTestCase):
    def _drive(self, event):
        """イベントを 1 件だけ返し、再生済みになったら停止するスケジューラ。"""
        def fake_next(is_fired=None):
            if is_fired is not None and is_fired(event):
                self.app.stop_event.set()
                return None
            return event

        self.app.scheduler.next_event = fake_next
        return self.app.run_forever()

    def test_plays_the_pending_event_then_stops(self):
        event = self.past_event()
        self.assertEqual(self._drive(event), 0)
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_build_failure_plays_the_minimal_plan_and_marks_fired(self):
        """組み立て全体が失敗しても、最小のプランで鳴らして常駐を続ける。"""
        minimal = Segment(self.wav, label="時報音だけ")
        self.app.builder = StubBuilder([], explode=True, minimal_segments=[minimal])
        event = self.past_event()
        self.assertEqual(self._drive(event), 0)
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day),
                        "失敗した回は再生済みとして記録し、無限リトライにしない")

    def test_build_and_minimal_failure_does_not_stop_the_loop(self):
        """最小プランの組み立てまで失敗しても、その回を飛ばして常駐を続ける。"""
        self.app.builder = StubBuilder([], explode=True, explode_minimal=True)
        event = self.past_event()
        self.assertEqual(self._drive(event), 0)
        self.assertEqual(self.player.played, [])
        self.assertTrue(self.app.state.is_fired(event.key, event.day),
                        "失敗した回は再生済みとして記録し、無限リトライにしない")

    def test_next_event_gets_only_the_is_fired_keyword_and_reads_the_live_state(self):
        """予定の選び直しは ``is_fired`` をキーワードだけで渡し、記録は呼ぶ時点の
        ``self.state`` を見る（構築後に差し替えても追従する）。"""
        event = self.past_event()
        calls = []

        def fake_next(*args, **kwargs):
            calls.append((args, sorted(kwargs)))
            if kwargs["is_fired"](event):
                self.app.stop_event.set()
                return None
            return event

        self.app.state = State(os.path.join(self.root, "swapped_state.json"))
        self.app.scheduler.next_event = fake_next
        self.assertEqual(self.app.run_forever(), 0)
        # 1 周目: 選ぶ・再確認、2 周目: 再生済みなので選んで None（停止）。
        self.assertEqual(calls, [((), ["is_fired"])] * 3)
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_a_changed_schedule_during_the_wait_is_recalculated(self):
        """待機中に予定が変わったら、再生せず選び直す。"""
        first, second = self.past_event(10.0), self.past_event(5.0)
        # 1 周目: 選ぶ(first)・再確認(second ≠ first で選び直し)。
        # 2 周目: 選ぶ(second)・再確認(second で再生)。3 周目: None で停止。
        answers = iter([first, second, second, second, None])

        def fake_next(is_fired=None):
            answer = next(answers)
            if answer is None:
                self.app.stop_event.set()
            return answer

        self.app.scheduler.next_event = fake_next
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(self.app.builder.built, [second])
        self.assertTrue(self.app.state.is_fired(second.key, second.day))

    def test_stop_before_start(self):
        self.app.stop_event.set()
        self.app.scheduler.next_event = lambda is_fired=None: self.past_event()
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(self.player.played, [])

    def test_a_stop_request_during_the_prepare_wait_builds_and_plays_nothing(self):
        """準備の時刻までの待機中に停止を求められたら、組み立て（TTS・天気）も再生もしない。"""
        event = self.past_event()
        guard = CallGuard()

        def fake_next(is_fired=None):
            guard.tick("next_event")
            return event

        def stopping_sleep_until(target, stop=None, precise=False):
            self.app.stop_event.set()
            return False

        self.app.scheduler.next_event = fake_next
        self.app.scheduler.sleep_until = stopping_sleep_until
        with mock.patch.object(self.app, "run_event") as run_event:
            self.assertEqual(self.app.run_forever(), 0)
        run_event.assert_not_called()
        self.assertEqual(self.app.builder.built, [])
        self.assertEqual(self.player.played, [])

    def test_a_different_key_at_the_same_instant_is_recalculated(self):
        """時刻が同じでも、イベントの種類（key）が変わっていたら、選び直す。"""
        first = self.past_event(5.0)
        other = dataclasses.replace(first, key="closing", kind="closing")
        answers = [first, other, other]
        guard = CallGuard()

        def fake_next(is_fired=None):
            guard.tick("next_event")
            if answers:
                return answers.pop(0)
            if is_fired(other):
                self.app.stop_event.set()
                return None
            return other

        self.app.scheduler.next_event = fake_next
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.run_forever()
        self.assertIn("待機中に予定が変わりました。再計算します。",
                      [record.getMessage() for record in captured.records])
        self.assertEqual(self.app.builder.built, [other])


class TimezoneTest(unittest.TestCase):
    def setUp(self):
        block_network(self)

    def test_now_uses_configured_timezone(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ChimeApp(Config(DEFAULT_CONFIG, base_dir=tmp), backend="mock")
            self.assertEqual(app.now().utcoffset(), timedelta(hours=9))

    def test_weather_today_follows_configured_timezone_not_os_local(self):
        """天気の「今日」も、スケジューリングと同じ設定タイムゾーン基準にすること。

        OS のローカル日付（``date.today()``）とは絶対に一致しないよう
        ``app.now`` を差し替え、その日付が ``builder.today_provider()`` に
        そのまま反映されることを確認する。
        """
        with tempfile.TemporaryDirectory() as tmp:
            app = ChimeApp(Config(DEFAULT_CONFIG, base_dir=tmp), backend="mock")
            fake_today = date.today() + timedelta(days=1)
            app.now = lambda: datetime.combine(fake_today, datetime.min.time())
            self.assertEqual(app.builder.today_provider(), fake_today)
            self.assertNotEqual(app.builder.today_provider(), date.today())

    def test_unknown_timezone_falls_back_to_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(dict(DEFAULT_CONFIG, timezone="Mars/Olympus"), base_dir=tmp)
            app = ChimeApp(config, backend="mock")
            self.assertIsNone(app.tzinfo)
            self.assertIsInstance(app.now(), datetime)


    def test_unknown_timezone_logs_an_error_naming_the_zone(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(dict(DEFAULT_CONFIG, timezone="Mars/Olympus"), base_dir=tmp)
            # 起動時の「作り置きの声」の集計が読むので、同梱のひとことを指す（無いと、ひとことの
            # ファイルが見つからない警告が、このテストと関係なく標準エラーへ漏れる）
            config.data["quotes"]["file"] = SHIPPED_QUOTES
            # アプリ全体（"chime"）で拾い、タイムゾーンの 1 件のほかに何も出ないことも確かめる
            with logs_enabled(), self.assertLogs("chime", level="WARNING") as captured:
                ChimeApp(config, backend="mock")
        self.assertEqual([record.name for record in captured.records], ["chime.app"])
        self.assertEqual(captured.records[0].levelname, "ERROR")
        self.assertTrue(captured.records[0].getMessage().startswith(
            "タイムゾーン 'Mars/Olympus' を解決できません（OS のローカル時刻を使用します）: "))

    def test_a_known_timezone_is_resolved_without_a_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(dict(DEFAULT_CONFIG, timezone="Asia/Tokyo"), base_dir=tmp)
            app = ChimeApp(config, backend="mock")
        self.assertEqual(str(app.tzinfo), "Asia/Tokyo")


class PlayerSelectionTest(AppTestCase):
    def test_player_is_created_lazily(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ChimeApp(Config(DEFAULT_CONFIG, base_dir=tmp), backend="mock")
            self.assertIsNone(app._player)
            self.assertEqual(app.player.name, "mock")
            self.assertIs(app.player, app._player)


class ExplodingPlayer(Player):
    """再生中に想定外の例外を送出するテスト用バックエンド。"""

    name = "exploding"

    def play_one(self, segment):
        raise RuntimeError("想定外の再生エラー")


class PlayReturnValueTest(AppTestCase):
    """``ChimeApp.play()`` の戻り値（再生の成否）を確認する。

    ``--say``・``--test-hourly``・``--test``・``--test-all`` は、この戻り値を
    見て終了コードを決める（``chime/cli.py``）。再生に失敗しても例外は外に
    出さず、常駐は継続できることも合わせて確認する。
    """

    def test_returns_true_on_success(self):
        plan = PlaybackPlan(event=None, segments=[Segment(self.wav, label="テスト音")])
        self.assertTrue(self.app.play(plan))
        self.assertEqual(self.player.played, [self.wav])

    def test_returns_false_when_no_segments(self):
        """再生対象のセグメントが 1 つも無ければ失敗として扱う。"""
        plan = PlaybackPlan(event=None, segments=[])
        self.assertTrue(self.app.play(plan) is False)
        self.assertEqual(self.player.played, [])

    def test_returns_false_on_playback_error(self):
        """必須セグメントの音源ファイルが欠落している場合は失敗として扱う
        （再現手順: 音源ファイルが存在しないパスを指定して ``--test`` する）。"""
        plan = PlaybackPlan(event=None,
                            segments=[Segment("/nonexistent.wav", label="欠落")])
        self.assertFalse(self.app.play(plan))
        self.assertEqual(self.player.played, [])

    def test_returns_false_on_unexpected_exception(self):
        self.app._player = ExplodingPlayer({})
        plan = PlaybackPlan(event=None, segments=[Segment(self.wav, label="テスト音")])
        self.assertFalse(self.app.play(plan))

    def test_unexpected_exception_is_logged_with_a_traceback(self):
        self.app._player = ExplodingPlayer({})
        plan = PlaybackPlan(event=None, segments=[Segment(self.wav, label="テスト音")])
        with logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
            self.assertFalse(self.app.play(plan))
        record = captured.records[0]
        self.assertEqual(record.getMessage(),
                         "再生中に予期しないエラーが発生しました: 想定外の再生エラー")
        self.assertIsNotNone(record.exc_info)

    def test_dry_run_returns_true_even_with_no_segments(self):
        """``--dry-run`` は再生をスキップするだけで失敗ではないため、
        セグメントが 0 件でも True を返す。"""
        self.app.dry_run = True
        plan = PlaybackPlan(event=None, segments=[])
        self.assertTrue(self.app.play(plan))
        self.assertEqual(self.player.played, [])

    def test_dry_run_returns_true_without_playing(self):
        self.app.dry_run = True
        plan = PlaybackPlan(event=None, segments=[Segment(self.wav, label="テスト音")])
        self.assertTrue(self.app.play(plan))
        self.assertEqual(self.player.played, [])

    def test_returns_false_when_all_optional_segments_are_missing(self):
        """穴の再現テスト: セグメントが 1 件以上あっても、すべて optional で
        音源ファイルが欠落していると Player.play() は例外を出さず 0 件再生
        で終わる。この場合も ChimeApp.play() は False を返す必要がある
        （一音も鳴っていないのに成功を返さない）。"""
        plan = PlaybackPlan(event=None,
                            segments=[Segment("/nonexistent.wav", label="任意",
                                              optional=True)])
        self.assertFalse(self.app.play(plan))
        self.assertEqual(self.player.played, [])

    def test_run_event_does_not_use_return_value(self):
        """``run_event()`` は再生済みの記録（``mark_fired``）に ``play()`` の
        戻り値を使わず、再生に失敗しても従来どおり再生済みとして記録する
        （戻り値を見るのは、ひとことの記録だけ）。"""
        self.app.builder = StubBuilder([Segment("/nonexistent.wav", label="欠落")])
        event = self.past_event()
        self.app.run_event(event)
        self.assertTrue(self.app.state.is_fired(event.key, event.day))


class PlayOutcomeTest(unittest.TestCase):
    def test_error_defaults_to_none(self):
        outcome = PlayOutcome(ok=True, played=2, total=3)
        self.assertEqual((outcome.ok, outcome.played, outcome.total, outcome.error),
                         (True, 2, 3, None))

    def test_it_is_frozen(self):
        outcome = PlayOutcome(ok=True, played=1, total=1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            outcome.ok = False

    def test_it_compares_by_value(self):
        self.assertEqual(PlayOutcome(False, 0, 1, "x"), PlayOutcome(ok=False, played=0, total=1,
                                                                    error="x"))


class PlayWithResultTest(AppTestCase):
    """``ChimeApp.play_with_result()``: ``play()`` と同じ再生の、件数・理由つきの結果。"""

    def two_segment_plan(self, **second):
        return PlaybackPlan(event=None, segments=[Segment(self.wav, label="一つ目"),
                                                  Segment(self.wav, label="二つ目", **second)])

    def test_success_counts_the_played_segments(self):
        outcome = self.app.play_with_result(self.two_segment_plan())
        self.assertEqual(outcome, PlayOutcome(ok=True, played=2, total=2))
        self.assertEqual(self.player.played, [self.wav, self.wav])

    def test_a_missing_optional_segment_is_total_but_not_played(self):
        plan = PlaybackPlan(event=None, segments=[
            Segment(self.wav, label="鳴る"),
            Segment("/nonexistent.wav", label="欠落", optional=True)])
        outcome = self.app.play_with_result(plan)
        self.assertEqual(outcome, PlayOutcome(ok=True, played=1, total=2))

    def test_nothing_playable_is_a_failure_without_an_error(self):
        plan = PlaybackPlan(event=None, segments=[
            Segment("/nonexistent.wav", label="任意", optional=True)])
        self.assertEqual(self.app.play_with_result(plan),
                         PlayOutcome(ok=False, played=0, total=1))

    def test_no_segments_is_a_failure_with_nothing_to_play(self):
        outcome = self.app.play_with_result(PlaybackPlan(event=None))
        self.assertEqual(outcome, PlayOutcome(ok=False, played=0, total=0))
        self.assertEqual(self.player.played, [])

    def test_a_missing_required_file_reports_the_playback_error(self):
        plan = PlaybackPlan(event=None, segments=[Segment("/nonexistent.wav", label="欠落")])
        outcome = self.app.play_with_result(plan)
        self.assertEqual(outcome, PlayOutcome(
            ok=False, played=0, total=1, error="音源ファイルが見つかりません: /nonexistent.wav"))

    def test_an_unexpected_exception_reports_its_type_and_message(self):
        self.app._player = ExplodingPlayer({})
        outcome = self.app.play_with_result(self.two_segment_plan())
        self.assertEqual(outcome, PlayOutcome(
            ok=False, played=0, total=2, error="RuntimeError: 想定外の再生エラー"))

    def test_dry_run_is_ok_without_playing_and_keeps_the_total(self):
        self.app.dry_run = True
        outcome = self.app.play_with_result(self.two_segment_plan())
        self.assertEqual(outcome, PlayOutcome(ok=True, played=0, total=2))
        self.assertEqual(self.player.played, [])

    def test_dry_run_with_no_segments_is_ok(self):
        self.app.dry_run = True
        self.assertEqual(self.app.play_with_result(PlaybackPlan(event=None)),
                         PlayOutcome(ok=True, played=0, total=0))

    def test_it_logs_the_plan_first_like_play(self):
        plan = self.two_segment_plan()
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.play_with_result(plan)
        messages = [record.getMessage() for record in captured.records]
        self.assertEqual(messages, [plan.describe(), "再生シーケンスが完了しました。"])

    def test_dry_run_logs_are_unchanged(self):
        self.app.dry_run = True
        plan = self.two_segment_plan()
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.play_with_result(plan)
        self.assertEqual([record.getMessage() for record in captured.records],
                         [plan.describe(), "dry-run のため再生しません。"])

    def test_failure_logs_are_unchanged(self):
        plan = PlaybackPlan(event=None, segments=[Segment("/nonexistent.wav", label="欠落")])
        with logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
            self.app.play_with_result(plan)
        self.assertEqual([record.getMessage() for record in captured.records],
                         ["再生に失敗しました: 音源ファイルが見つかりません: /nonexistent.wav"])

    def test_nothing_played_warns_like_play(self):
        plan = PlaybackPlan(event=None, segments=[
            Segment("/nonexistent.wav", label="任意", optional=True)])
        # chime.audio の警告（欠けた音源のスキップ）は、ここでは見ないので捨てる。
        with mock.patch("chime.audio.logger"), logs_enabled(), \
                self.assertLogs("chime.app", level="WARNING") as captured:
            self.app.play_with_result(plan)
        self.assertEqual([record.getMessage() for record in captured.records],
                         ["再生できたセグメントがありませんでした。"])

    def test_it_never_raises(self):
        for player in (ExplodingPlayer({}), RecordingPlayer()):
            self.app._player = player
            outcome = self.app.play_with_result(self.two_segment_plan())
            self.assertIsInstance(outcome, PlayOutcome)

    def test_play_returns_the_ok_of_play_with_result(self):
        plan = self.two_segment_plan()
        for ok in (True, False):
            with mock.patch.object(self.app, "play_with_result",
                                   return_value=PlayOutcome(ok=ok, played=0, total=1)) as inner:
                self.assertIs(self.app.play(plan), ok)
            inner.assert_called_once_with(plan)

    def test_play_returns_a_real_bool(self):
        self.assertIs(self.app.play(self.two_segment_plan()), True)
        self.assertIs(self.app.play(PlaybackPlan(event=None)), False)


class ExceptionTextTest(unittest.TestCase):
    """例外の説明を作るために、例外を増やさない（``str()`` が失敗する例外でも）。"""

    def test_describe_exception_is_the_type_and_the_message(self):
        self.assertEqual(describe_exception(RuntimeError("だめ")), "RuntimeError: だめ")
        self.assertEqual(describe_exception(KeyError("k")), "KeyError: 'k'")

    def test_describe_exception_keeps_the_format_for_an_empty_message(self):
        self.assertEqual(describe_exception(RuntimeError()), "RuntimeError: ")

    def test_describe_exception_falls_back_to_the_type_name(self):
        self.assertEqual(describe_exception(BadStr()), "BadStr")

    def test_describe_exception_falls_back_when_str_returns_a_non_string(self):
        class NotAString(Exception):
            def __str__(self):
                return 42

        self.assertEqual(describe_exception(NotAString()), "NotAString")

    def test_exception_text_is_the_message_alone(self):
        self.assertEqual(exception_text(RuntimeError("だめ")), "だめ")
        self.assertEqual(exception_text(PlaybackError("音源がない")), "音源がない")

    def test_exception_text_falls_back_to_the_type_name(self):
        self.assertEqual(exception_text(BadStr()), "BadStr")
        self.assertEqual(exception_text(BadStrPlaybackError()), "BadStrPlaybackError")

    def test_neither_raises_for_base_exception_subclasses_that_can_be_described(self):
        self.assertEqual(describe_exception(KeyboardInterrupt()), "KeyboardInterrupt: ")
        self.assertEqual(exception_text(SystemExit(3)), "3")


class BadStrPlaybackTest(AppTestCase):
    """``str()`` が失敗する例外が再生で出ても、``play()`` は例外を出さず、理由を型名で残す。"""

    class BadPlayer(Player):
        name = "bad"

        def __init__(self, error):
            super().__init__({})
            self.error = error

        def play_one(self, segment):
            raise self.error

    def outcome_for(self, error):
        self.app._player = self.BadPlayer(error)
        plan = PlaybackPlan(event=None, segments=[Segment(self.wav, label="一つ目")])
        return self.app.play_with_result(plan)

    def test_an_unexpected_exception_whose_str_fails_is_reported_by_its_type(self):
        self.assertEqual(self.outcome_for(BadStr()),
                         PlayOutcome(ok=False, played=0, total=1, error="BadStr"))

    def test_a_playback_error_whose_str_fails_is_reported_by_its_type(self):
        self.assertEqual(
            self.outcome_for(BadStrPlaybackError()),
            PlayOutcome(ok=False, played=0, total=1, error="BadStrPlaybackError"))

    def test_play_does_not_raise_and_returns_false(self):
        for error in (BadStr(), BadStrPlaybackError()):
            with self.subTest(error=type(error).__name__):
                self.app._player = self.BadPlayer(error)
                plan = PlaybackPlan(event=None, segments=[Segment(self.wav, label="一つ目")])
                self.assertIs(self.app.play(plan), False)

    def test_the_failure_is_still_logged_with_the_type_name(self):
        self.app._player = self.BadPlayer(BadStr())
        plan = PlaybackPlan(event=None, segments=[Segment(self.wav, label="一つ目")])
        with logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
            self.app.play_with_result(plan)
        self.assertEqual([record.getMessage() for record in captured.records],
                         ["再生中に予期しないエラーが発生しました: BadStr"])
        self.assertIsNotNone(captured.records[0].exc_info)


class HistoryTestCase(AppTestCase):
    """放送の履歴（``cache/history.jsonl``）を読み戻して確かめるための土台。"""

    @property
    def history_path(self):
        return self.app.config.path("state.history_file")

    def entries(self):
        """新しい順の履歴。"""
        return History(self.history_path).recent(limit=50)

    def only_entry(self):
        entries = self.entries()
        self.assertEqual(len(entries), 1, entries)
        return entries[0]


class RunEventHistoryTest(HistoryTestCase):
    """``run_event`` は、再生したあとに放送の結果を履歴へ 1 行足す。"""

    def test_the_default_history_file_is_under_cache(self):
        self.assertEqual(self.history_path, os.path.join(self.root, "cache", "history.jsonl"))

    def test_a_played_broadcast_appends_one_entry(self):
        event = self.past_event()
        self.app.run_event(event)
        entry = self.only_entry()
        self.assertEqual(entry["kind"], "hourly")
        self.assertEqual(entry["key"], event.key)
        self.assertEqual(entry["at"], event.at.isoformat(timespec="seconds"))
        self.assertEqual(entry["day"], event.day)
        self.assertEqual(entry["result"], "ok")
        self.assertEqual((entry["played"], entry["total"]), (1, 1))
        self.assertEqual((entry["silent"], entry["warnings"], entry["degraded"]), ([], [], False))
        self.assertNotIn("error", entry)

    def test_the_kind_and_key_come_from_the_event(self):
        event = make_event(self.app.now() - timedelta(seconds=5), key="closing", kind="closing",
                           hour=16)
        self.app.run_event(event)
        entry = self.only_entry()
        self.assertEqual((entry["kind"], entry["key"]), ("closing", "closing"))

    def test_the_at_is_the_scheduled_time_not_the_play_time(self):
        event = self.past_event(seconds_ago=30.0)
        later = event.__class__(**dict(event.__dict__, at=event.at + timedelta(seconds=3)))
        self.app.run_event(later)
        self.assertEqual(self.only_entry()["at"], later.at.isoformat(timespec="seconds"))

    def test_the_entry_is_written_after_playing(self):
        seen = []
        path = self.history_path

        class CheckingPlayer(RecordingPlayer):
            def play_one(self, segment):
                seen.append(os.path.exists(path))
                super().play_one(segment)

        self.app._player = CheckingPlayer()
        self.app.run_event(self.past_event())
        self.assertEqual(seen, [False])
        self.assertTrue(os.path.exists(path))

    def test_the_entry_is_written_after_the_event_is_marked_fired(self):
        # 履歴で何かあっても、二重再生を防ぐ記録は先に付いている。
        event = self.past_event()
        fired_when_written = []
        real_append = History.append

        def spying_append(history, entry):
            fired_when_written.append(self.app.state.is_fired(event.key, event.day))
            return real_append(history, entry)

        with mock.patch.object(History, "append", spying_append):
            self.app.run_event(event)
        self.assertEqual(fired_when_written, [True])

    def test_silent_texts_and_warnings_are_recorded_and_the_result_is_partial(self):
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")],
                                       silent=["無音の文。"], warnings=["注意の文"])
        self.app.run_event(self.past_event())
        entry = self.only_entry()
        self.assertEqual(entry["silent"], ["無音の文。"])
        self.assertEqual(entry["warnings"], ["注意の文"])
        self.assertEqual(entry["result"], "partial")

    def test_a_partly_played_plan_is_partial(self):
        self.app.builder = StubBuilder([Segment(self.wav, label="鳴る"),
                                        Segment("/nonexistent.wav", label="欠落", optional=True)])
        self.app.run_event(self.past_event())
        entry = self.only_entry()
        self.assertEqual((entry["played"], entry["total"], entry["result"]), (1, 2, "partial"))

    def test_a_part_that_could_not_be_added_is_recorded_and_the_result_is_partial(self):
        # 積めなかった必須の部品は total に数えられない。played == total でも「成功」にしない。
        self.app.builder = StubBuilder([Segment(self.wav, label="閉館アナウンス")],
                                       missing=["蛍の光（2000ms フェードイン）"],
                                       warnings=["音源ファイルが見つかりません: /x/hotaru.mp3"])
        self.app.run_event(self.past_event())
        entry = self.only_entry()
        self.assertEqual(entry["missing"], ["蛍の光（2000ms フェードイン）"])
        self.assertEqual((entry["played"], entry["total"], entry["result"]), (1, 1, "partial"))
        self.assertFalse(entry["silent"])

    def test_a_plan_with_nothing_missing_records_an_empty_list(self):
        self.app.run_event(self.past_event())
        entry = self.only_entry()
        self.assertEqual(entry["missing"], [])
        self.assertEqual(entry["result"], "ok")

    def test_nothing_played_and_parts_missing_is_failed(self):
        self.app.builder = StubBuilder([], missing=["閉館アナウンス", "蛍の光"])
        self.app.run_event(self.past_event())
        entry = self.only_entry()
        self.assertEqual((entry["result"], entry["missing"]), ("failed", ["閉館アナウンス", "蛍の光"]))

    def test_the_minimal_plan_is_recorded_as_degraded(self):
        self.app.builder = StubBuilder([], explode=True,
                                       minimal_segments=[Segment(self.wav, label="時報音だけ")])
        self.app.run_event(self.past_event())
        entry = self.only_entry()
        self.assertTrue(entry["degraded"])
        self.assertEqual((entry["played"], entry["total"], entry["result"]), (1, 1, "ok"))

    def test_a_normal_plan_is_not_degraded(self):
        self.app.run_event(self.past_event())
        self.assertFalse(self.only_entry()["degraded"])

    def test_a_missing_required_file_is_recorded_as_an_error(self):
        self.app.builder = StubBuilder([Segment("/nonexistent.wav", label="欠落")])
        self.app.run_event(self.past_event())
        entry = self.only_entry()
        self.assertEqual(entry["result"], "error")
        self.assertEqual(entry["error"], "音源ファイルが見つかりません: /nonexistent.wav")
        self.assertEqual((entry["played"], entry["total"]), (0, 1))

    def test_an_unexpected_playback_exception_is_recorded_as_an_error(self):
        self.app._player = ExplodingPlayer({})
        self.app.run_event(self.past_event())
        entry = self.only_entry()
        self.assertEqual((entry["result"], entry["error"]),
                         ("error", "RuntimeError: 想定外の再生エラー"))

    def test_nothing_played_without_an_exception_is_failed_not_error(self):
        self.app.builder = StubBuilder([Segment("/nonexistent.wav", label="任意", optional=True)])
        self.app.run_event(self.past_event())
        entry = self.only_entry()
        self.assertEqual(entry["result"], "failed")
        self.assertNotIn("error", entry)

    def test_an_empty_plan_is_recorded_as_failed(self):
        self.app.builder = StubBuilder([])
        self.app.run_event(self.past_event())
        entry = self.only_entry()
        self.assertEqual((entry["played"], entry["total"], entry["result"]), (0, 0, "failed"))

    def test_each_broadcast_adds_a_line(self):
        first = self.past_event(10.0)
        second = make_event(first.at + timedelta(seconds=5), key="hourly:11", hour=11)
        self.app.run_event(first)
        self.app.run_event(second)
        self.assertEqual([entry["key"] for entry in self.entries()], ["hourly:11", "hourly:10"])

    def test_the_configured_history_file_is_used(self):
        elsewhere = os.path.join(self.root, "elsewhere", "log.jsonl")
        self.app.config.data["state"]["history_file"] = elsewhere
        self.app.run_event(self.past_event())
        self.assertEqual(len(History(elsewhere).recent()), 1)
        self.assertFalse(os.path.exists(os.path.join(self.root, "cache", "history.jsonl")))

    def test_an_empty_history_file_setting_records_nothing(self):
        self.app.config.data["state"]["history_file"] = ""
        with mock.patch("chime.app.History") as history_class:
            self.app.run_event(self.past_event())
        history_class.assert_not_called()
        self.assertEqual(self.player.played, [self.wav])

    def test_the_quote_is_still_remembered(self):
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")],
                                       quote="テストのひとこと")
        self.app.run_event(self.past_event())
        self.assertEqual(self.app.state.recent_quotes(), ["テストのひとこと"])
        self.assertEqual(len(self.entries()), 1)

    def test_a_stop_request_before_playback_records_nothing(self):
        event = make_event(self.app.now() + timedelta(hours=1))
        self.app.stop_event.set()
        self.app.run_event(event)
        self.assertFalse(os.path.exists(self.history_path))

    def test_dry_run_writes_no_history_and_creates_no_directory(self):
        # dry-run で起動したアプリは state も書かないので、cache/ 自体が作られない。
        app = ChimeApp(self.app.config, backend="mock", dry_run=True)
        app._player = self.player
        app.builder = StubBuilder([Segment(self.wav, label="テスト音")])
        app.run_event(self.past_event())
        self.assertFalse(os.path.exists(self.history_path))
        self.assertFalse(os.path.exists(os.path.dirname(self.history_path)))

    def test_dry_run_does_not_even_open_the_history(self):
        self.app.dry_run = True
        with mock.patch("chime.app.History") as history_class:
            self.app.run_event(self.past_event())
        history_class.assert_not_called()

    def test_play_alone_writes_no_history(self):
        # --test / --test-hourly / --test-all / --say は play() を直接呼ぶ。履歴は付けない。
        plan = PlaybackPlan(event=None, segments=[Segment(self.wav, label="テスト音")])
        self.assertTrue(self.app.play(plan))
        self.assertTrue(self.app.play_with_result(plan).ok)
        self.assertFalse(os.path.exists(self.history_path))


class MissingFileHistoryTest(HistoryTestCase):
    """本物の ``SequenceBuilder`` で、音源ファイルが無いまま鳴らした放送を記録する。"""

    def closing_app(self, announce="assets/beep.wav", music="assets/hotaru.mp3"):
        """閉館放送だけを鳴らす ChimeApp（組み立ては本物、再生は記録するだけ）。"""
        config = Config(DEFAULT_CONFIG, base_dir=self.root)
        config.data["audio"]["gap_ms"] = 0
        config.data["quotes"]["file"] = SHIPPED_QUOTES
        config.data["closing"]["announce_file"] = announce
        config.data["closing"]["music_file"] = music
        app = ChimeApp(config, backend="mock")
        app._player = RecordingPlayer()
        return app

    def closing_event(self, app):
        return make_event(app.now() - timedelta(seconds=5), key="closing", kind="closing", hour=16)

    def entry_of(self, app):
        entries = History(app.config.path("state.history_file")).recent()
        self.assertEqual(len(entries), 1, entries)
        return entries[0]

    def test_a_closing_broadcast_without_the_music_is_partial_not_ok(self):
        app = self.closing_app(music="assets/does-not-exist.mp3")
        app.run_event(self.closing_event(app))
        self.assertEqual(app.player.played, [self.wav])  # 閉館アナウンスだけ鳴る
        entry = self.entry_of(app)
        self.assertEqual(entry["result"], "partial")
        self.assertEqual((entry["played"], entry["total"]), (1, 1))
        self.assertEqual(entry["missing"], ["蛍の光（2000ms フェードイン）"])
        self.assertEqual(entry["silent"], [])
        self.assertIn(os.path.join(self.root, "assets", "does-not-exist.mp3"), entry["warnings"][0])

    def test_a_closing_broadcast_without_the_announcement_is_partial_not_ok(self):
        music = make_wav(os.path.join(self.root, "assets", "hotaru.mp3"), seconds=0.01)
        app = self.closing_app(announce="assets/announce-gone.wav")
        app.run_event(self.closing_event(app))
        self.assertEqual(app.player.played, [music])
        entry = self.entry_of(app)
        self.assertEqual((entry["result"], entry["missing"]),
                         ("partial", ["閉館アナウンス"]))

    def test_a_closing_broadcast_without_either_is_failed(self):
        app = self.closing_app(announce="assets/announce-gone.wav",
                               music="assets/does-not-exist.mp3")
        event = self.closing_event(app)
        app.run_event(event)
        self.assertEqual(app.player.played, [])
        entry = self.entry_of(app)
        self.assertEqual(entry["result"], "failed")
        self.assertEqual(entry["missing"], ["閉館アナウンス", "蛍の光（2000ms フェードイン）"])
        self.assertTrue(app.state.is_fired(event.key, event.day))

    def test_a_complete_closing_broadcast_is_ok(self):
        make_wav(os.path.join(self.root, "assets", "hotaru.mp3"), seconds=0.01)
        app = self.closing_app()
        app.run_event(self.closing_event(app))
        entry = self.entry_of(app)
        self.assertEqual((entry["result"], entry["missing"]), ("ok", []))

    def test_the_minimal_plan_records_what_is_missing_too(self):
        app = self.closing_app(music="assets/does-not-exist.mp3")
        with mock.patch.object(app.builder, "build", side_effect=RuntimeError("組み立て失敗")):
            app.run_event(self.closing_event(app))
        entry = self.entry_of(app)
        self.assertTrue(entry["degraded"])
        self.assertEqual((entry["result"], entry["missing"]),
                         ("partial", ["蛍の光（2000ms フェードイン）"]))


class HistoryFailureTest(HistoryTestCase):
    """履歴が書けなくても、放送も常駐ループも止めない。"""

    def block_history_directory(self):
        """履歴の親が「ファイル」なので、ディレクトリを作れず書けない状態にする。"""
        blocker = os.path.join(self.root, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("ファイル")
        self.app.config.data["state"]["history_file"] = os.path.join(
            blocker, "history.jsonl")

    def test_an_unwritable_history_does_not_affect_the_broadcast(self):
        self.block_history_directory()
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")],
                                       quote="テストのひとこと")
        event = self.past_event()
        self.app.run_event(event)  # 例外は出ない
        self.assertEqual(self.player.played, [self.wav])
        self.assertEqual(self.app.state.recent_quotes(), ["テストのひとこと"])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_an_unwritable_history_leaves_a_warning(self):
        self.block_history_directory()
        with logs_enabled(), self.assertLogs("chime.history", level="WARNING") as captured:
            self.app.run_event(self.past_event())
        self.assertIn("放送の履歴を書けません", captured.records[0].getMessage())

    def test_a_history_that_raises_is_logged_and_the_broadcast_goes_on(self):
        event = self.past_event()
        with mock.patch.object(History, "append", side_effect=RuntimeError("履歴が壊れた")):
            with logs_enabled(), self.assertLogs("chime.app", level="WARNING") as captured:
                self.app.run_event(event)
        self.assertEqual(
            [record.getMessage() for record in captured.records],
            ["放送の履歴を残せませんでした（放送は続けます）: RuntimeError: 履歴が壊れた"])
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_a_failing_entry_builder_does_not_stop_the_broadcast(self):
        with mock.patch("chime.app.make_entry", side_effect=ValueError("作れない")):
            self.app.run_event(self.past_event())
        self.assertEqual(self.player.played, [self.wav])

    def test_an_unwritable_history_does_not_stop_the_loop(self):
        self.block_history_directory()
        first = self.past_event(10.0)
        second = make_event(first.at + timedelta(seconds=5), key="hourly:11", hour=11)
        events = [first, second]

        def fake_next(is_fired=None):
            for event in events:
                if not is_fired(event):
                    return event
            self.app.stop_event.set()
            return None

        self.app.scheduler.next_event = fake_next
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(self.player.played, [self.wav, self.wav])
        self.assertTrue(all(self.app.state.is_fired(e.key, e.day) for e in events))


class RunForeverHistoryTest(HistoryTestCase):
    """常駐ループの中の履歴。例外で終わった回も「error」として残す。"""

    def drive(self, event):
        def fake_next(is_fired=None):
            if is_fired is not None and is_fired(event):
                self.app.stop_event.set()
                return None
            return event

        self.app.scheduler.next_event = fake_next
        return self.app.run_forever()

    def test_a_scheduled_broadcast_is_recorded(self):
        event = self.past_event()
        self.assertEqual(self.drive(event), 0)
        entry = self.only_entry()
        self.assertEqual((entry["key"], entry["result"]), (event.key, "ok"))

    def test_an_exception_from_run_event_is_recorded_as_an_error(self):
        self.app.builder = StubBuilder([], explode=True, explode_minimal=True)
        event = self.past_event()
        self.assertEqual(self.drive(event), 0)
        entry = self.only_entry()
        self.assertEqual(entry["result"], "error")
        self.assertEqual(entry["error"], "RuntimeError: 最小プランも組み立て失敗")
        self.assertEqual((entry["kind"], entry["key"]), (event.kind, event.key))
        self.assertEqual(entry["at"], event.at.isoformat(timespec="seconds"))
        self.assertEqual((entry["played"], entry["total"]), (0, 0))
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_the_error_entry_is_written_once(self):
        self.app.builder = StubBuilder([], explode=True, explode_minimal=True)
        self.drive(self.past_event())
        self.assertEqual(len(self.entries()), 1)

    def test_dry_run_records_no_error_either(self):
        self.app.dry_run = True
        self.app.builder = StubBuilder([], explode=True, explode_minimal=True)
        self.assertEqual(self.drive(self.past_event()), 0)
        self.assertFalse(os.path.exists(self.history_path))

    def test_an_unwritable_history_does_not_stop_recording_the_failure_as_fired(self):
        blocker = os.path.join(self.root, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("ファイル")
        self.app.config.data["state"]["history_file"] = os.path.join(blocker, "h.jsonl")
        self.app.builder = StubBuilder([], explode=True, explode_minimal=True)
        event = self.past_event()
        self.assertEqual(self.drive(event), 0)
        self.assertTrue(self.app.state.is_fired(event.key, event.day))


class RunForeverFailureTest(HistoryTestCase):
    """放送が例外で終わったときの後始末（再生済みの記録 → 履歴）と、説明を作れない例外。"""

    def drive(self, event):
        """``event`` を 1 回だけ鳴らそうとして止まる（選ぶ・再確認のあとは停止を要求する）。"""
        guard = CallGuard()

        def fake_next(is_fired=None):
            if guard.tick("next_event") <= 2:
                return event
            self.app.stop_event.set()
            return None

        self.app.scheduler.next_event = fake_next
        return self.app.run_forever()

    def explode(self, error=None):
        self.app.builder = StubBuilder([], explode=True, explode_minimal=True, error=error)

    def test_an_exception_whose_str_fails_does_not_stop_the_loop(self):
        self.explode(BadStr())
        event = self.past_event()
        self.assertEqual(self.drive(event), 0)
        self.assertTrue(self.app.state.is_fired(event.key, event.day),
                        "記録しないと、再起動のたびに同じ放送をやり直す")

    def test_an_exception_whose_str_fails_is_recorded_by_its_type(self):
        self.explode(BadStr())
        self.drive(self.past_event())
        entry = self.only_entry()
        self.assertEqual((entry["result"], entry["error"]), ("error", "BadStr"))

    def test_an_exception_whose_str_fails_does_not_break_the_log_line_either(self):
        self.explode(BadStr())
        with logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
            self.drive(self.past_event())
        self.assertEqual([record.getMessage() for record in captured.records], [
            "再生内容の組み立てに失敗しました（最小の内容で鳴らします）: BadStr",
            "イベント処理に失敗しました（継続します）: BadStr"])

    def test_the_event_is_marked_fired_before_the_history_is_written(self):
        self.explode()
        order = []
        real_mark = self.app.state.mark_fired

        def mark_fired(key, day):
            order.append("mark_fired")
            real_mark(key, day)

        real_append = History.append

        def append(history, entry):
            order.append("history")
            return real_append(history, entry)

        with mock.patch.object(self.app.state, "mark_fired", mark_fired), \
                mock.patch.object(History, "append", append):
            self.drive(self.past_event())
        self.assertEqual(order, ["mark_fired", "history"])

    def test_marking_it_fired_can_fail_without_stopping_the_loop_or_the_history(self):
        self.explode()
        event = self.past_event()
        # State.save は OSError を自分で受ける。ここへ届くのは、それ以外の想定外の例外。
        with mock.patch.object(self.app.state, "mark_fired", side_effect=RuntimeError("書けない")):
            with logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
                self.assertEqual(self.drive(event), 0)
        self.assertIn(
            "失敗した放送を再生済みとして記録できませんでした（継続します）: RuntimeError: 書けない",
            [record.getMessage() for record in captured.records])
        self.assertEqual(self.only_entry()["result"], "error")

    def test_recording_the_failure_can_fail_without_stopping_the_loop(self):
        self.explode()
        event = self.past_event()
        with mock.patch.object(self.app, "_record_failure", side_effect=RuntimeError("履歴の失敗")):
            with logs_enabled(), self.assertLogs("chime.app", level="WARNING") as captured:
                self.assertEqual(self.drive(event), 0)
        self.assertTrue(self.app.state.is_fired(event.key, event.day))
        self.assertIn("放送の履歴を残せませんでした（放送は続けます）: RuntimeError: 履歴の失敗",
                      [record.getMessage() for record in captured.records])

    def test_both_can_fail_and_the_loop_still_ends_normally(self):
        self.explode()
        with mock.patch.object(self.app.state, "mark_fired", side_effect=RuntimeError("書けない")), \
                mock.patch.object(History, "append", side_effect=RuntimeError("書けない")):
            self.assertEqual(self.drive(self.past_event()), 0)

    def test_a_playback_whose_error_cannot_be_described_is_recorded_by_its_type(self):
        # 再生で出た例外も同じ。run_event は最後まで進み、履歴に型名が残る。
        self.app._player = BadStrPlaybackTest.BadPlayer(BadStr())
        event = self.past_event()
        self.assertEqual(self.drive(event), 0)
        entry = self.only_entry()
        self.assertEqual((entry["result"], entry["error"]), ("error", "BadStr"))
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_the_failure_is_recorded_once(self):
        self.explode(BadStr())
        self.drive(self.past_event())
        self.assertEqual(len(self.entries()), 1)


class RunForeverGuardTest(AppTestCase):
    """``CallGuard``: 偽物が呼ばれすぎたら、固まらずに失敗する。"""

    def test_the_guard_counts_by_name(self):
        guard = CallGuard()
        for number in range(1, CALL_LIMIT + 1):
            self.assertEqual(guard.tick("a"), number)
        self.assertEqual(guard.tick("b"), 1)
        with self.assertRaises(RunawayLoop):
            guard.tick("a")

    def test_a_runaway_loop_is_cut_off_instead_of_hanging(self):
        # 予定が無いまま、待機が止まらない。ループは自分では終わらない。
        guard = CallGuard()
        self.app.scheduler.next_event = lambda is_fired=None: None

        def endless_wait(timeout=None):
            guard.tick("wait")
            return False

        with mock.patch.object(self.app.stop_event, "wait", endless_wait):
            with self.assertRaises(RunawayLoop):
                self.app.run_forever()
        self.assertEqual(guard.counts["wait"], CALL_LIMIT + 1)

    def test_the_runaway_signal_is_not_swallowed_by_the_loop(self):
        self.assertFalse(issubclass(RunawayLoop, Exception))


class RunForeverResilienceTest(AppTestCase):
    """スケジューラーが例外を出しても、常駐ループは落ちずに 60 秒後にやり直す。

    偽物（待機・予定を返す関数）は ``self.guard`` で呼び出し回数を数え、
    ``CALL_LIMIT`` 回を超えたら ``RunawayLoop`` で打ち切る。直しが戻ったとき
    （退行したとき）に、ループが止まらずテストが固まるのを防ぐ。
    """

    def setUp(self):
        super().setUp()
        self.guard = CallGuard()

    def stub_waits(self, stop_after=None):
        """``stop_event.wait`` を待たずに記録する。``(秒数, その時点の再生数)`` を返すリスト。

        ``stop_after`` 回目の待機で停止を要求する（常に失敗するループを終わらせる）。
        """
        waits = []

        def fake_wait(timeout=None):
            self.guard.tick("wait")
            waits.append((timeout, len(self.player.played)))
            if stop_after is not None and len(waits) >= stop_after:
                self.app.stop_event.set()
            return self.app.stop_event.is_set()

        patcher = mock.patch.object(self.app.stop_event, "wait", fake_wait)
        patcher.start()
        self.addCleanup(patcher.stop)
        return waits

    def script_next_event(self, event, *answers):
        """``next_event`` の応答を順に返す（例外クラス／インスタンスなら送出する）。

        尽きたあとは ``event`` を鳴らし、鳴らし終えたら停止を要求する。
        """
        answers = list(answers)

        def fake_next(is_fired=None):
            self.guard.tick("next_event")
            if answers:
                answer = answers.pop(0)
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            if is_fired(event):
                self.app.stop_event.set()
                return None
            return event

        self.app.scheduler.next_event = fake_next

    def test_next_event_raising_once_is_retried_after_60_seconds_and_plays_later(self):
        waits = self.stub_waits()
        event = self.past_event()
        self.script_next_event(event, RuntimeError("予定を求められない"))
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(waits[0], (60, 0), "鳴らす前に 60 秒待ってやり直す")
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_next_event_raising_every_time_waits_again_and_again(self):
        waits = self.stub_waits(stop_after=3)
        self.script_next_event(self.past_event(), *[RuntimeError("だめ")] * 10)
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(waits, [(60, 0)] * 3)
        self.assertEqual(self.player.played, [])

    def test_a_stop_request_during_the_retry_wait_exits_the_loop(self):
        waits = self.stub_waits(stop_after=1)
        calls = []

        def fake_next(is_fired=None):
            self.guard.tick("next_event")
            calls.append(1)
            raise RuntimeError("だめ")

        self.app.scheduler.next_event = fake_next
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(waits, [(60, 0)])
        self.assertEqual(len(calls), 1)

    def test_the_failure_is_logged_with_a_traceback(self):
        self.stub_waits()
        self.script_next_event(self.past_event(), RuntimeError("予定を求められない"))
        with logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
            self.app.run_forever()
        record = captured.records[0]
        self.assertEqual(record.getMessage(),
                         "次の予定を求められませんでした（60 秒後にやり直します）: 予定を求められない")
        self.assertIsNotNone(record.exc_info)

    def test_a_failing_recheck_after_the_wait_is_retried_too(self):
        waits = self.stub_waits()
        event = self.past_event()
        # 1 回目の選択は成功、待機後の再確認で例外。やり直して、次の周で鳴らす。
        self.script_next_event(event, event, RuntimeError("再確認で失敗"))
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(waits[0], (60, 0))
        self.assertEqual(self.player.played, [self.wav])

    def test_a_failing_wait_for_the_prepare_time_is_retried_too(self):
        waits = self.stub_waits()
        event = self.past_event()
        self.script_next_event(event)
        real_sleep_until = self.app.scheduler.sleep_until
        failures = []

        def flaky_sleep_until(*args, **kwargs):
            self.guard.tick("sleep_until")
            if not failures:
                failures.append(1)
                raise RuntimeError("時計が壊れた")
            return real_sleep_until(*args, **kwargs)

        self.app.scheduler.sleep_until = flaky_sleep_until
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(waits[0], (60, 0))
        self.assertEqual(self.player.played, [self.wav])

    def test_upcoming_raising_at_startup_does_not_stop_the_loop(self):
        self.stub_waits()
        event = self.past_event()
        self.script_next_event(event)
        self.app.scheduler.upcoming = mock.Mock(side_effect=RuntimeError("一覧を作れない"))
        with logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
            self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(captured.records[0].getMessage(),
                         "次回以降の予定を求められませんでした（継続します）: 一覧を作れない")
        self.assertIsNotNone(captured.records[0].exc_info)
        self.assertEqual(self.player.played, [self.wav])

    def test_upcoming_is_listed_once_at_startup(self):
        self.stub_waits()
        self.script_next_event(self.past_event())
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.run_forever()
        listed = [record.getMessage() for record in captured.records
                  if record.getMessage().startswith("次回以降の予定:")]
        self.assertEqual(len(listed), 1)

    def test_no_pending_event_still_waits_60_seconds_with_the_same_message(self):
        waits = self.stub_waits(stop_after=1)

        def no_event(is_fired=None):
            self.guard.tick("next_event")

        self.app.scheduler.next_event = no_event
        with logs_enabled(), self.assertLogs("chime.app", level="WARNING") as captured:
            self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(waits, [(60, 0)])
        self.assertIn("予定されたイベントがありません。60 秒後に再確認します。",
                      [record.getMessage() for record in captured.records])

    def test_a_changed_schedule_is_logged_and_recalculated(self):
        self.stub_waits()
        first, second = self.past_event(10.0), self.past_event(5.0)
        self.script_next_event(second, first, second)
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.run_forever()
        self.assertIn("待機中に予定が変わりました。再計算します。",
                      [record.getMessage() for record in captured.records])
        self.assertEqual(self.app.builder.built, [second])

    def test_a_loop_that_never_stops_fails_fast_instead_of_hanging(self):
        # 停止を要求しない待機と、いつも失敗する予定。止まらないループは打ち切られる。
        self.stub_waits()  # stop_after なし: 止まらない
        self.script_next_event(self.past_event(), *[RuntimeError("だめ")] * 1000)
        with self.assertRaises(RunawayLoop):
            self.app.run_forever()
        self.assertEqual(max(self.guard.counts.values()), CALL_LIMIT + 1)

    def test_a_failure_in_the_scheduler_does_not_mark_anything_fired(self):
        self.stub_waits(stop_after=1)
        event = self.past_event()

        def fake_next(is_fired=None):
            self.guard.tick("next_event")
            raise RuntimeError("だめ")

        self.app.scheduler.next_event = fake_next
        self.app.run_forever()
        self.assertFalse(self.app.state.is_fired(event.key, event.day))


class LogEnvironmentTest(AppTestCase):
    """起動時のログ: 版・実行環境・作り置きの音声が揃っているか。"""

    def make_app(self, skip=(), engines=None):
        """同梱のひとこと・作り置きを使う ChimeApp。``skip`` の文言だけ、声のファイルを置かない。"""
        config = Config(DEFAULT_CONFIG, base_dir=self.root)
        config.data["quotes"]["file"] = SHIPPED_QUOTES
        voice = os.path.join(self.root, "voice")
        os.makedirs(voice)
        for text in phrases.collect_phrases(config, include_quotes=True):
            if text not in skip:
                make_wav(os.path.join(voice, prerecorded_filename(text)), seconds=0.001)
        config.data["tts"]["prerecorded_dir"] = voice
        if engines is not None:
            config.data["tts"]["engines"] = engines
        app = ChimeApp(config, backend="mock", dry_run=True)
        app._player = RecordingPlayer()
        return app, phrases.collect_phrases(config, include_quotes=True)

    def messages(self, app, level="INFO"):
        with logs_enabled(), self.assertLogs("chime.app", level=level) as captured:
            app.log_environment()
        return [(record.levelname, record.getMessage()) for record in captured.records]

    def test_the_first_line_is_the_version(self):
        with mock.patch("chime.app.buildinfo.version_string",
                        return_value="campus-chime 6.1.0 (abc1234)"):
            lines = self.messages(self.app)
        self.assertEqual(lines[0], ("INFO", "campus-chime 6.1.0 (abc1234)"))

    def test_the_version_line_is_the_real_version_string(self):
        self.assertEqual(self.messages(self.app)[0][1], buildinfo.version_string())

    def test_run_forever_starts_by_logging_the_version(self):
        """常駐の起動ログ（版・実行環境・作り置き）は ``run_forever`` が出す。"""
        guard = CallGuard()

        def stop_at_once(is_fired=None):
            guard.tick("next_event")
            self.app.stop_event.set()

        self.app.scheduler.next_event = stop_at_once
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(captured.records[0].getMessage(), buildinfo.version_string())

    def test_the_existing_lines_are_still_logged(self):
        lines = [message for _, message in self.messages(self.app)]
        self.assertTrue(any(line.startswith("実行環境: ") for line in lines))
        self.assertTrue(any(line.startswith("再生バックエンド: recording / TTS: ") for line in lines))
        self.assertTrue(any(line.startswith("設定ソース: ") for line in lines))

    def test_the_coverage_line_comes_last_and_counts_every_phrase(self):
        app, enumerated = self.make_app()
        lines = self.messages(app)
        total = len(enumerated)
        self.assertEqual(lines[-1], ("INFO", "作り置きの音声: {0}/{0} 件".format(total)))

    def test_a_complete_set_gives_no_warning(self):
        app, _ = self.make_app()
        self.assertEqual([level for level, _ in self.messages(app)
                          if level in ("WARNING", "ERROR")], [])

    def test_missing_voices_are_counted_and_warned_with_the_first_three(self):
        _, enumerated = self.make_app()
        missing = [enumerated[2], enumerated[7], enumerated[11], enumerated[20]]
        app, _ = self.make_app_fresh(skip=missing)
        lines = self.messages(app)
        total = len(enumerated)
        self.assertIn(("INFO", "作り置きの音声: {0}/{1} 件".format(total - 4, total)), lines)
        warnings = [message for level, message in lines if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("4 件", warnings[0])
        for text in missing[:3]:
            self.assertIn("「{0}」".format(text), warnings[0])
        self.assertNotIn(missing[3], warnings[0])
        positions = [warnings[0].index(text) for text in missing[:3]]
        self.assertEqual(positions, sorted(positions), "列挙の順に挙げる")

    def test_fewer_than_three_missing_lists_just_those(self):
        _, enumerated = self.make_app()
        app, _ = self.make_app_fresh(skip=[enumerated[5]])
        warnings = [message for level, message in self.messages(app) if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("1 件", warnings[0])
        self.assertIn("「{0}」".format(enumerated[5]), warnings[0])

    def test_exactly_three_missing_lists_all_three(self):
        _, enumerated = self.make_app()
        missing = enumerated[:3]
        app, _ = self.make_app_fresh(skip=missing)
        warnings = [message for level, message in self.messages(app) if level == "WARNING"]
        for text in missing:
            self.assertIn("「{0}」".format(text), warnings[0])

    def test_without_the_prerecorded_engine_everything_is_missing(self):
        app, enumerated = self.make_app(engines=["voicevox"])
        lines = self.messages(app)
        self.assertIn(("INFO", "作り置きの音声: 0/{0} 件".format(len(enumerated))), lines)
        self.assertTrue(any(level == "WARNING" and "{0} 件".format(len(enumerated)) in message
                            for level, message in lines))

    def test_the_coverage_uses_the_prerecorded_lookup_of_the_tts_service(self):
        app, _ = self.make_app()
        with mock.patch("chime.app.coverage", wraps=phrases.coverage) as counted:
            self.messages(app)
        counted.assert_called_once_with(app.config, app.tts.prerecorded_lookup)

    def test_a_coverage_that_cannot_be_counted_is_a_warning_not_a_crash(self):
        app, _ = self.make_app()
        with mock.patch("chime.app.coverage", side_effect=RuntimeError("数えられない")):
            lines = self.messages(app)
        self.assertIn(("WARNING", "作り置きの音声を数えられませんでした: "
                                   "RuntimeError: 数えられない"), lines)
        self.assertTrue(any(message.startswith("設定ソース: ") for _, message in lines))

    def test_a_huge_temperature_range_skips_the_count_with_a_warning(self):
        # weather.prerecord.temp_max が巨大でも、起動のたびに何億件も数えない。
        app, _ = self.make_app()
        app.config.data["weather"]["prerecord"]["temp_max"] = 10 ** 9
        with mock.patch("chime.weather.prerecord_phrases",
                        side_effect=AssertionError("列挙してはいけない")) as listing:
            lines = self.messages(app)
        listing.assert_not_called()
        warnings = [message for level, message in lines if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith(
            "作り置きの音声の数え上げを省きました（起動を遅くしないため）: "), warnings[0])
        self.assertIn("1000000006 度分", warnings[0])
        self.assertIn("上限は 200 度分", warnings[0])
        self.assertFalse(any(message.startswith("作り置きの音声: ") for _, message in lines))
        self.assertTrue(any(message.startswith("設定ソース: ") for _, message in lines))

    def test_a_too_large_count_is_a_warning_about_skipping_not_about_failing(self):
        app, _ = self.make_app()
        with mock.patch("chime.app.coverage", side_effect=phrases.CoverageTooLarge("多すぎる")):
            lines = self.messages(app)
        self.assertIn(("WARNING", "作り置きの音声の数え上げを省きました（起動を遅くしないため）: "
                                   "多すぎる"), lines)

    def test_other_value_errors_are_still_reported_as_failing_to_count(self):
        app, _ = self.make_app()
        with mock.patch("chime.app.coverage", side_effect=ValueError("壊れた")):
            lines = self.messages(app)
        self.assertIn(("WARNING", "作り置きの音声を数えられませんでした: ValueError: 壊れた"), lines)

    def test_a_huge_width_in_a_template_skips_the_count_without_building_the_sentences(self):
        # 天気を止めていても、数え上げは天気の文言も列挙する。幅 2 億の文は 200 MB になる。
        app, _ = self.make_app()
        app.config.data["weather"]["enabled"] = False
        app.config.data["weather"]["sentence_weather"] = "{label:>200000000}"
        with mock.patch("chime.weather.prerecord_phrases",
                        side_effect=AssertionError("列挙してはいけない")) as listing:
            lines = self.messages(app)
        listing.assert_not_called()
        warnings = [message for level, message in lines if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith(
            "作り置きの音声の数え上げを省きました（起動を遅くしないため）: "), warnings[0])
        self.assertIn("weather.sentence_weather", warnings[0])
        self.assertNotIn("CoverageTooLarge", warnings[0])

    def test_a_huge_location_name_skips_the_count_without_building_the_sentences(self):
        app, _ = self.make_app()
        app.config.data["weather"]["enabled"] = False
        app.config.data["weather"]["open_meteo"]["locations"] = [
            {"label": "あ" * 100_000, "latitude": 35.0, "longitude": 135.0}]
        with mock.patch("chime.weather.prerecord_phrases",
                        side_effect=AssertionError("列挙してはいけない")) as listing:
            lines = self.messages(app)
        listing.assert_not_called()
        warnings = [message for level, message in lines if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("weather.open_meteo.locations", warnings[0])
        self.assertIn("weather.prerecord", warnings[0])

    def test_the_count_is_still_made_for_a_wide_but_allowed_range(self):
        # 上限（200 度分）以内なら、広くても数える。足した 100 度分は、作り置きが無いので足りない。
        app, enumerated = self.make_app()
        app.config.data["weather"]["prerecord"]["temp_max"] = 40 + 100
        lines = self.messages(app)
        count = len(enumerated)
        self.assertIn(("INFO", "作り置きの音声: {0}/{1} 件".format(count, count + 100)), lines)
        self.assertFalse(any("省きました" in message for _, message in lines))

    def test_a_tts_that_cannot_be_described_does_not_stop_the_startup(self):
        # VOICEVOX ENGINE への疎通確認が例外になっても（設定の値次第で起こる）、起動は続く。
        app, _ = self.make_app()
        with mock.patch.object(app.tts, "describe", side_effect=OverflowError("timestamp out of range")):
            lines = self.messages(app)
        warnings = [message for level, message in lines if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith("読み上げエンジンの状態を調べられませんでした（起動は続けます）: "),
                        warnings[0])
        self.assertIn("OverflowError: timestamp out of range", warnings[0])
        messages = [message for _, message in lines]
        self.assertIn("再生バックエンド: recording / TTS: 確認できません", messages)
        self.assertTrue(any(message.startswith("設定ソース: ") for message in messages))
        self.assertTrue(any(message.startswith("作り置きの音声: ") for message in messages))

    def test_a_huge_probe_timeout_does_not_stop_the_startup(self):
        """ソケットが受けない待ち時間（``OverflowError``）。``urlopen`` は通信の失敗に直さない。"""

        def socket_like_urlopen(request, *args, timeout=None, **kwargs):
            with contextlib.closing(socket.socket()) as sock:
                sock.settimeout(timeout)  # 本物と同じ拒み方。通信はしない
            raise urllib.error.URLError("テスト中は通信しません")

        app, _ = self.make_app()
        app.config.data["tts"]["voicevox"]["probe_timeout_seconds"] = 1e12
        app.tts = type(app.tts)(app.config.section("tts"), app.config.path("tts.cache_dir"),
                                app.config.path("tts.prerecorded_dir"))
        with mock.patch("urllib.request.urlopen", socket_like_urlopen):
            with self.assertRaises(OverflowError):  # 守らなければ、これが起動を止める
                app.tts.describe()
            lines = self.messages(app)
        warnings = [message for level, message in lines if level == "WARNING"]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("読み上げエンジンの状態を調べられませんでした", warnings[0])
        self.assertTrue(any(message.startswith("作り置きの音声: ") for _, message in lines))

    def test_the_tts_line_is_unchanged_when_the_engines_can_be_described(self):
        app, _ = self.make_app()
        lines = [message for _, message in self.messages(app)]
        self.assertIn("再生バックエンド: recording / TTS: {0}".format(app.tts.describe()), lines)

    def test_a_broken_announce_template_does_not_stop_the_startup(self):
        # 文言の列挙がテンプレートの書き間違いで例外になる設定でも、起動は続く。
        self.app.config.data["time_signal"]["announce_template"] = "{hours}"
        lines = self.messages(self.app)
        warnings = [message for level, message in lines if level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith("作り置きの音声を数えられませんでした: KeyError"))
        self.assertTrue(any(message.startswith("設定ソース: ") for _, message in lines))

    def make_app_fresh(self, skip):
        """``make_app`` を呼び直すため、前回作った声のフォルダを消してから作る。"""
        import shutil
        shutil.rmtree(os.path.join(self.root, "voice"))
        return self.make_app(skip=skip)


class StateFileTest(unittest.TestCase):
    """``--dry-run`` の ChimeApp は、実機の state.json を作らず・書き換えない。"""

    def setUp(self):
        block_network(self)

    def make_app(self, tmp, dry_run):
        config = Config(DEFAULT_CONFIG, base_dir=tmp)
        config.data["state"]["file"] = os.path.join(tmp, "state", "state.json")
        return ChimeApp(config, backend="mock", dry_run=dry_run), config.data["state"]["file"]

    def test_dry_run_does_not_create_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, state_file = self.make_app(tmp, dry_run=True)
            app.state.mark_fired("hourly:10", "2026-08-26")
            app.state.remember_quote("テストのひとこと")
            self.assertFalse(os.path.exists(state_file))
            self.assertFalse(os.path.exists(os.path.dirname(state_file)))

    def test_dry_run_does_not_touch_an_existing_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(DEFAULT_CONFIG, base_dir=tmp)
            state_file = os.path.join(tmp, "state.json")
            config.data["state"]["file"] = state_file
            original = '{"last_fired": {"hourly:10": "2026-08-25"}, "recent_quotes": ["きのうの一言"]}\n'
            with open(state_file, "w", encoding="utf-8") as handle:
                handle.write(original)

            app = ChimeApp(config, backend="mock", dry_run=True)
            # 既存の記録は読める（直近のひとことを避けるため）。
            self.assertEqual(app.state.recent_quotes(), ["きのうの一言"])
            app.state.mark_fired("hourly:11", "2026-08-26")
            with open(state_file, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), original)

    def test_dry_run_run_event_does_not_create_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, state_file = self.make_app(tmp, dry_run=True)
            wav = make_wav(os.path.join(tmp, "beep.wav"), seconds=0.01)
            app.builder = StubBuilder([Segment(wav, label="テスト音")], quote="テストのひとこと")
            moment = app.now() - timedelta(seconds=5)
            app.run_event(make_event(moment))
            self.assertFalse(os.path.exists(state_file))

    def test_a_normal_app_does_write_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, state_file = self.make_app(tmp, dry_run=False)
            app.state.mark_fired("hourly:10", "2026-08-26")
            self.assertTrue(os.path.exists(state_file))


if __name__ == "__main__":
    unittest.main()
