"""常駐ループ（ChimeApp）のテスト。"""

from __future__ import annotations

import os
import tempfile
import unittest
import wave
from datetime import date, datetime, timedelta

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
    def __init__(self, segments, explode=False):
        self.segments = segments
        self.explode = explode
        self.built = []

    def build(self, event):
        self.built.append(event)
        if self.explode:
            raise RuntimeError("組み立て失敗")
        return PlaybackPlan(event=event, segments=list(self.segments))


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

    def test_build_failure_does_not_stop_the_loop(self):
        """組み立てに失敗しても、その回を飛ばして常駐を続ける。"""
        self.app.builder = StubBuilder([], explode=True)
        event = self.past_event()
        self.assertEqual(self._drive(event), 0)
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


if __name__ == "__main__":
    unittest.main()
