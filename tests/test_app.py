"""常駐ループ（ChimeApp）のテスト。"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from tests.support import RecordingPlayer, block_network, logs_enabled, make_event, make_wav

from chime.app import ChimeApp
from chime.audio import Player
from chime.config import DEFAULT_CONFIG, Config
from chime.scheduler import Event, Scheduler
from chime.sequence import PlaybackPlan
from chime.audio import Segment
from chime.state import State


class StubBuilder:
    """``SequenceBuilder`` のスタブ。

    ``explode`` なら ``build`` が例外を出す。そのとき ``run_event`` は
    ``build_minimal`` の結果（``minimal_segments``）で鳴らす。``quote`` は
    プランに載せる「選んだひとこと」。
    """

    def __init__(self, segments, explode=False, quote=None, minimal_segments=None,
                 explode_minimal=False):
        self.segments = segments
        self.explode = explode
        self.quote = quote
        self.minimal_segments = minimal_segments if minimal_segments is not None else []
        self.explode_minimal = explode_minimal
        self.built = []
        self.built_minimal = []

    def build(self, event):
        self.built.append(event)
        if self.explode:
            raise RuntimeError("組み立て失敗")
        return PlaybackPlan(event=event, segments=list(self.segments), quote=self.quote)

    def build_minimal(self, event):
        self.built_minimal.append(event)
        if self.explode_minimal:
            raise RuntimeError("最小プランも組み立て失敗")
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
            with logs_enabled(), self.assertLogs("chime.app", level="ERROR") as captured:
                ChimeApp(config, backend="mock")
        self.assertEqual(len(captured.records), 1)
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
