"""再生バックエンドのテスト。"""

from __future__ import annotations

import os
import tempfile
import unittest
import wave
from unittest import mock

from chime import audio
from chime.audio import (CommandPlayer, MockPlayer, PlaybackError, Player, PygamePlayer,
                         Segment, create_player, wav_duration)
from chime.config import DEFAULT_CONFIG

AUDIO = dict(DEFAULT_CONFIG["audio"], gap_ms=0, mock_max_seconds=0.01)


def make_wav(path: str, seconds: float = 0.05) -> str:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


class RecordingPlayer(Player):
    name = "recording"

    def __init__(self, settings):
        super().__init__(settings)
        self.played = []
        self.opened = 0
        self.closed = 0

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def play_one(self, segment):
        self.played.append(segment.path)


class WavDurationTest(unittest.TestCase):
    def test_reads_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_wav(os.path.join(tmp, "a.wav"), 0.25)
            self.assertAlmostEqual(wav_duration(path), 0.25, places=3)

    def test_returns_none_for_non_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mp3")
            with open(path, "wb") as handle:
                handle.write(b"not a wav")
            self.assertIsNone(wav_duration(path))


class PlaySequenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.first = make_wav(os.path.join(self.tmp.name, "1.wav"))
        self.second = make_wav(os.path.join(self.tmp.name, "2.wav"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_plays_in_order(self):
        player = RecordingPlayer(AUDIO)
        player.play([Segment(self.first), Segment(self.second)])
        self.assertEqual(player.played, [self.first, self.second])

    def test_opens_and_closes_once(self):
        player = RecordingPlayer(AUDIO)
        player.play([Segment(self.first), Segment(self.second)])
        self.assertEqual((player.opened, player.closed), (1, 1))

    def test_device_is_released_even_on_error(self):
        class Failing(RecordingPlayer):
            def play_one(self, segment):
                raise PlaybackError("再生失敗")

        player = Failing(AUDIO)
        with self.assertRaises(PlaybackError):
            player.play([Segment(self.first)])
        self.assertEqual(player.closed, 1)

    def test_missing_required_file_raises(self):
        player = RecordingPlayer(AUDIO)
        with self.assertRaises(PlaybackError):
            player.play([Segment("/nonexistent.wav")])

    def test_missing_optional_file_is_skipped(self):
        player = RecordingPlayer(AUDIO)
        player.play([Segment("/nonexistent.wav", optional=True), Segment(self.first)])
        self.assertEqual(player.played, [self.first])

    def test_nothing_to_play_is_not_an_error(self):
        player = RecordingPlayer(AUDIO)
        player.play([])
        self.assertEqual(player.opened, 0)

    def test_empty_path_is_ignored(self):
        player = RecordingPlayer(AUDIO)
        player.play([Segment(""), Segment(self.first)])
        self.assertEqual(player.played, [self.first])


class MockPlayerTest(unittest.TestCase):
    def test_plays_without_sound(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_wav(os.path.join(tmp, "a.wav"))
            player = MockPlayer(AUDIO)
            player.play([Segment(path, label="テスト")])


class CommandPlayerTest(unittest.TestCase):
    def setUp(self):
        self.player = CommandPlayer(AUDIO)

    def test_maps_extension_to_command(self):
        self.assertEqual(self.player.command_for("/tmp/a.wav"), ["aplay", "-q", "/tmp/a.wav"])
        self.assertEqual(self.player.command_for("/tmp/a.mp3"), ["mpg123", "-q", "/tmp/a.mp3"])

    def test_extension_is_case_insensitive(self):
        self.assertIsNotNone(self.player.command_for("/tmp/A.WAV"))

    def test_unknown_extension(self):
        self.assertIsNone(self.player.command_for("/tmp/a.ogg"))

    def test_unknown_extension_raises_on_play(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.ogg")
            with open(path, "wb") as handle:
                handle.write(b"x")
            with self.assertRaises(PlaybackError):
                self.player.play_one(Segment(path))

    def test_missing_command_raises(self):
        player = CommandPlayer(dict(AUDIO, commands={".wav": ["definitely-not-a-command", "{path}"]}))
        with self.assertRaises(PlaybackError):
            player.play_one(Segment("/tmp/a.wav"))

    def test_non_zero_exit_raises(self):
        with mock.patch("chime.audio.env.has_command", return_value=True), \
                mock.patch("chime.audio.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1)
            with self.assertRaises(PlaybackError):
                self.player.play_one(Segment("/tmp/a.wav"))


class CreatePlayerTest(unittest.TestCase):
    def test_explicit_backend_wins(self):
        self.assertIsInstance(create_player(AUDIO, "mock"), MockPlayer)
        self.assertIsInstance(create_player(AUDIO, "command"), CommandPlayer)
        self.assertIsInstance(create_player(AUDIO, "pygame"), PygamePlayer)

    def test_development_environment_uses_mock(self):
        with mock.patch("chime.audio.env.is_production_linux", return_value=False):
            self.assertIsInstance(create_player(AUDIO), MockPlayer)

    def test_production_prefers_pygame(self):
        with mock.patch("chime.audio.env.is_production_linux", return_value=True), \
                mock.patch.object(PygamePlayer, "available", staticmethod(lambda: True)):
            self.assertIsInstance(create_player(AUDIO), PygamePlayer)

    def test_production_falls_back_to_commands(self):
        with mock.patch("chime.audio.env.is_production_linux", return_value=True), \
                mock.patch.object(PygamePlayer, "available", staticmethod(lambda: False)), \
                mock.patch("chime.audio.env.has_command", return_value=True):
            self.assertIsInstance(create_player(AUDIO), CommandPlayer)

    def test_last_resort_is_mock(self):
        with mock.patch("chime.audio.env.is_production_linux", return_value=True), \
                mock.patch.object(PygamePlayer, "available", staticmethod(lambda: False)), \
                mock.patch("chime.audio.env.has_command", return_value=False):
            self.assertIsInstance(create_player(AUDIO), MockPlayer)

    def test_unknown_backend_is_treated_as_auto(self):
        with mock.patch("chime.audio.env.is_production_linux", return_value=False):
            self.assertIsInstance(create_player(AUDIO, "quantum"), MockPlayer)


class PygameAvailabilityTest(unittest.TestCase):
    def test_available_reflects_import(self):
        self.assertEqual(PygamePlayer.available(), audio.pygame is not None)


if __name__ == "__main__":
    unittest.main()
