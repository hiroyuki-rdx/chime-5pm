"""常駐ループ（ChimeApp）のテスト。"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from tests.support import (SHIPPED_QUOTES, RecordingPlayer, block_network,
                           logs_enabled, make_event, make_wav)

from chime import buildinfo, phrases
from chime.app import (INTERRUPTED_TEXT, NO_EVENTS_RECHECK_SECONDS, RETRY_SECONDS, ChimeApp,
                       PlayOutcome, describe_exception, exception_text, pause_reason)
from chime.audio import Player, PlaybackError, PlaybackInterrupted
from chime.config import DEFAULT_CONFIG, Config
from chime.history import History
from chime.pause import Pause
from chime.scheduler import Event, Scheduler, format_events
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
    """設定を一時フォルダに置いた ``ChimeApp``（再生は記録するだけの偽物）。

    本物の待機（``sleep_until`` の中の ``stop_event.wait``）には入らない。時計が進まない・
    予定が無いなどで本物の待機に入ると、数分から 1 時間は戻らない。退行（再生済みにならない、
    休止を見逃す、割り込みの結果を無視する、など）があるとテストが固まって CI が止まって
    しまうので、``stop_event.wait`` を数えるだけの代用品に差し替え、``self.guard`` が上限
    （``CALL_LIMIT``）を超えて数えたら ``RunawayLoop`` で打ち切る。待ち方を調べるテストは、
    ``mock.patch.object(self.app.stop_event, "wait", …)`` で、さらに上から差し替える。
    """

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
        self.guard = CallGuard()
        self.app.stop_event.wait = self.never_really_wait

    def never_really_wait(self, timeout=None):
        """``stop_event.wait`` の代わり。待たずに数えて、止まったかを返す（上限を超えたら ``RunawayLoop``）。"""
        self.guard.tick("stop_event.wait")
        return self.app.stop_event.is_set()

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

        def spying(target, stop=None, precise=False, interrupt=None):
            calls.append((target, precise))
            return real(target, stop, precise, interrupt)

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
        [(args, kwargs)] = calls
        self.assertEqual((args, list(kwargs)), ((), ["is_fired"]))
        # 再生済みかどうかは、呼ぶ時点の state を見る（休止の判定も同じ関数に入っている）。
        event = self.past_event()
        self.assertFalse(kwargs["is_fired"](event))
        self.app.state.mark_fired(event.key, event.day)
        self.assertTrue(kwargs["is_fired"](event))

    def test_next_pending_event_reads_the_scheduler_at_call_time(self):
        event = self.past_event()
        self.app.scheduler = mock.Mock()
        self.app.scheduler.next_event.return_value = event
        self.assertIs(self.app._next_pending_event(), event)
        self.app.scheduler.next_event.assert_called_once()
        args, kwargs = self.app.scheduler.next_event.call_args
        self.assertEqual((args, list(kwargs)), ((), ["is_fired"]))

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
        """イベントを 1 件だけ返し、再生済みになったら停止するスケジューラ。

        再生済みにならない退行（``is_fired`` が効かない）では、同じ放送を鳴らし続けて止まらない。
        呼び出し回数を数え、上限を超えたら ``RunawayLoop`` で打ち切る。
        """
        guard = CallGuard()

        def fake_next(is_fired=None):
            guard.tick("next_event")
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
        guard = CallGuard()

        def fake_next(*args, **kwargs):
            guard.tick("next_event")
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

        def stopping_sleep_until(target, stop=None, precise=False, interrupt=None):
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
        guard = CallGuard()

        def fake_next(is_fired=None):
            guard.tick("next_event")
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
        guard = CallGuard()

        def fake_next(is_fired=None):
            guard.tick("next_event")      # 再生済みにならない退行で、止まらずに回り続けないように
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

    def test_no_pending_event_waits_in_interruptible_steps_and_warns(self):
        # 予定が無いときの待ちは、停止要求に応えられるよう scheduler.sleep_until の
        # 刻み（max_sleep_seconds ずつ）で待つ。1 度の警告に、再確認の間隔を書く。
        waits = self.stub_waits(stop_after=1)

        def no_event(is_fired=None):
            self.guard.tick("next_event")

        self.app.scheduler.next_event = no_event
        with logs_enabled(), self.assertLogs("chime.app", level="WARNING") as captured:
            self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(waits, [(30.0, 0)])
        [warning] = [record.getMessage() for record in captured.records
                     if record.getMessage().startswith("予定されたイベントがありません")]
        self.assertIn("60 分ごとに再確認します", warning)

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


# ---------------------------------------------------------------------------
# 休止（「今日だけ止める」）・中断・予定が無いときの待ち
# ---------------------------------------------------------------------------
TOKYO = ZoneInfo("Asia/Tokyo")

#: 2026-10-14 は水曜日。既定のスケジュールでは、時報（10〜16 時）と閉館放送（16:57）が鳴る日。
WEDNESDAY = datetime(2026, 10, 14, 9, 30, tzinfo=TOKYO)
THURSDAY_DATE = date(2026, 10, 15)


class PauseTestCase(HistoryTestCase):
    """時計を水曜の朝に止めた ``ChimeApp``。休止の記録は一時フォルダの ``cache/pause.json``。

    時計は止まっているので、本物の待機に入ると時刻が進まず永久に終わらない。``AppTestCase`` の
    ``stop_event.wait`` の代用品（``self.guard`` で数えて、``CALL_LIMIT`` を超えたら ``RunawayLoop``）が
    それを打ち切る。偽の ``sleep_until``（:meth:`install_sleeper`）も同じ ``guard`` で数える。
    """

    def setUp(self):
        super().setUp()
        self.moment = WEDNESDAY
        self.app.now = lambda: self.moment
        self.app.scheduler = Scheduler(DEFAULT_CONFIG["schedule"], self.app.tzinfo, 3.0,
                                       clock=self.app.now)

    def set_pause(self, last_day=date(2026, 10, 14), reason="点検"):
        """現地で ``--pause-today`` / ``--pause-until`` を実行したのと同じ記録を書く。"""
        return self.app.pause.set(last_day, reason, now=self.moment)

    def write_pause(self, first_day, last_day, reason=""):
        """今日より先から始まる記録など、``set`` では作れない記録を直接書く。"""
        os.makedirs(os.path.dirname(self.app.pause.path), exist_ok=True)
        with open(self.app.pause.path, "w", encoding="utf-8") as handle:
            json.dump({"v": 1, "first_day": first_day.isoformat(), "last_day": last_day.isoformat(),
                       "set_at": self.moment.isoformat(), "reason": reason}, handle)

    def scheduled(self, key="hourly:10", day=date(2026, 10, 14)):
        """スケジュールどおりのイベント（日付と key で選ぶ）。"""
        [found] = [event for event in self.app.scheduler.events_for_date(day) if event.key == key]
        return found

    def install_sleeper(self, *behaviours):
        """``scheduler.sleep_until`` の代わり。呼び出しを記録し、呼び出しごとの振る舞いを順に実行する。

        振る舞いは ``(target, interrupt)`` を受けて、戻り値（待ち終えたか）を返す関数。
        尽きたあとは、すぐ ``True``（待ち終えた）を返す。呼び出しは ``self.guard`` で数え、
        ``CALL_LIMIT`` 回を超えたら ``RunawayLoop``（ループが止まらない退行で固まらない）。
        """
        calls = []
        script = list(behaviours)

        def fake(target, stop=None, precise=False, interrupt=None):
            self.guard.tick("sleep_until")   # 尽きたあとも True を返し続けるので、止まらないループは打ち切る
            calls.append({"target": target, "stop": stop, "precise": precise, "interrupt": interrupt})
            return script.pop(0)(target, interrupt) if script else True

        self.app.scheduler.sleep_until = fake
        return calls

    def skipped_entry(self):
        entry = self.only_entry()
        self.assertEqual(entry["result"], "skipped", entry)
        return entry


class PauseStoreWiringTest(PauseTestCase):
    def test_the_record_lives_in_the_configured_place(self):
        self.assertEqual(self.app.pause.path, os.path.join(self.root, "cache", "pause.json"))
        self.assertEqual(self.app.pause.path, self.app.config.path("state.pause_file"))

    def test_today_follows_the_apps_clock_in_the_configured_zone(self):
        self.moment = datetime(2026, 10, 14, 23, 30, tzinfo=TOKYO)
        self.assertEqual(self.set_pause().first_day, date(2026, 10, 14))
        self.moment = datetime(2026, 10, 15, 0, 30, tzinfo=TOKYO)
        self.assertIsNone(self.app.pause.current())  # 翌日 0 時を過ぎれば、今日だけの休止は切れる

    def test_an_empty_place_means_pausing_is_not_used(self):
        config = Config(DEFAULT_CONFIG, base_dir=self.root)
        config.data["state"]["pause_file"] = ""
        app = ChimeApp(config, backend="mock")
        self.assertEqual(app.pause.path, "")
        self.assertIsNone(app.pause.current())
        self.assertFalse(app.pause.changed())
        self.assertFalse(app.pause.changed())

    def test_pause_reason_reads_well_in_the_log_and_the_history(self):
        pause = Pause(date(2026, 10, 14), date(2026, 10, 14), WEDNESDAY, "学園祭の準備")
        self.assertEqual(pause_reason(pause), "休止: 学園祭の準備")
        self.assertEqual(pause_reason(Pause(date(2026, 10, 14), date(2026, 10, 14), WEDNESDAY, "")),
                         "休止")


class PauseSelectionTest(PauseTestCase):
    """休止中の日のイベントは、選ばれない（再生済みにはしない）。"""

    def test_without_a_pause_the_next_event_is_the_ten_oclock_chime(self):
        event = self.app._next_pending_event()
        self.assertEqual((event.key, event.day), ("hourly:10", "2026-10-14"))

    def test_a_paused_day_is_skipped_and_the_next_day_comes_first(self):
        self.set_pause()
        event = self.app._next_pending_event()
        self.assertEqual((event.key, event.day), ("hourly:10", "2026-10-15"))

    def test_a_pause_over_a_weekend_resumes_on_monday(self):
        self.set_pause(date(2026, 10, 16))
        event = self.app._next_pending_event()
        self.assertEqual((event.key, event.day), ("hourly:10", "2026-10-19"))

    def test_a_pause_for_another_day_changes_nothing(self):
        self.write_pause(THURSDAY_DATE, THURSDAY_DATE)
        event = self.app._next_pending_event()
        self.assertEqual((event.key, event.day), ("hourly:10", "2026-10-14"))

    def test_an_event_of_the_first_unpaused_day_is_still_chosen_by_its_own_day(self):
        self.write_pause(date(2026, 10, 14), date(2026, 10, 14))
        self.moment = datetime(2026, 10, 14, 16, 0, tzinfo=TOKYO)
        self.assertEqual(self.app._next_pending_event().day, "2026-10-15")

    def test_a_pause_that_has_ended_does_not_matter(self):
        self.write_pause(date(2026, 10, 12), date(2026, 10, 13))
        self.assertEqual(self.app._next_pending_event().day, "2026-10-14")

    def test_a_broken_pause_file_is_not_a_pause_and_warns_once(self):
        os.makedirs(os.path.dirname(self.app.pause.path))
        with open(self.app.pause.path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        with logs_enabled(), self.assertLogs("chime.pause", level="WARNING") as captured:
            first = self.app._next_pending_event()
            second = self.app._next_pending_event()
        self.assertEqual((first.day, second.day), ("2026-10-14", "2026-10-14"))
        self.assertEqual(len(captured.records), 1)
        self.assertIn("放送は通常どおり行います", captured.records[0].getMessage())

    def test_a_failing_pause_store_does_not_stop_the_selection(self):
        with mock.patch.object(self.app.pause, "current", side_effect=RuntimeError("壊れた")), \
                logs_enabled(), self.assertLogs("chime.app", level="WARNING") as captured:
            event = self.app._next_pending_event()
        self.assertEqual(event.day, "2026-10-14")
        self.assertIn("放送は通常どおり行います", captured.records[0].getMessage())
        self.assertIn("RuntimeError: 壊れた", captured.records[0].getMessage())

    def test_the_pause_is_read_once_per_selection(self):
        self.set_pause(date(2026, 10, 30))
        with mock.patch.object(self.app.pause, "current",
                               wraps=self.app.pause.current) as current:
            event = self.app._next_pending_event()
        self.assertEqual(event.day, "2026-11-02")  # 10/30 は金曜。週末を挟んで、次は月曜
        self.assertEqual(current.call_count, 1)

    def test_a_just_missed_event_plays_once_the_pause_is_lifted_within_the_grace(self):
        self.moment = datetime(2026, 10, 14, 10, 0, 30, tzinfo=TOKYO)  # 再生開始 9:59:57 の 33 秒後
        self.set_pause()
        self.assertEqual(self.app._next_pending_event().day, "2026-10-15")
        self.app.pause.clear()
        event = self.app._next_pending_event()
        self.assertEqual((event.key, event.day), ("hourly:10", "2026-10-14"))

    def test_a_pause_lifted_after_the_grace_does_not_replay_the_missed_event(self):
        self.moment = datetime(2026, 10, 14, 10, 3, 0, tzinfo=TOKYO)  # 猶予の 120 秒を過ぎた
        self.set_pause()
        self.app.pause.clear()
        self.assertEqual(self.app._next_pending_event().key, "hourly:11")

    def test_a_paused_event_is_not_marked_fired(self):
        self.set_pause()
        event = self.scheduled()
        self.app._next_pending_event()
        self.assertFalse(self.app._is_fired(event))


class RunEventPauseTest(PauseTestCase):
    """``run_event`` は、組み立てる直前と再生する直前に休止を確かめる。"""

    def test_a_pause_before_the_build_plays_nothing_and_does_not_mark_fired(self):
        self.set_pause()
        event = self.scheduled()
        self.app.run_event(event)
        self.assertEqual(self.app.builder.built, [])
        self.assertEqual(self.player.played, [])
        self.assertFalse(self.app.state.is_fired(event.key, event.day))

    def test_the_skip_is_recorded_in_the_history_with_the_reason(self):
        self.set_pause(reason="学園祭の準備")
        event = self.scheduled()
        self.app.run_event(event)
        entry = self.skipped_entry()
        self.assertEqual((entry["kind"], entry["key"], entry["day"]), ("hourly", "hourly:10", "2026-10-14"))
        self.assertEqual(entry["at"], event.at.isoformat(timespec="seconds"))
        self.assertEqual((entry["played"], entry["total"]), (0, 0))
        self.assertEqual(entry["reason"], "休止: 学園祭の準備")
        self.assertNotIn("error", entry)

    def test_a_pause_without_a_reason_says_just_paused(self):
        self.set_pause(reason="")
        self.app.run_event(self.scheduled())
        self.assertEqual(self.skipped_entry()["reason"], "休止")

    def test_a_pause_set_while_waiting_for_the_play_time_skips(self):
        event = self.scheduled()

        def operator_pauses(target, interrupt):
            self.set_pause()
            self.assertTrue(interrupt())  # 待機の割り込みの確認が、休止に気づく
            return False

        calls = self.install_sleeper(operator_pauses)
        self.app.run_event(event)
        self.assertEqual(self.app.builder.built, [event])  # 組み立ては、休止の前に済んでいた
        self.assertEqual(self.player.played, [])
        self.assertFalse(self.app.state.is_fired(event.key, event.day))
        self.assertEqual(len(calls), 1)
        self.skipped_entry()

    def test_a_pause_set_in_the_last_moment_is_still_noticed_right_before_playing(self):
        # 待機が（割り込みを見ないまま）終わり、そのあとで休止が書かれた。
        event = self.scheduled()

        def operator_pauses_at_the_last_moment(target, interrupt):
            self.set_pause()
            return True

        self.install_sleeper(operator_pauses_at_the_last_moment)
        self.app.run_event(event)
        self.assertEqual(self.player.played, [])
        self.assertFalse(self.app.state.is_fired(event.key, event.day))
        self.skipped_entry()

    def test_the_wait_is_given_an_interrupt_that_is_true_only_while_the_pause_covers_the_event(self):
        event = self.scheduled()
        seen = []

        def look(target, interrupt):
            seen.append(interrupt())            # 休止なし
            self.write_pause(THURSDAY_DATE, THURSDAY_DATE)
            seen.append(interrupt())            # 別の日の休止
            self.set_pause()
            seen.append(interrupt())            # このイベントの日の休止
            self.app.pause.clear()
            seen.append(interrupt())            # 解除
            return True

        calls = self.install_sleeper(look)
        self.app.run_event(event)
        self.assertEqual(seen, [False, False, True, False])
        self.assertEqual(calls[0]["target"], event.play_at)
        self.assertIs(calls[0]["precise"], True)
        self.assertIs(calls[0]["stop"], self.app.stop_event)
        self.assertEqual(self.player.played, [self.wav])  # 解除されたので、鳴らす

    def test_a_pause_lifted_right_after_the_interrupt_waits_again_instead_of_playing_early(self):
        event = self.scheduled()

        def paused_then_lifted(target, interrupt):
            self.set_pause()
            self.assertTrue(interrupt())
            self.app.pause.clear()
            return False                          # 休止が効いたので待ちを打ち切った、が、もう解けた

        calls = self.install_sleeper(paused_then_lifted)
        self.app.run_event(event)
        # 再生の時刻より前に鳴らさない。もう一度、再生の時刻まで待つ（こんどは休止では打ち切らない）。
        self.assertEqual([call["target"] for call in calls], [event.play_at, event.play_at])
        self.assertIsNotNone(calls[0]["interrupt"])
        self.assertIsNone(calls[1]["interrupt"])
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))
        self.assertEqual(self.entries()[0]["result"], "ok")

    def test_a_stop_request_beats_a_pause_and_leaves_no_skip_entry(self):
        event = self.scheduled()

        def stop_and_pause(target, interrupt):
            self.app.stop_event.set()
            self.set_pause()
            return False

        self.install_sleeper(stop_and_pause)
        self.app.run_event(event)
        self.assertEqual(self.player.played, [])
        self.assertFalse(self.app.state.is_fired(event.key, event.day))
        self.assertEqual(self.entries(), [])

    def test_the_skip_is_logged(self):
        self.set_pause(reason="点検")
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.run_event(self.scheduled())
        messages = [record.getMessage() for record in captured.records]
        self.assertTrue(any(message.startswith("休止中のため鳴らしません（休止: 点検）") for message in messages),
                        messages)

    def test_a_pause_for_another_day_plays_normally(self):
        self.write_pause(THURSDAY_DATE, THURSDAY_DATE)
        event = self.past_event()
        self.install_sleeper()
        self.app.run_event(event)
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))
        self.assertEqual(self.only_entry()["result"], "ok")

    def test_a_broken_pause_file_plays_normally(self):
        os.makedirs(os.path.dirname(self.app.pause.path))
        with open(self.app.pause.path, "wb") as handle:
            handle.write(b"\xff\xfe not utf-8")
        event = self.scheduled()
        self.install_sleeper()
        self.app.run_event(event)
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_a_pause_that_is_a_directory_plays_normally(self):
        os.makedirs(self.app.pause.path)
        event = self.scheduled()
        self.install_sleeper()
        self.app.run_event(event)
        self.assertEqual(self.player.played, [self.wav])

    def test_a_failing_pause_store_never_stops_the_broadcast(self):
        event = self.scheduled()
        self.install_sleeper()
        with mock.patch.object(self.app.pause, "current", side_effect=RuntimeError("壊れた")), \
                logs_enabled(), self.assertLogs("chime.app", level="WARNING"):
            self.app.run_event(event)
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))

    def test_a_failing_pause_store_does_not_break_the_real_wait(self):
        # 本物の sleep_until の上で（偽の待機に置き換えない）。休止の確認が失敗しても鳴る。
        self.moment = datetime(2026, 10, 14, 9, 59, 57, tzinfo=TOKYO)
        event = self.scheduled()
        with mock.patch.object(self.app.pause, "current", side_effect=RuntimeError("壊れた")), \
                logs_enabled(), self.assertLogs(level="WARNING"):
            self.app.run_event(event)
        self.assertEqual(self.player.played, [self.wav])

    def test_a_dry_run_skips_without_writing_a_history(self):
        self.app.dry_run = True
        self.set_pause()
        event = self.scheduled()
        self.app.run_event(event)
        self.assertEqual(self.player.played, [])
        self.assertEqual(self.app.builder.built, [])
        self.assertFalse(os.path.exists(self.history_path))

    def test_the_quote_is_not_used_up_by_a_paused_broadcast(self):
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")], quote="ひとこと")
        self.set_pause()
        self.app.run_event(self.scheduled())
        self.assertEqual(self.app.state.recent_quotes(), [])


class PauseLoopTest(PauseTestCase):
    """常駐ループ: 待っているあいだに休止が書かれたら、待つのをやめて選び直す。"""

    def test_the_prepare_wait_is_interrupted_by_a_change_of_the_pause_record(self):
        calls = self.install_sleeper(lambda target, interrupt: self.app.stop_event.set() or False)
        self.app.run_forever()
        self.assertEqual(calls[0]["target"], self.scheduled().prepare_at)
        self.assertEqual(calls[0]["interrupt"], self.app.pause.changed)
        self.assertIs(calls[0]["stop"], self.app.stop_event)

    def test_a_pause_set_while_waiting_means_no_chime_that_day(self):
        today, tomorrow = self.scheduled(), self.scheduled(day=THURSDAY_DATE)

        def operator_pauses(target, interrupt):
            self.assertFalse(interrupt())    # 待ち始めは、変化なし
            self.set_pause()                  # 現地で --pause-today
            self.assertTrue(interrupt())     # 待機中の確認が、書き換わりに気づく
            return False

        def stop(target, interrupt):
            self.app.stop_event.set()
            return False

        calls = self.install_sleeper(operator_pauses, stop)
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.assertEqual(self.app.run_forever(), 0)
        # 選び直した次の待ちは、翌日の 10 時の準備。今日のイベントは何も起きない。
        self.assertEqual([call["target"] for call in calls], [today.prepare_at, tomorrow.prepare_at])
        self.assertEqual(self.app.builder.built, [])
        self.assertEqual(self.player.played, [])
        self.assertFalse(self.app.state.is_fired(today.key, today.day))
        self.assertEqual(self.entries(), [])
        self.assertIn("休止の記録が変わりました。予定を選び直します。",
                      [record.getMessage() for record in captured.records])

    def test_the_change_baseline_is_taken_before_the_pause_is_read(self):
        # 変化の基準は、休止の記録を読む前に取る。読んだあとに取ると、その間の書き換えを見逃す。
        self.install_sleeper(lambda target, interrupt: self.app.stop_event.set() or False)
        order = []
        real_changed, real_pending = self.app.pause.changed, self.app._next_pending_event
        self.app.pause.changed = lambda: order.append("changed") or real_changed()
        self.app._next_pending_event = lambda: order.append("pending") or real_pending()
        self.app.run_forever()
        self.assertEqual(order[:2], ["changed", "pending"])

    def test_a_pause_lifted_within_the_grace_plays_the_just_missed_chime(self):
        self.moment = datetime(2026, 10, 14, 10, 0, 30, tzinfo=TOKYO)
        self.set_pause()
        missed, next_one = self.scheduled(), self.scheduled("hourly:11")

        def operator_lifts(target, interrupt):
            self.assertFalse(interrupt())
            self.app.pause.clear()           # 現地で --pause-off
            self.assertTrue(interrupt())
            return False

        def stop(target, interrupt):
            self.app.stop_event.set()
            return False

        # 1 回目: 翌日の準備を待つ（解除で中断）。2・3 回目: 取りこぼした 10 時の準備と再生の時刻
        # （どちらも過去）。4 回目: その次（11 時）の準備を待って、停止する。
        calls = self.install_sleeper(operator_lifts, lambda t, i: True, lambda t, i: True, stop)
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(missed.key, missed.day))
        self.assertEqual(calls[1]["target"], missed.prepare_at)
        self.assertEqual(calls[3]["target"], next_one.prepare_at)
        self.assertEqual(self.only_entry()["result"], "ok")

    def test_a_pause_that_starts_after_the_prepare_wait_is_caught_by_the_recheck(self):
        # 待機が（割り込みなしで）終わった直後に休止が書かれた。再確認が、別の予定を選び直す。
        today, tomorrow = self.scheduled(), self.scheduled(day=THURSDAY_DATE)

        def pause_then_wake(target, interrupt):
            self.set_pause()
            return True

        def stop(target, interrupt):
            self.app.stop_event.set()
            return False

        calls = self.install_sleeper(pause_then_wake, stop)
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.run_forever()
        self.assertEqual(self.app.builder.built, [])
        self.assertEqual(calls[1]["target"], tomorrow.prepare_at)
        self.assertFalse(self.app.state.is_fired(today.key, today.day))
        self.assertIn("待機中に予定が変わりました。再計算します。",
                      [record.getMessage() for record in captured.records])

    def test_startup_tells_that_a_pause_is_in_effect(self):
        self.set_pause(reason="点検")
        self.install_sleeper(lambda target, interrupt: self.app.stop_event.set() or False)
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.run_forever()
        messages = [record.getMessage() for record in captured.records]
        self.assertIn("休止の記録があります（2026-10-14〜2026-10-14。休止: 点検）。この間の放送は鳴らしません。",
                      messages)

    def test_startup_says_nothing_about_the_pause_when_there_is_none(self):
        self.install_sleeper(lambda target, interrupt: self.app.stop_event.set() or False)
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.run_forever()
        self.assertFalse([record for record in captured.records if "休止" in record.getMessage()])


class NoEventsWaitTest(PauseTestCase):
    """予定が無いときの待ち: 警告は途切れごとに 1 度、再確認は 1 時間おき。"""

    def no_events(self):
        self.guard = CallGuard()

        def none(is_fired=None):
            self.guard.tick("next_event")

        self.app.scheduler.next_event = none

    def stop_after(self, count):
        """``count`` 回目の待ちで停止を求める ``sleep_until`` の代わり。"""
        waits = []

        def sleeping(target, stop=None, precise=False, interrupt=None):
            self.guard.tick("sleep_until")
            waits.append((target, stop, interrupt))
            if len(waits) >= count:
                self.app.stop_event.set()
            return False

        self.app.scheduler.sleep_until = sleeping
        return waits

    def warnings(self, captured):
        return [record.getMessage() for record in captured.records
                if record.getMessage().startswith("予定されたイベントがありません")]

    def test_the_recheck_interval_is_an_hour(self):
        self.assertEqual(NO_EVENTS_RECHECK_SECONDS, 3600)
        self.assertEqual(RETRY_SECONDS, 60)   # 例外のあとのやり直しは、今までどおり

    def test_the_warning_is_logged_once_for_a_whole_stretch(self):
        self.no_events()
        waits = self.stop_after(5)
        with logs_enabled(), self.assertLogs("chime.app", level="WARNING") as captured:
            self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(len(waits), 5)
        [warning] = self.warnings(captured)
        self.assertEqual(warning, "予定されたイベントがありません。60 分ごとに再確認します"
                                  "（この警告は予定が見つかるまで繰り返しません）。")

    def test_it_waits_an_hour_at_a_time_and_can_be_woken_by_stop_or_pause(self):
        self.no_events()
        waits = self.stop_after(2)
        self.app.run_forever()
        for target, stop, interrupt in waits:
            self.assertEqual(target, WEDNESDAY + timedelta(seconds=3600))
            self.assertIs(stop, self.app.stop_event)
            self.assertEqual(interrupt, self.app.pause.changed)

    def test_the_warning_comes_back_after_an_event_has_been_found(self):
        self.guard = CallGuard()
        event = self.scheduled()
        answers = [None, None, event, None, None]

        def next_event(is_fired=None):
            self.guard.tick("next_event")
            return answers.pop(0) if answers else None

        self.app.scheduler.next_event = next_event
        # 見つかったイベントの準備の待ち（3 回目の選択）は、中断して選び直す。
        sleeps = []

        def sleeping(target, stop=None, precise=False, interrupt=None):
            self.guard.tick("sleep_until")
            sleeps.append(target)
            if len(sleeps) >= 6:
                self.app.stop_event.set()
            return False

        self.app.scheduler.sleep_until = sleeping
        with logs_enabled(), self.assertLogs("chime.app", level="WARNING") as captured:
            self.app.run_forever()
        self.assertEqual(len(self.warnings(captured)), 2)

    def test_a_change_of_the_pause_record_wakes_the_wait(self):
        # 本物の sleep_until の上で。待ち始めた時点の基準と比べ、書き換わっていたらすぐ戻る。
        self.app.pause.changed()
        self.set_pause()
        with mock.patch.object(self.app.stop_event, "wait",
                               side_effect=AssertionError("待ってはいけない")):
            self.app._wait_without_events()

    def test_a_stop_request_ends_the_wait(self):
        self.app.stop_event.set()
        with mock.patch.object(self.app.stop_event, "wait",
                               side_effect=AssertionError("待ってはいけない")):
            self.app._wait_without_events()

    def test_the_loop_stays_quiet_in_the_log_apart_from_the_one_warning(self):
        self.no_events()
        self.stop_after(4)
        with logs_enabled(), self.assertLogs("chime.app", level="WARNING") as captured:
            self.app.run_forever()
        warnings = [record for record in captured.records if record.levelno >= 30]
        self.assertEqual(len([r for r in warnings if "予定されたイベントがありません" in r.getMessage()]), 1)


class FailFastGuardTest(PauseTestCase):
    """退行しても、休止まわりのテストは固まらずに失敗する（``PauseTestCase`` の guard が効いている）。"""

    def test_a_real_wait_on_the_frozen_clock_is_cut_off(self):
        # 休止を見逃す退行（_skip_if_paused が常に False）を作る。本物の待機に入るが、時計が止まって
        # いるので終わらない。数えるだけの stop_event.wait が、上限で RunawayLoop にする。
        self.set_pause()
        self.app._skip_if_paused = lambda event: False
        with self.assertRaises(RunawayLoop):
            self.app.run_event(self.scheduled())
        self.assertEqual(self.guard.counts["stop_event.wait"], CALL_LIMIT + 1)
        self.assertEqual(self.player.played, [])

    def test_a_fake_sleeper_that_keeps_saying_yes_is_cut_off(self):
        # 休止を飛ばせない退行（選んだイベントがいつまでも残る）を作る。偽の待機は尽きたあとも
        # すぐ True を返すので、ループは自分では止まらない。
        event = self.scheduled()
        self.app.scheduler.next_event = lambda is_fired=None: event
        calls = self.install_sleeper()
        with self.assertRaises(RunawayLoop):
            self.app.run_forever()
        self.assertEqual(len(calls), CALL_LIMIT)

    def test_the_real_wait_is_replaced_in_every_pause_test(self):
        self.assertEqual(self.app.stop_event.wait(30), False)
        self.app.stop_event.set()
        self.assertEqual(self.app.stop_event.wait(30), True)
        self.assertEqual(self.guard.counts["stop_event.wait"], 2)

    def test_a_missed_pause_in_the_wait_fails_instead_of_hanging(self):
        # 待機が割り込みの結果を無視する退行（本物の sleep_until で、interrupt が True でも続ける）。
        self.set_pause()
        event = self.scheduled()
        real = self.app.scheduler.sleep_until

        def ignoring_the_interrupt(target, stop=None, precise=False, interrupt=None):
            return real(target, stop, precise, interrupt=None)

        self.app.scheduler.sleep_until = ignoring_the_interrupt
        self.app._skip_if_paused = lambda e: False
        with self.assertRaises(RunawayLoop):
            self.app._wait_for_play_time(event)


class PauseCoversByAtDateKiller(PauseTestCase):
    """休止は、イベントの **鳴る予定の日**（``Event.at`` の日付）で照らす（再生を始める日ではない）。

    ``hourly.start_hour`` が 0 の設定では、0 時の時報は前日の 23:59:57 に鳴り始める。それは
    「次の日の放送」として扱う。今日だけの休止では止まらず、翌日までの休止なら止まる。
    既定の設定（時報は 10〜16 時）では起きない。
    """

    def setUp(self):
        super().setUp()
        settings = dict(DEFAULT_CONFIG["schedule"],
                        hourly=dict(DEFAULT_CONFIG["schedule"]["hourly"], start_hour=0, end_hour=0,
                                    skip_hours=[], weekdays=[0, 1, 2, 3, 4, 5, 6]),
                        closing=dict(DEFAULT_CONFIG["schedule"]["closing"], enabled=False))
        self.app.scheduler = Scheduler(settings, self.app.tzinfo, 3.0, clock=self.app.now)
        self.moment = datetime(2026, 10, 14, 23, 30, tzinfo=TOKYO)
        self.midnight = self.scheduled("hourly:00", date(2026, 10, 15))

    def test_the_midnight_chime_starts_playing_on_the_previous_day(self):
        self.assertEqual(self.midnight.at, datetime(2026, 10, 15, 0, 0, tzinfo=TOKYO))
        self.assertEqual(self.midnight.play_at, datetime(2026, 10, 14, 23, 59, 57, tzinfo=TOKYO))
        self.assertEqual(self.midnight.day, "2026-10-15")

    def test_A1_pause_for_today_does_not_cover_a_midnight_chime_whose_at_date_is_tomorrow(self):
        self.set_pause(date(2026, 10, 14))
        self.assertIsNone(self.app._pause_covering(self.midnight))
        self.assertFalse(self.app._paused_for(self.midnight)())

    def test_a_pause_through_tomorrow_covers_it(self):
        pause = self.set_pause(date(2026, 10, 15))
        self.assertEqual(self.app._pause_covering(self.midnight), pause)
        self.assertTrue(self.app._paused_for(self.midnight)())

    def test_selection_uses_the_same_day_as_the_check(self):
        self.set_pause(date(2026, 10, 14))
        self.assertEqual(self.app._next_pending_event().key, "hourly:00")
        self.assertEqual(self.app._next_pending_event().day, "2026-10-15")
        self.set_pause(date(2026, 10, 15))
        self.assertEqual(self.app._next_pending_event().day, "2026-10-16")

    def test_a_midnight_chime_is_listed_at_startup_after_a_pause_for_the_day_before(self):
        # 起動時の「次回以降の予定」も、止める日は選ぶときと同じ（Event.at の日付）で決める。0 時の時報（再生開始は
        # 前日の 23:59:57）は、前の日だけの休止では止まらないので、並べる。再生開始の日で決めると、並べ損なう。
        self.set_pause(date(2026, 10, 14))
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app._log_upcoming()
        [message] = [record.getMessage() for record in captured.records]
        self.assertIn("  - 時報 2026-10-15 00:00:00（再生開始 23:59:57）", message)
        self.assertEqual(self.app._next_pending_event().day, "2026-10-15")     # 選ぶときも同じ

    def test_the_midnight_chime_plays_after_a_pause_for_today_only(self):
        self.set_pause(date(2026, 10, 14))
        self.install_sleeper()
        self.app.run_event(self.midnight)
        self.assertEqual(self.player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(self.midnight.key, self.midnight.day))
        self.assertEqual(self.only_entry()["result"], "ok")

    def test_the_midnight_chime_is_skipped_after_a_pause_through_tomorrow(self):
        self.set_pause(date(2026, 10, 15), "年末の点検")
        self.install_sleeper()
        self.app.run_event(self.midnight)
        self.assertEqual(self.player.played, [])
        entry = self.skipped_entry()
        self.assertEqual((entry["day"], entry["reason"]), ("2026-10-15", "休止: 年末の点検"))
        self.assertFalse(self.app.state.is_fired(self.midnight.key, self.midnight.day))


class StartupUpcomingTest(PauseTestCase):
    """起動時の「次回以降の予定」: 休止中は、止めている日の予定を鳴るように並べない。"""

    def logged(self):
        """``_log_upcoming`` が残した行（INFO 以上の ``chime.app``）。"""
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app._log_upcoming()
        return [record.getMessage() for record in captured.records]

    def test_without_a_pause_the_list_is_the_next_five_as_before(self):
        [message] = self.logged()
        expected = format_events(self.app.scheduler.upcoming(limit=5))
        self.assertEqual(message, "次回以降の予定:\n" + expected)
        self.assertIn("2026-10-14 10:00:00", message)

    def test_the_paused_days_events_are_not_listed_as_upcoming(self):
        self.set_pause(date(2026, 10, 14), "試験")
        [message] = self.logged()
        self.assertNotIn("2026-10-14 10:00:00", message)
        self.assertNotIn("2026-10-14", message)
        self.assertTrue(message.startswith("次回以降の予定（休止のあいだは省いています）:\n"), message)
        self.assertIn("  - 時報 2026-10-15 10:00:00（再生開始 09:59:57）", message)

    def test_the_list_starts_after_the_pause_like_the_schedule_command(self):
        self.set_pause(date(2026, 10, 16))     # 金曜まで。土日は鳴らない
        [message] = self.logged()
        lines = message.splitlines()[1:]
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("  - 時報 2026-10-19 10:00:00"), lines)
        self.assertEqual(lines, format_events(
            self.app.scheduler.upcoming(datetime(2026, 10, 17, 0, 0, tzinfo=TOKYO), 5)).splitlines())

    def test_a_pause_that_starts_later_leaves_the_days_before_it_listed(self):
        self.moment = datetime(2026, 10, 14, 14, 30, tzinfo=TOKYO)
        self.write_pause(date(2026, 10, 15), date(2026, 10, 15))
        [message] = self.logged()
        self.assertIn("2026-10-14 15:00:00", message)
        self.assertIn("2026-10-14 16:57:00", message)
        self.assertNotIn("2026-10-15", message)
        self.assertIn("2026-10-16 11:00:00", message)    # 5 件目まで数えなおして、休止の日を飛ばす

    def test_an_empty_list_after_a_pause_says_so(self):
        settings = dict(DEFAULT_CONFIG["schedule"],
                        hourly=dict(DEFAULT_CONFIG["schedule"]["hourly"], enabled=False),
                        closing=dict(DEFAULT_CONFIG["schedule"]["closing"], enabled=False))
        self.app.scheduler = Scheduler(settings, self.app.tzinfo, 3.0, clock=self.app.now)
        self.set_pause()
        [message] = self.logged()
        self.assertEqual(message, "次回以降の予定（休止のあいだは省いています）:\n"
                                  + format_events([]))

    def test_a_broken_pause_record_lists_everything_as_before(self):
        os.makedirs(os.path.dirname(self.app.pause.path))
        with open(self.app.pause.path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured, \
                self.assertLogs("chime.pause", level="WARNING"):
            self.app._log_upcoming()
        [message] = [record.getMessage() for record in captured.records]
        self.assertIn("2026-10-14 10:00:00", message)
        self.assertTrue(message.startswith("次回以降の予定:\n"))

    def test_a_failing_pause_store_lists_everything_as_before(self):
        with mock.patch.object(self.app.pause, "current", side_effect=RuntimeError("壊れた")), \
                logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app._log_upcoming()
        messages = [record.getMessage() for record in captured.records]
        listing = [message for message in messages if message.startswith("次回以降の予定")]
        self.assertEqual(len(listing), 1)
        self.assertIn("2026-10-14 10:00:00", listing[0])

    def test_failing_to_build_the_list_does_not_stop_startup(self):
        self.set_pause()
        with mock.patch.object(self.app.scheduler, "iter_events", side_effect=RuntimeError("一覧を作れない")), \
                logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
            self.app._log_upcoming()
        self.assertEqual(captured.records[0].getMessage(),
                         "次回以降の予定を求められませんでした（継続します）: 一覧を作れない")

    def test_startup_says_the_pause_and_then_a_list_that_agrees_with_it(self):
        self.set_pause(reason="点検")
        self.install_sleeper(lambda target, interrupt: self.app.stop_event.set() or False)
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.run_forever()
        messages = [record.getMessage() for record in captured.records]
        said = [i for i, m in enumerate(messages) if m.startswith("休止の記録があります")]
        listed = [i for i, m in enumerate(messages) if m.startswith("次回以降の予定")]
        self.assertEqual((len(said), len(listed)), (1, 1))
        self.assertLess(said[0], listed[0])
        self.assertIn("休止のあいだは省いています", messages[listed[0]])
        self.assertNotIn("2026-10-14", messages[listed[0]])


class InterruptedPlaybackTest(HistoryTestCase):
    """停止要求（``stop_event``）で再生を打ち切ったとき。"""

    def two_segments(self):
        return [Segment(self.wav, label="一つ目"), Segment(self.wav, label="二つ目")]

    def interrupting_player(self, played=1):
        """``Player.play`` が ``PlaybackInterrupted`` を出す代用品。"""
        player = mock.Mock(name="player")
        player.name = "interrupting"
        player.play.side_effect = PlaybackInterrupted("止めた", played=played)
        return player

    def test_the_stop_event_is_handed_to_the_player(self):
        self.app._player = mock.Mock()
        self.app._player.play.return_value = 1
        plan = PlaybackPlan(event=None, segments=self.two_segments())
        self.app.play_with_result(plan)
        self.app._player.play.assert_called_once_with(plan.segments, stop=self.app.stop_event)

    def test_the_outcome_of_an_interrupted_playback(self):
        self.app._player = self.interrupting_player(played=1)
        outcome = self.app.play_with_result(PlaybackPlan(event=None, segments=self.two_segments()))
        self.assertEqual(outcome, PlayOutcome(ok=False, played=1, total=2, error="停止要求で中断",
                                              interrupted=True))
        self.assertEqual(INTERRUPTED_TEXT, "停止要求で中断")

    def test_play_returns_false_for_an_interrupted_playback(self):
        self.app._player = self.interrupting_player()
        self.assertFalse(self.app.play(PlaybackPlan(event=None, segments=self.two_segments())))

    def test_it_is_logged_as_a_warning_not_an_error(self):
        self.app._player = self.interrupting_player(played=1)
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.app.play_with_result(PlaybackPlan(event=None, segments=self.two_segments()))
        records = [r for r in captured.records if "中断" in r.getMessage()]
        self.assertEqual([(r.levelname, r.getMessage()) for r in records],
                         [("WARNING", "停止要求のため再生を中断しました（1/2 件を鳴らしました）")])
        self.assertFalse([r for r in captured.records if r.levelname == "ERROR"])

    def test_an_ordinary_failure_is_not_marked_interrupted(self):
        outcome = self.app.play_with_result(
            PlaybackPlan(event=None, segments=[Segment("/nonexistent.wav", label="欠落")]))
        self.assertFalse(outcome.interrupted)
        self.assertFalse(PlayOutcome(ok=True, played=1, total=1).interrupted)

    def test_a_stop_during_the_playback_is_an_interrupted_entry_and_the_event_is_marked_fired(self):
        class StoppingPlayer(RecordingPlayer):
            def __init__(self, app):
                super().__init__()
                self.app = app

            def play_one(self, segment):
                super().play_one(segment)
                self.app.stop_event.set()   # 1 つ鳴らしたところで、サービスの停止が求められた

        self.app._player = StoppingPlayer(self.app)
        self.app.builder = StubBuilder(self.two_segments(), quote="ひとこと")
        event = self.past_event()
        self.app.run_event(event)
        self.assertEqual(self.player.played, [])           # 差し替えたので、記録は新しい方
        self.assertEqual(self.app._player.played, [self.wav])
        self.assertTrue(self.app.state.is_fired(event.key, event.day))
        entry = self.only_entry()
        self.assertEqual(entry["result"], "interrupted")
        self.assertEqual((entry["played"], entry["total"]), (1, 2))
        self.assertNotIn("error", entry)
        self.assertEqual(self.app.state.recent_quotes(), [])  # 最後まで鳴らせなかったひとことは使ったことにしない

    def test_an_interrupted_event_is_not_played_again_after_the_restart(self):
        self.app._player = self.interrupting_player(played=1)
        event = self.past_event()
        self.app.run_event(event)
        self.assertTrue(self.app._is_fired(event))

    def test_a_stop_in_the_middle_ends_the_loop_with_exit_code_0(self):
        class StoppingPlayer(RecordingPlayer):
            def play_one(inner, segment):
                super().play_one(segment)
                self.app.stop_event.set()

        self.app._player = StoppingPlayer()
        self.app.builder = StubBuilder(self.two_segments())
        event = self.past_event()
        guard = CallGuard()

        def fake_next(is_fired=None):
            guard.tick("next_event")      # 再生済みにならない退行で、止まらずに回り続けないように
            return None if is_fired(event) else event

        self.app.scheduler.next_event = fake_next
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(self.only_entry()["result"], "interrupted")

    def test_the_interrupted_entry_is_written_even_if_the_history_is_unwritable(self):
        self.app._player = self.interrupting_player()
        os.makedirs(self.history_path)   # ファイルの場所がフォルダ: 追記できない
        event = self.past_event()
        with logs_enabled(), self.assertLogs(level="WARNING") as captured:
            self.app.run_event(event)
        self.assertTrue(self.app._is_fired(event))
        self.assertTrue(any("放送の履歴を書けません" in record.getMessage() for record in captured.records))

    def test_a_dry_run_leaves_no_interrupted_entry(self):
        self.app.dry_run = True
        self.app._player = self.interrupting_player()
        self.app.run_event(self.past_event())
        self.assertFalse(os.path.exists(self.history_path))
        self.app._player.play.assert_not_called()


class StopBeforeAnySoundTest(PauseTestCase):
    """停止要求が、音を出す前に届いたとき（``PlayOutcome.started`` が偽）。

    何も鳴っていないので、再生済みにも履歴にも残さない。再起動したあと、追いかけ再生の猶予
    （``catchup_grace_seconds``）のうちは、同じ放送を鳴らし直せる。音が出始めたあとの中断は、
    今までどおり再生済みにして ``interrupted`` を残す（鳴らし直すと、頭から二重に鳴ってしまう）。
    """

    def early_stop(self, played=0):
        """再生側（``chime/audio.py``）が、デバイスを開く前の停止で出す例外と同じ印を持つ例外。"""
        error = PlaybackInterrupted("再生の開始前", played=played)
        error.started = False       # 再生側が付ける印。ここでは直接付けて、再生側の実装に頼らない
        return error

    def late_stop(self, played=1):
        error = PlaybackInterrupted("再生の途中", played=played)
        error.started = True
        return error

    def player_raising(self, error):
        player = mock.Mock(name="player")
        player.name = "stopping"
        player.play.side_effect = error
        return player

    def run_with(self, error, quote=None):
        """``error`` で再生が終わる放送を 1 回、ふつうに流す。``(イベント, プレーヤー)`` を返す。"""
        player = self.player_raising(error)
        self.app._player = player
        self.app.builder = StubBuilder([Segment(self.wav, label="一つ目"), Segment(self.wav, label="二つ目")],
                                       quote=quote)
        event = self.scheduled()
        self.install_sleeper()
        self.app.run_event(event)
        return event, player

    # -- 結果の印 ---------------------------------------------------------
    def test_started_is_true_by_default_and_does_not_change_the_comparison(self):
        self.assertTrue(PlayOutcome(ok=True, played=1, total=1).started)
        self.assertEqual(PlayOutcome(ok=False, played=1, total=2, error="停止要求で中断", interrupted=True),
                         PlayOutcome(ok=False, played=1, total=2, error="停止要求で中断", interrupted=True,
                                     started=True))

    def test_the_outcome_says_that_nothing_had_started(self):
        self.app._player = self.player_raising(self.early_stop())
        plan = PlaybackPlan(event=None, segments=[Segment(self.wav), Segment(self.wav)])
        self.assertEqual(self.app.play_with_result(plan),
                         PlayOutcome(ok=False, played=0, total=2, error=INTERRUPTED_TEXT,
                                     interrupted=True, started=False))

    def test_an_interruption_after_the_sound_started_says_started(self):
        self.app._player = self.player_raising(self.late_stop(played=1))
        plan = PlaybackPlan(event=None, segments=[Segment(self.wav), Segment(self.wav)])
        outcome = self.app.play_with_result(plan)
        self.assertEqual((outcome.interrupted, outcome.started, outcome.played), (True, True, 1))

    def test_a_player_that_gives_no_mark_counts_as_started(self):
        # 印を付けない再生側（古い実装・別の実装）でも、音は出たものとして扱う（二重に鳴らさない側に倒す）。
        error = PlaybackInterrupted("止めた", played=1)
        error.__dict__.pop("started", None)
        self.app._player = self.player_raising(error)
        outcome = self.app.play_with_result(PlaybackPlan(event=None, segments=[Segment(self.wav)]))
        self.assertEqual((outcome.interrupted, outcome.started), (True, True))

    def test_only_an_interruption_can_be_not_started(self):
        for player_error in (PlaybackError("失敗"), RuntimeError("想定外")):
            with self.subTest(error=player_error), logs_enabled(), self.assertLogs("chime.app"):
                self.app._player = self.player_raising(player_error)
                outcome = self.app.play_with_result(PlaybackPlan(event=None, segments=[Segment(self.wav)]))
                self.assertFalse(outcome.interrupted)
                self.assertTrue(outcome.started)

    # -- run_event --------------------------------------------------------
    def test_a_stop_before_any_sound_marks_nothing_and_writes_no_history(self):
        event, player = self.run_with(self.early_stop(), quote="ひとこと")
        player.play.assert_called_once()
        self.assertFalse(self.app.state.is_fired(event.key, event.day))
        self.assertFalse(os.path.exists(self.history_path))
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.app.state.recent_quotes(), [])

    def test_it_is_logged_for_the_operator(self):
        with logs_enabled(), self.assertLogs("chime.app", level="INFO") as captured:
            self.run_with(self.early_stop())
        records = [(record.levelname, record.getMessage()) for record in captured.records]
        self.assertIn(("WARNING", "停止要求のため再生を中断しました（0/2 件を鳴らしました）"), records)
        [note] = [message for level, message in records
                  if message.startswith("音を出す前に停止要求を受けたため、再生済みにしません")]
        self.assertIn("追いかけて鳴らします", note)
        self.assertIn("2026-10-14 10:00:00", note)
        self.assertFalse([level for level, _ in records if level == "ERROR"])

    def test_the_event_plays_after_a_restart_within_the_grace(self):
        event, _ = self.run_with(self.early_stop())
        self.moment = event.play_at + timedelta(seconds=15)       # 再起動して戻ってきた
        restarted = ChimeApp(Config(DEFAULT_CONFIG, base_dir=self.root), backend="mock")
        restarted.now = lambda: self.moment
        restarted.scheduler = Scheduler(DEFAULT_CONFIG["schedule"], restarted.tzinfo, 3.0,
                                        clock=restarted.now)
        with logs_enabled(), self.assertLogs("chime.scheduler", level="WARNING"):
            again = restarted._next_pending_event()
        self.assertEqual((again.key, again.day), (event.key, event.day))

    def test_a_stop_after_the_sound_started_is_marked_fired_and_not_replayed(self):
        event, _ = self.run_with(self.late_stop(played=1))
        self.assertTrue(self.app.state.is_fired(event.key, event.day))
        entry = self.only_entry()
        self.assertEqual((entry["result"], entry["played"], entry["total"]), ("interrupted", 1, 2))
        self.moment = event.play_at + timedelta(seconds=15)
        self.assertEqual(self.app._next_pending_event().key, "hourly:11")

    def test_a_stop_during_the_very_first_segment_still_counts_as_started(self):
        # 最初の部品が鳴り出したあとの停止は、まだ 1 件も鳴り終えていなくても「鳴り始めた」。
        event, _ = self.run_with(self.late_stop(played=0))
        self.assertTrue(self.app.state.is_fired(event.key, event.day))
        entry = self.only_entry()
        self.assertEqual((entry["result"], entry["played"]), ("interrupted", 0))

    def test_a_dry_run_never_plays_so_it_never_has_this_case(self):
        self.app.dry_run = True
        self.app._player = self.player_raising(self.early_stop())
        self.install_sleeper()
        self.app.run_event(self.scheduled())
        self.app._player.play.assert_not_called()

    def test_the_loop_ends_cleanly_when_the_stop_comes_before_any_sound(self):
        event = self.scheduled()

        def stop_then_refuse(segments, stop=None):
            self.app.stop_event.set()           # サービスの停止（SIGTERM）が、再生の入り口で届いた
            raise self.early_stop()

        self.app._player = mock.Mock(name="player")
        self.app._player.name = "stopping"
        self.app._player.play.side_effect = stop_then_refuse
        self.install_sleeper()
        with logs_enabled(), self.assertLogs("chime.app", level="INFO"):
            self.assertEqual(self.app.run_forever(), 0)
        self.app._player.play.assert_called_once()
        self.assertFalse(self.app.state.is_fired(event.key, event.day))
        self.assertEqual(self.entries(), [])

    def test_the_stop_flag_of_the_real_player_reaches_the_app(self):
        # 本物の Player.play を通して（偽の例外ではなく）、開く前の停止要求が started=False になるか。
        # 再生側（chime/audio.py）がまだ印を付けない版なら、ここは確かめられないので飛ばす。
        probe = threading.Event()
        probe.set()
        try:
            RecordingPlayer().play([Segment(self.wav)], stop=probe)
        except PlaybackInterrupted as exc:
            if getattr(exc, "started", None) is not False:
                self.skipTest("再生側が、音を出す前の中断に started=False を付けていない")
        else:
            self.fail("停止要求が立っていても、再生が始まってしまった")

        recording = RecordingPlayer()
        self.app._player = recording
        event = self.scheduled()
        self.install_sleeper(lambda target, interrupt: self.app.stop_event.set() or True)   # 待ち終えた直後に SIGTERM
        with logs_enabled(), self.assertLogs("chime.app", level="INFO"):
            self.app.run_event(event)
        self.assertEqual((recording.played, recording.opened), ([], 0))     # 何も聞こえていない
        self.assertFalse(self.app.state.is_fired(event.key, event.day),
                         "何も鳴っていないのに再生済みにすると、再起動しても鳴らし直されない")
        self.assertEqual(self.entries(), [])


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
