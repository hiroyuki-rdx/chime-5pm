"""再生バックエンドのテスト。"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
import wave
from unittest import mock

from tests.support import RecordingPlayer, logs_enabled, make_wav

from chime import audio
from chime.audio import (CommandPlayer, MockPlayer, PlaybackError, Player, PygamePlayer,
                         Segment, create_player, wav_duration)
from chime.config import DEFAULT_CONFIG

AUDIO = dict(DEFAULT_CONFIG["audio"], gap_ms=0, mock_max_seconds=0.01)


class WavDurationTest(unittest.TestCase):
    def test_reads_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_wav(os.path.join(tmp, "a.wav"), 0.25)
            self.assertAlmostEqual(wav_duration(path), 0.25, places=3)

    def test_returns_none_for_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(wav_duration(os.path.join(tmp, "missing.wav")))

    def test_returns_none_for_an_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.wav")
            open(path, "wb").close()
            self.assertIsNone(wav_duration(path))

    def test_returns_none_when_the_frame_rate_is_zero(self):
        # フレームレートが 0 の WAV は、割り算せず None を返す。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "zero.wav")
            # wave の書き込みは rate=0 を拒むので、ヘッダーを直接組み立てる。
            fmt = struct.pack("<HHIIHH", 1, 1, 0, 0, 2, 16)
            data = b"\x00\x00" * 8
            body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
                    + b"data" + struct.pack("<I", len(data)) + data)
            with open(path, "wb") as handle:
                handle.write(b"RIFF" + struct.pack("<I", len(body)) + body)
            self.assertIsNone(wav_duration(path))

    def test_the_file_is_closed_after_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_wav(os.path.join(tmp, "a.wav"), 0.1)
            closes = []
            real_open = wave.open

            def tracking_open(*args, **kwargs):
                handle = real_open(*args, **kwargs)
                real_close = handle.close

                def close():
                    closes.append(handle)
                    real_close()

                handle.close = close
                return handle

            with mock.patch("chime.audio.wave.open", tracking_open):
                self.assertAlmostEqual(wav_duration(path), 0.1, places=3)
            self.assertEqual(len(closes), 1)

    def test_the_file_is_closed_even_when_reading_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_wav(os.path.join(tmp, "a.wav"), 0.1)
            closes = []
            real_open = wave.open

            def tracking_open(*args, **kwargs):
                handle = real_open(*args, **kwargs)
                real_close = handle.close

                def close():
                    closes.append(handle)
                    real_close()

                handle.close = close
                handle.getnframes = mock.Mock(side_effect=EOFError)
                return handle

            with mock.patch("chime.audio.wave.open", tracking_open):
                self.assertIsNone(wav_duration(path))
            self.assertEqual(len(closes), 1)

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

    def test_gap_ms_is_slept_between_segments_but_not_after_the_last(self):
        # セグメントごとの間隔指定は無く、プレイヤーの gap_ms だけが使われる。
        player = RecordingPlayer(dict(AUDIO, gap_ms=250))
        with mock.patch("chime.audio.time.sleep") as sleep:
            player.play([Segment(self.first), Segment(self.second)])
        sleep.assert_called_once_with(0.25)

    def test_returns_count_of_played_segments(self):
        player = RecordingPlayer(AUDIO)
        result = player.play([Segment(self.first), Segment(self.second)])
        self.assertEqual(result, 2)

    def test_returns_zero_when_nothing_to_play(self):
        player = RecordingPlayer(AUDIO)
        self.assertEqual(player.play([]), 0)

    def test_returns_zero_when_all_segments_are_missing_optional(self):
        """全セグメントが optional かつ音源が欠落している場合、例外を出さず
        再生数 0 を返す（合成音声のキャッシュが再生前に消えるケースなどを
        想定した再現テスト）。"""
        player = RecordingPlayer(AUDIO)
        result = player.play([Segment("/nonexistent.wav", optional=True)])
        self.assertEqual(result, 0)
        self.assertEqual(player.played, [])


class PlaySelectionTest(unittest.TestCase):
    """再生前に対象を絞る処理（空パスは無視・optional の欠落はスキップ・必須の欠落は失敗）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.first = make_wav(os.path.join(self.tmp.name, "1.wav"))
        self.second = make_wav(os.path.join(self.tmp.name, "2.wav"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_required_file_message_names_the_path(self):
        player = RecordingPlayer(AUDIO)
        with self.assertRaises(PlaybackError) as caught:
            player.play([Segment("/nonexistent.wav", label="欠落")])
        self.assertEqual(str(caught.exception), "音源ファイルが見つかりません: /nonexistent.wav")

    def test_missing_required_file_fails_before_anything_is_opened_or_played(self):
        # 絞り込みは再生の前に全セグメントへ行う。後ろに必須の欠落があれば、
        # 手前の正常なセグメントも鳴らさず、デバイスも開かない。
        player = RecordingPlayer(AUDIO)
        with self.assertRaises(PlaybackError):
            player.play([Segment(self.first), Segment("/nonexistent.wav")])
        self.assertEqual((player.played, player.opened, player.closed), ([], 0, 0))

    def test_first_missing_required_file_wins(self):
        player = RecordingPlayer(AUDIO)
        with self.assertRaises(PlaybackError) as caught:
            player.play([Segment("/missing-a.wav"), Segment("/missing-b.wav")])
        self.assertIn("/missing-a.wav", str(caught.exception))

    def test_missing_optional_file_is_skipped_with_a_warning(self):
        player = RecordingPlayer(AUDIO)
        with logs_enabled(), self.assertLogs("chime.audio", level="WARNING") as captured:
            player.play([Segment("/nonexistent.wav", optional=True), Segment(self.first)])
        self.assertEqual([record.getMessage() for record in captured.records], [
            "音源ファイルが見つかりません: /nonexistent.wav（このセグメントはスキップします）"])
        self.assertEqual(captured.records[0].levelname, "WARNING")
        self.assertEqual(player.played, [self.first])

    def test_a_missing_required_file_is_not_hidden_by_an_optional_one(self):
        player = RecordingPlayer(AUDIO)
        with self.assertRaises(PlaybackError):
            player.play([Segment("/missing-optional.wav", optional=True),
                         Segment("/missing-required.wav")])

    def test_nothing_playable_warns_and_does_not_open_the_device(self):
        player = RecordingPlayer(AUDIO)
        with logs_enabled(), self.assertLogs("chime.audio", level="WARNING") as captured:
            result = player.play([Segment(""), Segment("/nonexistent.wav", optional=True)])
        self.assertEqual(result, 0)
        self.assertEqual([record.getMessage() for record in captured.records], [
            "音源ファイルが見つかりません: /nonexistent.wav（このセグメントはスキップします）",
            "再生できるセグメントがありません。"])
        self.assertEqual((player.opened, player.closed), (0, 0))

    def test_empty_segment_list_also_warns(self):
        player = RecordingPlayer(AUDIO)
        with logs_enabled(), self.assertLogs("chime.audio", level="WARNING") as captured:
            self.assertEqual(player.play([]), 0)
        self.assertEqual([record.getMessage() for record in captured.records],
                         ["再生できるセグメントがありません。"])

    def test_empty_path_is_dropped_silently(self):
        player = RecordingPlayer(AUDIO)
        with logs_enabled(), self.assertLogs("chime.audio", level="INFO") as captured:
            player.play([Segment("", label="空"), Segment(self.first, label="一つ目")])
        self.assertEqual([record.getMessage() for record in captured.records],
                         ["再生[recording]: 一つ目"])

    def test_segments_are_played_in_the_given_order_and_may_repeat(self):
        player = RecordingPlayer(AUDIO)
        result = player.play([Segment(self.second), Segment(self.first), Segment(self.second)])
        self.assertEqual(player.played, [self.second, self.first, self.second])
        self.assertEqual(result, 3)

    def test_segments_may_be_any_sequence_type(self):
        player = RecordingPlayer(AUDIO)
        self.assertEqual(player.play((Segment(self.first), Segment(self.second))), 2)
        self.assertEqual(player.played, [self.first, self.second])


class SelectPlayableTest(unittest.TestCase):
    """``Player._select_playable``: ``play`` から切り出した絞り込み。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.first = make_wav(os.path.join(self.tmp.name, "1.wav"))
        self.second = make_wav(os.path.join(self.tmp.name, "2.wav"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_keeps_the_playable_segments_in_order_and_by_identity(self):
        a, b = Segment(self.first), Segment(self.second)
        selected = Player._select_playable(
            [Segment(""), a, Segment("/nonexistent.wav", optional=True), b])
        self.assertIsInstance(selected, list)
        self.assertEqual(len(selected), 2)
        self.assertIs(selected[0], a)
        self.assertIs(selected[1], b)

    def test_is_callable_on_an_instance_and_does_not_open_the_device(self):
        player = RecordingPlayer(AUDIO)
        segment = Segment(self.first)
        self.assertEqual(player._select_playable([segment]), [segment])
        self.assertEqual((player.opened, player.closed, player.played), (0, 0, []))

    def test_empty_input_gives_an_empty_list(self):
        self.assertEqual(Player._select_playable([]), [])

    def test_accepts_a_tuple_and_leaves_the_input_untouched(self):
        segments = (Segment(""), Segment(self.first))
        self.assertEqual(Player._select_playable(segments), [segments[1]])
        self.assertEqual(len(segments), 2)

    def test_a_missing_required_segment_raises_with_the_path(self):
        with self.assertRaises(PlaybackError) as caught:
            Player._select_playable([Segment(self.first), Segment("/nonexistent.wav")])
        self.assertEqual(str(caught.exception), "音源ファイルが見つかりません: /nonexistent.wav")

    def test_a_missing_optional_segment_warns_and_is_dropped(self):
        with logs_enabled(), self.assertLogs("chime.audio", level="WARNING") as captured:
            selected = Player._select_playable([Segment("/nonexistent.wav", optional=True)])
        self.assertEqual(selected, [])
        self.assertEqual([record.getMessage() for record in captured.records], [
            "音源ファイルが見つかりません: /nonexistent.wav（このセグメントはスキップします）"])

    def test_play_uses_the_selection(self):
        player = RecordingPlayer(AUDIO)
        selected = [Segment(self.first)]
        with mock.patch.object(Player, "_select_playable", return_value=selected) as select:
            self.assertEqual(player.play([Segment("/ignored.wav")]), 1)
        select.assert_called_once()
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
