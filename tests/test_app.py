"""常駐ループ（ChimeApp）のテスト。"""

from __future__ import annotations

import os
import tempfile
import unittest
import wave
from datetime import date, datetime, timedelta
from unittest import mock

from chime.app import ChimeApp
from chime.audio import Player
from chime.config import DEFAULT_CONFIG, Config
from chime.scheduler import Event
from chime.sequence import PlaybackPlan
from chime.audio import Segment


class RecordingPlayer(Player):
    name = "recording"

    def __init__(self, settings=None):
        super().__init__(settings or {})
        self.played = []

    def play_one(self, segment):
        self.played.append(segment.path)


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


def make_wav(path: str) -> str:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 80)
    return path


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "assets"), exist_ok=True)
        self.wav = make_wav(os.path.join(self.root, "assets", "beep.wav"))

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
        return Event(key="hourly:10", kind="hourly", hour=10, minute=0,
                     at=moment, play_at=moment, prepare_at=moment)


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
        event = Event(key="hourly:10", kind="hourly", hour=10, minute=0,
                      at=moment, play_at=moment, prepare_at=moment)
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
        import logging

        self.app.builder = StubBuilder([], explode=True)
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("chime.app", level="ERROR") as captured:
                self.app.run_event(self.past_event())
        finally:
            logging.disable(logging.CRITICAL)
        self.assertTrue(any("組み立て失敗" in line for line in captured.output))

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
        event = Event(key="hourly:10", kind="hourly", hour=10, minute=0,
                      at=moment, play_at=moment, prepare_at=moment)
        self.app.builder = StubBuilder([Segment(self.wav, label="テスト音")],
                                       quote="テストのひとこと")
        self.app.stop_event.set()
        self.app.run_event(event)
        self.assertEqual(self.app.state.recent_quotes(), [])
        self.assertFalse(self.app.state.is_fired(event.key, event.day))


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

    def test_stop_before_start(self):
        self.app.stop_event.set()
        self.app.scheduler.next_event = lambda is_fired=None: self.past_event()
        self.assertEqual(self.app.run_forever(), 0)
        self.assertEqual(self.player.played, [])


class TimezoneTest(unittest.TestCase):
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
            wav = make_wav(os.path.join(tmp, "beep.wav"))
            app.builder = StubBuilder([Segment(wav, label="テスト音")], quote="テストのひとこと")
            moment = app.now() - timedelta(seconds=5)
            app.run_event(Event(key="hourly:10", kind="hourly", hour=10, minute=0,
                                at=moment, play_at=moment, prepare_at=moment))
            self.assertFalse(os.path.exists(state_file))

    def test_a_normal_app_does_write_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, state_file = self.make_app(tmp, dry_run=False)
            app.state.mark_fired("hourly:10", "2026-08-26")
            self.assertTrue(os.path.exists(state_file))


if __name__ == "__main__":
    unittest.main()
