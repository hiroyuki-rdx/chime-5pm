"""再生バックエンドのテスト。"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from unittest import mock

from tests.support import REPO_ROOT, RecordingPlayer, logs_enabled, make_wav

from chime import audio
from chime.audio import (SEGMENT_KINDS, CommandPlayer, MockPlayer, PlaybackError,
                         PlaybackInterrupted, Player, PygamePlayer, Segment, create_player,
                         max_segment_seconds, segment_time_limit, volume_for, wav_duration)
from chime.config import DEFAULT_CONFIG

AUDIO = dict(DEFAULT_CONFIG["audio"], gap_ms=0, mock_max_seconds=0.01)


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def captured_logs(level=logging.WARNING):
    """``chime.audio`` のログを集める。1 件も出ないことの確認にも使える（``assertLogs`` と違い）。

    ``tests/__init__.py`` が止めているログを、このブロックの間だけ戻す。
    集めたログは親（root）へ流さない（stderr に漏らさない）。戻り値は
    ``(水準名, メッセージ)`` の一覧で、ブロックのあとも読める。
    """
    logger = audio.logger
    handler = _ListHandler()
    saved = (logger.level, logger.propagate)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    found = []
    try:
        with logs_enabled():
            yield found
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]
        found.extend((record.levelname, record.getMessage()) for record in handler.records)


def make_odd_wav(path, seconds=0.1):
    """鳴らせるが、``wave`` が ``RuntimeError`` を出すヘッダーの WAV を ``path`` に書く。

    fmt チャンクの大きさを RIFF 全体より大きくしてある（``wav_duration`` が読めない形）。
    """
    make_wav(path, seconds)
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    struct.pack_into("<I", data, 16, 0xE410)
    with open(path, "wb") as handle:
        handle.write(bytes(data))
    return path


class WarnOnceTestCase(unittest.TestCase):
    """「プロセスで 1 回だけ」の警告の記憶を、テストごとに空にする。"""

    def setUp(self):
        audio._warned.clear()
        self.addCleanup(audio._warned.clear)


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

    def test_returns_none_for_a_header_whose_chunk_is_larger_than_the_file(self):
        # fmt チャンクの大きさが RIFF 全体を超えるヘッダーでは、wave が wave.Error ではなく
        # RuntimeError を出す（Python 3.10〜3.13 で確認）。長さが分からないだけなので、例外にしない。
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(wav_duration(make_odd_wav(os.path.join(tmp, "odd.wav"))))

    def test_returns_none_whatever_the_reader_raises(self):
        # 長さが読めないことは、再生を止める理由にならない（呼び出し側は上限の既定値で続ける）。
        errors = (RuntimeError("chunk"), struct.error("short"), ValueError("bad"),
                  OverflowError("big"), ZeroDivisionError("rate"), MemoryError(),
                  AttributeError("odd"), UnicodeError("name"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with mock.patch("chime.audio.wave.open", side_effect=error):
                    self.assertIsNone(wav_duration("a.wav"))
        with tempfile.TemporaryDirectory() as tmp:
            path = make_wav(os.path.join(tmp, "a.wav"), 0.1)
            handle = wave.open(path, "rb")
            self.addCleanup(handle.close)
            handle.getnframes = mock.Mock(side_effect=RuntimeError("frames"))
            with mock.patch("chime.audio.wave.open", return_value=handle):
                self.assertIsNone(wav_duration(path))

    def test_does_not_swallow_a_request_to_quit(self):
        # Exception だけを受ける。Ctrl+C（KeyboardInterrupt）や SystemExit は通す。
        for error in (KeyboardInterrupt(), SystemExit(1)):
            with self.subTest(error=type(error).__name__):
                with mock.patch("chime.audio.wave.open", side_effect=error):
                    with self.assertRaises(type(error)):
                        wav_duration("a.wav")


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



class SegmentKindTest(unittest.TestCase):
    def test_kinds_are_the_keys_of_audio_volume(self):
        self.assertEqual(set(SEGMENT_KINDS), set(DEFAULT_CONFIG["audio"]["volume"]))
        self.assertEqual(SEGMENT_KINDS, ("time_signal", "voice", "music"))

    def test_the_default_kind_is_voice_so_existing_construction_sites_keep_working(self):
        self.assertEqual(Segment("a.wav").kind, "voice")
        self.assertEqual(Segment("a.wav", "ラベル", 100, True).kind, "voice")

    def test_kind_is_a_keyword_that_does_not_change_the_other_fields(self):
        segment = Segment("a.wav", label="x", fade_in_ms=5, optional=True, kind="music")
        self.assertEqual((segment.path, segment.label, segment.fade_in_ms, segment.optional,
                          segment.kind), ("a.wav", "x", 5, True, "music"))
        self.assertEqual(segment.describe(), "x")


class VolumeForTest(WarnOnceTestCase):
    def volume(self, kind, **volumes):
        return volume_for(Segment("a.wav", kind=kind), {"volume": volumes})

    def test_reads_the_volume_of_the_kind(self):
        settings = {"volume": {"time_signal": 0.25, "voice": 0.5, "music": 0.75}}
        self.assertEqual([volume_for(Segment("a.wav", kind=kind), settings)
                          for kind in SEGMENT_KINDS], [0.25, 0.5, 0.75])

    def test_the_default_config_is_full_volume_for_every_kind(self):
        with captured_logs() as found:
            self.assertEqual([volume_for(Segment("a.wav", kind=kind), DEFAULT_CONFIG["audio"])
                              for kind in SEGMENT_KINDS], [1.0, 1.0, 1.0])
        self.assertEqual(found, [])

    def test_the_boundaries_and_integers_are_valid(self):
        with captured_logs() as found:
            self.assertEqual(self.volume("voice", voice=0), 0.0)
            self.assertEqual(self.volume("voice", voice=1), 1.0)
            self.assertEqual(self.volume("voice", voice=0.0), 0.0)
        self.assertEqual(found, [])
        self.assertIsInstance(self.volume("voice", voice=1), float)

    def test_a_missing_key_is_full_volume_with_one_warning(self):
        with captured_logs() as found:
            self.assertEqual(self.volume("music", voice=0.5), 1.0)
            self.assertEqual(self.volume("music", voice=0.5), 1.0)
        self.assertEqual(found, [("WARNING", "audio.volume.music が設定されていません。1.0 として扱います。")])

    def test_a_missing_volume_section_or_a_non_mapping_is_full_volume(self):
        for settings in ({}, {"volume": None}, {"volume": 0.5}, {"volume": [1.0]}):
            audio._warned.clear()
            with self.subTest(settings=settings), captured_logs() as found:
                self.assertEqual(volume_for(Segment("a.wav"), settings), 1.0)
            self.assertEqual(len(found), 1, found)

    def test_a_value_that_is_not_a_number_is_full_volume_with_one_warning(self):
        for value in ("0.5", "loud", None, True, False, [0.5], {"v": 1}):
            audio._warned.clear()
            with self.subTest(value=value), captured_logs() as found:
                self.assertEqual(self.volume("voice", voice=value), 1.0)
                self.assertEqual(self.volume("voice", voice=value), 1.0)
            self.assertEqual(len(found), 1, found)
            self.assertEqual(found[0][0], "WARNING")
            self.assertIn("audio.volume.voice", found[0][1])
            self.assertIn("解釈できません", found[0][1])

    def test_nan_is_full_volume_with_one_warning(self):
        with captured_logs() as found:
            self.assertEqual(self.volume("voice", voice=float("nan")), 1.0)
        self.assertEqual(len(found), 1)
        self.assertIn("解釈できません", found[0][1])

    def test_a_huge_integer_is_clamped_not_raised(self):
        with captured_logs() as found:
            self.assertEqual(self.volume("voice", voice=10 ** 400), 1.0)
        self.assertEqual(len(found), 1)

    def test_a_huge_integer_keeps_its_sign_like_infinity_and_minus_one(self):
        # float() に直せない整数（OverflowError）も、符号は分かる。-1 や -inf が無音（0.0）なのに
        # -10**400 だけ全開（1.0）になってはいけない。
        cases = [(10 ** 400, 1.0), (-10 ** 400, 0.0), (2 ** 1024, 1.0), (-2 ** 1024, 0.0),
                 (2 ** 1023, 1.0), (-2 ** 1023, 0.0)]
        for value, expected in cases:
            audio._warned.clear()
            with self.subTest(value=str(value)[:12]), captured_logs() as found:
                self.assertEqual(self.volume("music", music=value), expected)
                self.assertEqual(self.volume("music", music=value), expected)
            self.assertEqual(len(found), 1, found)
            self.assertEqual(found[0][0], "WARNING")
            self.assertIn("範囲外", found[0][1])  # 「解釈できません」ではなく、0〜1 に丸めた
        for huge, small in ((10 ** 400, 2), (-10 ** 400, -1)):
            audio._warned.clear()
            with captured_logs():
                self.assertEqual(self.volume("voice", voice=huge), self.volume("voice", voice=small))
                self.assertEqual(self.volume("voice", voice=huge),
                                 self.volume("voice", voice=float(small * 10 ** 300)))

    def test_as_number_gives_infinity_with_the_sign_of_a_huge_integer(self):
        self.assertEqual(audio._as_number(10 ** 400), float("inf"))
        self.assertEqual(audio._as_number(-10 ** 400), float("-inf"))
        self.assertEqual(audio._as_number(7), 7.0)
        for value in (True, False, "1", None, [1], float("nan")):
            self.assertIsNone(audio._as_number(value), value)

    def test_out_of_range_is_clamped_with_one_warning(self):
        cases = [(1.5, 1.0), (2, 1.0), (-0.1, 0.0), (-3, 0.0),
                 (float("inf"), 1.0), (float("-inf"), 0.0)]
        for value, expected in cases:
            audio._warned.clear()
            with self.subTest(value=value), captured_logs() as found:
                self.assertEqual(self.volume("music", music=value), expected)
                self.assertEqual(self.volume("music", music=value), expected)
            self.assertEqual(len(found), 1, found)
            self.assertIn("範囲外", found[0][1])
            self.assertIn("audio.volume.music", found[0][1])

    def test_each_key_warns_once_independently(self):
        with captured_logs() as found:
            self.volume("voice", voice=2.0, music="x", time_signal=-1)
            self.volume("music", voice=2.0, music="x", time_signal=-1)
            self.volume("time_signal", voice=2.0, music="x", time_signal=-1)
            self.volume("voice", voice=2.0, music="x", time_signal=-1)
            self.volume("music", voice=2.0, music="x", time_signal=-1)
        self.assertEqual(len(found), 3)
        self.assertEqual([("voice" in m, "music" in m, "time_signal" in m) for _, m in found],
                         [(True, False, False), (False, True, False), (False, False, True)])

    def test_an_unknown_kind_is_full_volume_with_a_warning(self):
        with captured_logs() as found:
            self.assertEqual(volume_for(Segment("a.wav", kind="jingle"), AUDIO), 1.0)
        self.assertEqual(len(found), 1)
        self.assertIn("audio.volume.jingle", found[0][1])


class MaxSegmentSecondsTest(WarnOnceTestCase):
    def test_reads_the_setting(self):
        self.assertEqual(max_segment_seconds({"max_segment_seconds": 30}), 30)
        self.assertEqual(max_segment_seconds({"max_segment_seconds": 0.5}), 0.5)

    def test_the_default_config_is_600(self):
        with captured_logs() as found:
            self.assertEqual(max_segment_seconds(DEFAULT_CONFIG["audio"]), 600)
        self.assertEqual(found, [])

    def test_a_missing_key_is_600_without_a_warning(self):
        with captured_logs() as found:
            self.assertEqual(max_segment_seconds({}), 600)
        self.assertEqual(found, [])

    def test_a_bad_value_is_600_with_one_warning(self):
        for value in ("600", None, True, 0, -5, float("nan"), float("inf"), [], 10 ** 400 * 10 ** 400):
            audio._warned.clear()
            with self.subTest(value=value), captured_logs() as found:
                self.assertEqual(max_segment_seconds({"max_segment_seconds": value}), 600)
                self.assertEqual(max_segment_seconds({"max_segment_seconds": value}), 600)
            self.assertEqual(len(found), 1, found)
            self.assertEqual(found[0][0], "WARNING")
            self.assertIn("audio.max_segment_seconds", found[0][1])

    def test_a_huge_integer_of_either_sign_is_600_with_one_warning(self):
        for value in (10 ** 400, -10 ** 400):
            audio._warned.clear()
            with self.subTest(value=str(value)[:12]), captured_logs() as found:
                self.assertEqual(max_segment_seconds({"max_segment_seconds": value}), 600)
            self.assertEqual(len(found), 1, found)
            self.assertIn("audio.max_segment_seconds", found[0][1])


class SegmentTimeLimitTest(WarnOnceTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data=b"x"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_a_wav_is_its_duration_plus_ten_seconds(self):
        path = make_wav(os.path.join(self.tmp.name, "a.wav"), 0.5)
        self.assertAlmostEqual(segment_time_limit(Segment(path), {}), 10.5, places=3)

    def test_a_wav_ignores_max_segment_seconds(self):
        path = make_wav(os.path.join(self.tmp.name, "a.wav"), 0.5)
        self.assertAlmostEqual(
            segment_time_limit(Segment(path), {"max_segment_seconds": 3}), 10.5, places=3)

    def test_other_files_use_max_segment_seconds(self):
        path = self.write("a.mp3")
        self.assertEqual(segment_time_limit(Segment(path), {"max_segment_seconds": 42}), 42)
        self.assertEqual(segment_time_limit(Segment(path), AUDIO), 600)

    def test_an_unreadable_wav_uses_max_segment_seconds(self):
        path = self.write("broken.wav", b"RIFF")
        self.assertEqual(segment_time_limit(Segment(path), {"max_segment_seconds": 42}), 42)

    def test_a_bad_max_segment_seconds_is_600(self):
        path = self.write("a.mp3")
        with captured_logs():
            self.assertEqual(segment_time_limit(Segment(path), {"max_segment_seconds": "x"}), 600)

    def test_a_wav_whose_header_cannot_be_read_never_raises(self):
        path = make_odd_wav(os.path.join(self.tmp.name, "odd.wav"))
        self.assertEqual(segment_time_limit(Segment(path), {"max_segment_seconds": 42}), 42)
        with mock.patch("chime.audio.wave.open", side_effect=RuntimeError("chunk")):
            self.assertEqual(segment_time_limit(Segment(path), AUDIO), 600)


class PlaybackInterruptedTest(unittest.TestCase):
    def test_it_is_a_playback_error_so_old_callers_still_catch_it(self):
        self.assertTrue(issubclass(PlaybackInterrupted, PlaybackError))

    def test_it_carries_the_number_of_finished_segments(self):
        self.assertEqual(PlaybackInterrupted().played, 0)
        error = PlaybackInterrupted("止めた", played=3)
        self.assertEqual((error.played, str(error)), (3, "止めた"))

    def test_it_says_whether_any_sound_was_started(self):
        # 既定は True（音が出たかもしれない。分からないときは出たものとして扱い、今までどおり
        # 「放送した」ことにする）。音を出す前に止まったと分かっている場所だけが False にする。
        self.assertIs(PlaybackInterrupted().started, True)
        self.assertIs(PlaybackInterrupted("止めた", played=3).started, True)
        error = PlaybackInterrupted("止めた", played=0, started=False)
        self.assertEqual((error.played, error.started, str(error)), (0, False, "止めた"))
        self.assertIs(PlaybackInterrupted(started=False).started, False)


class FakeClock(object):
    """時計と ``sleep`` の偽物。``sleep`` した秒数だけ時計が進む（実時間では待たない）。

    ``limit`` 回を超えて ``sleep`` すると ``AssertionError``。退行して待ちが終わらなくなっても、
    テストが固まらずに落ちる（上限の 600 秒でも 12000 回なので、通常は届かない）。
    """

    def __init__(self, limit=100000):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = None
        self.limit = limit

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.limit:
            raise AssertionError("待ちが終わらない（sleep が {0} 回を超えた）".format(self.limit))
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))


class FakeMusic(object):
    """``pygame.mixer.music`` の偽物。呼ばれた順を ``mixer.calls`` に残す。"""

    def __init__(self, mixer):
        self.mixer = mixer
        self.left = 0
        self.polls = 0
        #: ``fadeout`` を呼ばれたあと（``mixer.fade`` が "blocking"）は鳴り止んでいる。
        self.faded = False
        #: ``mixer.fade`` が "nonblocking" のとき、フェードが終わる偽の時刻。
        self.fade_ends = None

    def record(self, *call):
        self.mixer.calls.append(call)
        if self.mixer.clock is not None:
            self.mixer.times.append((call[0], self.mixer.clock.now))
        if self.mixer.on_call is not None:
            self.mixer.on_call(call[0])

    def load(self, path):
        self.record("load", os.path.basename(path))

    def set_volume(self, volume):
        self.record("set_volume", volume)

    def play(self, *args, **kwargs):
        self.record("play", args, kwargs)
        self.left = self.mixer.busy_polls
        if self.mixer.on_play is not None:
            self.mixer.on_play()

    def get_busy(self):
        self.polls += 1
        if self.faded:
            return False
        if self.fade_ends is not None:
            return self.mixer.clock.now < self.fade_ends
        if self.mixer.stuck:
            return True
        if self.left > 0:
            self.left -= 1
            return True
        return False

    def fadeout(self, milliseconds):
        self.record("fadeout", milliseconds)
        if self.mixer.fade == "blocking":  # 実機の pygame の説明どおり、フェードが終わるまで戻らない
            self.mixer.clock.now += milliseconds / 1000.0
            self.faded = True
        elif self.mixer.fade == "nonblocking":  # すぐ戻る版。フェードの間は鳴っている
            self.fade_ends = self.mixer.clock.now + milliseconds / 1000.0

    def stop(self):
        self.record("stop")


class FakeMixer(object):
    """``pygame.mixer`` の偽物。

    ``busy_polls``: 1 つのセグメントの ``get_busy`` が ``True`` を返す回数（その後 ``False``）。
    ``stuck``: ``get_busy`` が永遠に ``True``。``init_failures``: 最初の何回の ``init`` が失敗するか。
    ``on_call``: 音楽側の呼び出し（load / set_volume / play / fadeout / stop）のたびに、その名前で呼ぶ。
    ``fade``: ``fadeout`` の振る舞い。``None``（呼ぶだけ）/ "blocking" / "nonblocking"。
    ``clock``: 渡すと、呼び出しを ``times`` に ``(名前, 偽の時刻)`` で残す。``quit_error``: ``quit`` が出す例外。
    """

    def __init__(self, busy_polls=2, stuck=False, init_failures=0):
        self.calls = []
        self.busy_polls = busy_polls
        self.stuck = stuck
        self.init_failures = init_failures
        self.on_play = None
        self.on_call = None
        self.fade = None
        self.clock = None
        self.times = []
        self.quit_error = None
        self.music = FakeMusic(self)

    def init(self, **kwargs):
        self.calls.append(("init", kwargs))
        if self.init_failures > 0:
            self.init_failures -= 1
            raise RuntimeError("ALSA: Device or resource busy")

    def get_init(self):
        return (44100, -16, 2)

    def quit(self):
        self.calls.append(("quit",))
        if self.quit_error is not None:
            raise self.quit_error

    def names(self):
        return [call[0] for call in self.calls]

    def time_of(self, name):
        """``name`` の呼び出しの偽の時刻（最初の 1 回）。"""
        return [moment for call, moment in self.times if call == name][0]


class PygamePlayerTestCase(WarnOnceTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.first = make_wav(os.path.join(self.tmp.name, "1.wav"))
        self.second = make_wav(os.path.join(self.tmp.name, "2.wav"))
        self.third = make_wav(os.path.join(self.tmp.name, "3.wav"))
        self.clock = FakeClock()
        self.mixer = FakeMixer()
        self.mixer.clock = self.clock

    def make_player(self, **settings):
        return PygamePlayer(dict(AUDIO, **settings), mixer_module=self.mixer,
                            sleep=self.clock.sleep, clock=self.clock.time)

    def stop_at_sleep(self, number):
        """``number`` 回目の待機で停止要求を立てる（立てた Event を返す）。"""
        stop = threading.Event()
        self.clock.on_sleep = lambda count: stop.set() if count == number else None
        return stop


class PygamePlayerPlayTest(PygamePlayerTestCase):
    def test_the_call_order_is_load_then_set_volume_then_play(self):
        self.make_player().play([Segment(self.first)])
        self.assertEqual(self.mixer.calls[1:4], [
            ("load", "1.wav"), ("set_volume", 1.0), ("play", (), {})])

    def test_default_behaviour_is_v6_1_0_plus_set_volume(self):
        # 既定の設定・停止要求なし: init → (load → play → 終わるまで待つ) → stop → quit。
        # v6.1.0 との違いは set_volume(1.0) の 1 行だけ。
        player = self.make_player()
        self.assertEqual(player.play([Segment(self.first), Segment(self.second)]), 2)
        expected = [
            ("init", {"frequency": 44100, "size": -16, "channels": 2, "buffer": 4096}),
            ("load", "1.wav"), ("set_volume", 1.0), ("play", (), {}),
            ("load", "2.wav"), ("set_volume", 1.0), ("play", (), {}),
            ("stop",), ("quit",)]
        self.assertEqual(self.mixer.calls, expected)
        self.assertEqual(self.clock.sleeps, [audio.POLL_SECONDS] * 4)

    def test_fade_in_is_passed_to_play(self):
        self.make_player().play([Segment(self.first, fade_in_ms=2000)])
        self.assertIn(("play", (), {"loops": 0, "fade_ms": 2000}), self.mixer.calls)

    def test_volume_is_per_kind(self):
        player = self.make_player(volume={"time_signal": 0.3, "voice": 0.6, "music": 0.9})
        player.play([Segment(self.first, kind="time_signal"), Segment(self.second, kind="voice"),
                     Segment(self.third, kind="music")])
        volumes = [call[1] for call in self.mixer.calls if call[0] == "set_volume"]
        self.assertEqual(volumes, [0.3, 0.6, 0.9])
        # 音量は毎回 load の後・play の前（load すると pygame は音量を戻すため）。
        names = [call[0] for call in self.mixer.calls if call[0] in ("load", "set_volume", "play")]
        self.assertEqual(names, ["load", "set_volume", "play"] * 3)

    def test_a_zero_volume_is_set_as_zero(self):
        self.make_player(volume={"time_signal": 1.0, "voice": 0.0, "music": 1.0}).play(
            [Segment(self.first)])
        self.assertIn(("set_volume", 0.0), self.mixer.calls)

    def test_a_bad_volume_still_plays_at_full_volume_and_warns_once(self):
        player = self.make_player(volume={"time_signal": 1.0, "voice": "loud", "music": 1.0})
        with captured_logs() as found:
            self.assertEqual(player.play([Segment(self.first), Segment(self.second)]), 2)
        self.assertEqual([call[1] for call in self.mixer.calls if call[0] == "set_volume"],
                         [1.0, 1.0])
        self.assertEqual(len(found), 1)
        self.assertIn("audio.volume.voice", found[0][1])

    def test_a_failing_set_volume_does_not_stop_the_broadcast(self):
        def broken(volume):
            raise RuntimeError("mixer の音量を変えられない")

        self.mixer.music.set_volume = broken
        with captured_logs() as found:
            self.assertEqual(self.make_player().play([Segment(self.first), Segment(self.second)]), 2)
        self.assertEqual(self.mixer.names().count("play"), 2)
        self.assertEqual(len(found), 1)  # 2 回目は黙る
        self.assertIn("音量を設定できませんでした", found[0][1])
        self.assertIn("RuntimeError", found[0][1])

    def test_the_device_is_released_after_playing_and_after_an_error(self):
        self.make_player().play([Segment(self.first)])
        self.assertEqual(self.mixer.calls[-2:], [("stop",), ("quit",)])

        self.mixer.calls.clear()
        self.mixer.music.load = mock.Mock(side_effect=RuntimeError("壊れた音源"))
        with self.assertRaises(RuntimeError):
            self.make_player().play([Segment(self.first)])
        self.assertEqual(self.mixer.calls[-2:], [("stop",), ("quit",)])

    def test_without_pygame_installed_the_player_raises_playback_error(self):
        with mock.patch.object(audio, "pygame", None):
            with self.assertRaises(PlaybackError) as caught:
                PygamePlayer(AUDIO).play([Segment(self.first)])
        self.assertIn("pygame が導入されていません", str(caught.exception))

    def test_the_default_mixer_is_pygame_mixer(self):
        fake = types.SimpleNamespace(mixer=self.mixer)
        with mock.patch.object(audio, "pygame", fake):
            player = PygamePlayer(AUDIO, sleep=self.clock.sleep, clock=self.clock.time)
            self.assertEqual(player.play([Segment(self.first)]), 1)
        self.assertEqual(self.mixer.names()[:2], ["init", "load"])

    def test_playback_that_ends_in_time_is_not_cut_off(self):
        self.mixer.busy_polls = 100  # 5 秒。WAV の上限（長さ + 10 秒）より短い
        self.assertEqual(self.make_player().play([Segment(self.first)]), 1)
        self.assertNotIn("stop", self.mixer.names()[:-2])

    def test_the_time_limit_is_decided_before_the_sound_starts(self):
        # 鳴らし始めたあとに例外が出て放送を途中で投げ出さないよう、上限の秒数は play() の前に決める。
        real = audio.segment_time_limit

        def spy(segment, settings):
            self.mixer.calls.append(("limit",))
            return real(segment, settings)

        with mock.patch.object(audio, "segment_time_limit", spy):
            self.make_player().play([Segment(self.first), Segment(self.second)])
        names = self.mixer.names()
        self.assertEqual(names.count("limit"), 2)
        self.assertLess(names.index("limit"), names.index("play"))
        second_limit = names.index("limit", names.index("limit") + 1)
        self.assertLess(second_limit, names.index("play", names.index("play") + 1))

    def test_nothing_is_played_if_the_time_limit_cannot_be_decided(self):
        with mock.patch.object(audio, "segment_time_limit", side_effect=RuntimeError("limit")):
            with self.assertRaises(RuntimeError):
                self.make_player().play([Segment(self.first)])
        self.assertNotIn("play", self.mixer.names())
        self.assertEqual(self.mixer.calls[-2:], [("stop",), ("quit",)])

    def test_a_wav_with_an_odd_header_is_still_played_to_the_end(self):
        # wav_duration が RuntimeError を出す WAV でも、鳴らし始めたあとに放送を止めない。
        odd = make_odd_wav(os.path.join(self.tmp.name, "odd.wav"))
        self.assertEqual(self.make_player().play([Segment(odd)]), 1)
        self.assertEqual(self.mixer.names().count("play"), 1)
        self.assertEqual(self.mixer.calls[-2:], [("stop",), ("quit",)])

    def test_a_wav_with_an_odd_header_is_cut_off_at_max_segment_seconds(self):
        odd = make_odd_wav(os.path.join(self.tmp.name, "odd.wav"))
        self.mixer.stuck = True
        with self.assertRaises(PlaybackError) as caught:
            self.make_player(max_segment_seconds=30).play([Segment(odd)])
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertIn(odd, str(caught.exception))
        self.assertGreaterEqual(self.clock.now, 30)
        self.assertLess(self.clock.now, 30 + 2 * audio.POLL_SECONDS)


class PygamePlayerInitRetryTest(PygamePlayerTestCase):
    def test_init_is_retried_three_times_one_second_apart_then_playback_error(self):
        self.mixer.init_failures = 99
        player = self.make_player()
        with captured_logs() as found, self.assertRaises(PlaybackError) as caught:
            player.play([Segment(self.first)])
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertEqual(self.mixer.names(), ["init", "init", "init"])
        self.assertEqual(self.clock.sleeps, [1.0, 1.0])
        self.assertIn("3 回試しました", str(caught.exception))
        self.assertIn("ALSA: Device or resource busy", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual([level for level, _ in found], ["WARNING", "WARNING"])
        self.assertIn("1/3 回目", found[0][1])
        self.assertIn("2/3 回目", found[1][1])

    def test_nothing_is_loaded_and_the_mixer_is_not_quit_when_init_never_works(self):
        self.mixer.init_failures = 99
        with captured_logs(), self.assertRaises(PlaybackError):
            self.make_player().play([Segment(self.first)])
        self.assertEqual(set(self.mixer.names()), {"init"})

    def test_init_that_works_on_the_second_try_plays(self):
        self.mixer.init_failures = 1
        with captured_logs() as found:
            self.assertEqual(self.make_player().play([Segment(self.first)]), 1)
        self.assertEqual(self.mixer.names().count("init"), 2)
        self.assertEqual(self.clock.sleeps[:1], [1.0])
        self.assertEqual(len(found), 1)

    def test_init_that_works_on_the_third_try_plays(self):
        self.mixer.init_failures = 2
        with captured_logs():
            self.assertEqual(self.make_player().play([Segment(self.first)]), 1)
        self.assertEqual(self.mixer.names().count("init"), 3)
        self.assertEqual(self.clock.sleeps[:2], [1.0, 1.0])

    def test_init_that_works_first_time_does_not_wait(self):
        self.make_player().play([Segment(self.first, kind="music")])
        self.assertEqual(self.mixer.names().count("init"), 1)
        self.assertNotIn(1.0, self.clock.sleeps)

    def test_a_stop_request_during_the_retry_wait_gives_up_quickly(self):
        self.mixer.init_failures = 99
        stop = self.stop_at_sleep(1)
        with captured_logs(), self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first)], stop=stop)
        self.assertEqual(caught.exception.played, 0)
        self.assertEqual(self.mixer.names(), ["init"])

    def test_without_the_injected_sleep_the_wait_is_time_sleep(self):
        self.mixer.init_failures = 1
        player = PygamePlayer(AUDIO, mixer_module=self.mixer)
        with captured_logs(), mock.patch("chime.audio.time.sleep") as sleep:
            player.play([Segment(self.first)])
        self.assertEqual(sleep.call_args_list[0], mock.call(1.0))


class PygamePlayerStopTest(PygamePlayerTestCase):
    def test_a_stop_request_fades_out_and_raises_with_the_finished_count(self):
        self.mixer.busy_polls = 5
        # 1 つ目の待機が 5 回（sleep 1〜5）。2 つ目の途中（sleep 7）で停止要求。
        stop = self.stop_at_sleep(7)
        player = self.make_player()
        with self.assertRaises(PlaybackInterrupted) as caught:
            player.play([Segment(self.first), Segment(self.second), Segment(self.third)], stop=stop)
        self.assertEqual(caught.exception.played, 1)
        self.assertIsInstance(caught.exception, PlaybackError)
        loads = [call[1] for call in self.mixer.calls if call[0] == "load"]
        self.assertEqual(loads, ["1.wav", "2.wav"])  # 3 つ目は始めない
        self.assertEqual(self.mixer.calls.count(("fadeout", 300)), 1)
        # フェードアウトは 2 つ目の再生のあと。後始末（stop → quit）はそのあと。
        self.assertLess(self.mixer.calls.index(("fadeout", 300)),
                        self.mixer.calls.index(("stop",)))
        self.assertEqual(self.mixer.calls[-2:], [("stop",), ("quit",)])

    def test_the_interruption_is_quick(self):
        self.mixer.stuck = True
        self.mixer.fade = "blocking"  # 実機の pygame の説明どおり、fadeout は終わるまで戻る
        stop = self.stop_at_sleep(3)
        with self.assertRaises(PlaybackInterrupted):
            self.make_player().play([Segment(self.first)], stop=stop)
        self.assertEqual(self.clock.sleeps, [audio.POLL_SECONDS] * 3)

    def test_a_stop_request_that_is_already_set_does_not_open_the_device(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first)], stop=stop)
        self.assertEqual(caught.exception.played, 0)
        self.assertEqual(self.mixer.calls, [])

    def test_a_stop_request_between_segments_does_not_start_the_next_one(self):
        self.mixer.busy_polls = 0
        stop = threading.Event()
        self.mixer.on_play = stop.set  # 1 つ目を鳴らし始めた直後に停止要求
        with self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first), Segment(self.second)], stop=stop)
        self.assertEqual(caught.exception.played, 1)
        self.assertEqual([call[1] for call in self.mixer.calls if call[0] == "load"], ["1.wav"])
        self.assertNotIn("fadeout", self.mixer.names())

    def test_an_unset_stop_event_changes_nothing(self):
        stop = threading.Event()
        self.assertEqual(self.make_player().play([Segment(self.first)], stop=stop), 1)
        self.assertNotIn("fadeout", self.mixer.names())

    def test_a_failing_fadeout_still_raises_the_interruption(self):
        self.mixer.stuck = True
        self.mixer.music.fadeout = mock.Mock(side_effect=RuntimeError("fadeout が失敗"))
        stop = self.stop_at_sleep(1)
        with captured_logs() as found, self.assertRaises(PlaybackInterrupted):
            self.make_player().play([Segment(self.first)], stop=stop)
        self.assertEqual(self.mixer.calls[-2:], [("stop",), ("quit",)])
        self.assertEqual(len(found), 1)
        self.assertIn("フェードアウトに失敗しました", found[0][1])

    def test_the_player_can_be_used_again_after_an_interruption(self):
        player = self.make_player()
        self.mixer.stuck = True
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.first)], stop=self.stop_at_sleep(1))
        self.mixer.stuck = False
        self.clock.on_sleep = None
        self.assertEqual(player.play([Segment(self.first)]), 1)

    def test_a_stop_request_between_load_and_play_never_starts_the_segment(self):
        # 停止要求が load〜play の間に来たら、鳴らし始めない（一瞬鳴ってから消えることもない）。
        for hook in ("load", "set_volume"):
            self.mixer = FakeMixer()
            self.mixer.clock = self.clock
            stop = threading.Event()
            self.mixer.on_call = lambda name, hook=hook, stop=stop: stop.set() if name == hook else None
            with self.subTest(stop_after=hook), self.assertRaises(PlaybackInterrupted) as caught:
                self.make_player().play([Segment(self.first), Segment(self.second)], stop=stop)
            names = self.mixer.names()
            self.assertNotIn("play", names)
            self.assertNotIn("fadeout", names)  # 鳴っていないので、フェードアウトもしない
            self.assertEqual(names[-2:], ["stop", "quit"])  # 後始末は今までどおり
            self.assertEqual((caught.exception.played, caught.exception.started), (0, False))

    def test_a_stop_request_between_load_and_play_of_a_later_segment_keeps_the_count(self):
        self.mixer.busy_polls = 0
        stop = threading.Event()
        loads = []

        def stop_at_second_load(name):
            if name == "load":
                loads.append(name)
                if len(loads) == 2:
                    stop.set()

        self.mixer.on_call = stop_at_second_load
        with self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first), Segment(self.second)], stop=stop)
        self.assertEqual(self.mixer.names().count("play"), 1)  # 2 つ目は鳴らし始めない
        self.assertNotIn("fadeout", self.mixer.names())
        self.assertEqual((caught.exception.played, caught.exception.started), (1, True))

    def test_started_is_false_when_the_stop_request_was_already_set(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (0, False))

    def test_started_is_false_when_the_stop_request_comes_while_the_device_is_opening(self):
        self.mixer.init_failures = 99
        stop = self.stop_at_sleep(1)
        with captured_logs(), self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (0, False))

    def test_started_is_true_when_the_stop_request_comes_during_the_first_segment(self):
        self.mixer.stuck = True
        with self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first)], stop=self.stop_at_sleep(2))
        self.assertEqual((caught.exception.played, caught.exception.started), (0, True))

    def test_started_is_true_when_the_stop_request_comes_right_after_play(self):
        # music.play() を呼んだ直後（get_busy を確かめる前）に立った停止要求。
        self.mixer.stuck = True
        stop = threading.Event()
        self.mixer.on_play = stop.set
        with self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (0, True))

    def test_started_is_true_when_the_stop_request_comes_between_segments_or_in_the_gap(self):
        self.mixer.busy_polls = 0
        stop = threading.Event()
        self.mixer.on_play = stop.set
        with self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first), Segment(self.second)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (1, True))

        self.mixer.on_play = None
        with self.assertRaises(PlaybackInterrupted) as caught:  # 間隔（gap_ms）の待ちの中
            self.make_player(gap_ms=200).play([Segment(self.first), Segment(self.second)],
                                              stop=self.stop_at_sleep(1))
        self.assertEqual((caught.exception.played, caught.exception.started), (1, True))


class PygamePlayerTimeoutTest(PygamePlayerTestCase):
    def test_a_wav_that_never_ends_is_cut_off_after_its_duration_plus_ten_seconds(self):
        self.mixer.stuck = True
        with self.assertRaises(PlaybackError) as caught:
            self.make_player().play([Segment(self.first)])
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertIn(self.first, str(caught.exception))
        self.assertIn("10.05", str(caught.exception))
        self.assertGreaterEqual(self.clock.now, 10.05)
        self.assertLess(self.clock.now, 10.05 + 2 * audio.POLL_SECONDS)
        self.assertEqual(self.mixer.calls[-3:], [("stop",), ("stop",), ("quit",)])
        self.assertNotIn("fadeout", self.mixer.names())

    def test_a_longer_wav_gets_a_longer_limit(self):
        long_wav = make_wav(os.path.join(self.tmp.name, "long.wav"), 2.0)
        self.mixer.stuck = True
        with self.assertRaises(PlaybackError):
            self.make_player().play([Segment(long_wav)])
        self.assertGreaterEqual(self.clock.now, 12.0)
        self.assertLess(self.clock.now, 12.0 + 2 * audio.POLL_SECONDS)

    def test_a_non_wav_uses_max_segment_seconds(self):
        mp3 = os.path.join(self.tmp.name, "hotaru.mp3")
        with open(mp3, "wb") as handle:
            handle.write(b"not a wav")
        self.mixer.stuck = True
        with self.assertRaises(PlaybackError) as caught:
            self.make_player(max_segment_seconds=30).play([Segment(mp3, kind="music")])
        self.assertIn(mp3, str(caught.exception))
        self.assertGreaterEqual(self.clock.now, 30)
        self.assertLess(self.clock.now, 30 + 2 * audio.POLL_SECONDS)

    def test_a_bad_max_segment_seconds_falls_back_to_600_with_one_warning(self):
        mp3 = os.path.join(self.tmp.name, "hotaru.mp3")
        with open(mp3, "wb") as handle:
            handle.write(b"x")
        for value in ("long", 0, -1, None):
            audio._warned.clear()
            self.clock.now = 0.0
            self.mixer.stuck = True
            with self.subTest(value=value), captured_logs() as found, \
                    self.assertRaises(PlaybackError):
                self.make_player(max_segment_seconds=value).play([Segment(mp3)])
            self.assertGreaterEqual(self.clock.now, 600)
            self.assertEqual(len(found), 1, found)
            self.assertIn("audio.max_segment_seconds", found[0][1])

    def test_the_timeout_aborts_the_whole_broadcast_and_does_not_start_the_next(self):
        self.mixer.stuck = True
        with self.assertRaises(PlaybackError):
            self.make_player().play([Segment(self.first), Segment(self.second)])
        self.assertEqual([call[1] for call in self.mixer.calls if call[0] == "load"], ["1.wav"])

    def test_a_failing_stop_still_raises_the_timeout_error(self):
        self.mixer.stuck = True
        original = self.mixer.music.stop
        self.mixer.music.stop = mock.Mock(side_effect=RuntimeError("stop が失敗"))
        with captured_logs() as found, self.assertRaises(PlaybackError) as caught:
            self.make_player().play([Segment(self.first)])
        self.assertIn(self.first, str(caught.exception))
        self.assertTrue(any("再生の停止に失敗しました" in message for _, message in found))
        self.mixer.music.stop = original

    def test_the_stop_request_wins_over_the_timeout(self):
        # 時計が一気に上限を越え、同じ待機で停止要求も立った場合は、中断として扱う。
        self.mixer.stuck = True
        stop = threading.Event()

        def jump_past_the_limit(count):
            self.clock.now += 100
            stop.set()

        self.clock.on_sleep = jump_past_the_limit
        with self.assertRaises(PlaybackInterrupted):
            self.make_player().play([Segment(self.first)], stop=stop)
        self.assertIn(("fadeout", 300), self.mixer.calls)


class RealTimeMusic(object):
    """本物の時計で動く ``music`` の偽物。5 秒鳴る。``fadeout`` はすぐ戻り、フェードの間は鳴っている。"""

    def __init__(self):
        self.ends = 0.0
        self.busy_calls = 0
        self.faded_at = self.stopped_at = None
        self.on_play = None

    def load(self, path):
        pass

    def set_volume(self, volume):
        pass

    def play(self, *args, **kwargs):
        self.ends = time.monotonic() + 5.0
        if self.on_play is not None:
            self.on_play()

    def get_busy(self):
        self.busy_calls += 1
        return time.monotonic() < self.ends

    def fadeout(self, milliseconds):
        self.faded_at = time.monotonic()
        self.ends = self.faded_at + milliseconds / 1000.0

    def stop(self):
        self.stopped_at = time.monotonic()
        self.ends = 0.0


class PygamePlayerFadeoutTest(PygamePlayerTestCase):
    """停止要求のフェードアウト。``fadeout()`` が終わるまで戻る版でも、すぐ戻る版でも、フェードは聞こえる。"""

    BOUND = (audio.STOP_FADEOUT_MS + 100) / 1000.0

    def interrupt_while_playing(self):
        self.mixer.stuck = True  # 鳴り続ける（フェードの設定があれば、そちらが優先）
        with self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first)], stop=self.stop_at_sleep(1))
        self.assertEqual(self.mixer.calls.count(("fadeout", audio.STOP_FADEOUT_MS)), 1)
        return caught.exception

    def fade_to_stop(self):
        """fadeout を呼んでから、後始末が music.stop を呼ぶまでの（偽の時計の）秒数。"""
        return self.mixer.time_of("stop") - self.mixer.time_of("fadeout")

    def test_a_fadeout_that_returns_at_once_is_waited_for_before_the_music_is_stopped(self):
        self.mixer.fade = "nonblocking"
        self.interrupt_while_playing()
        fade = audio.STOP_FADEOUT_MS / 1000.0
        self.assertGreaterEqual(self.fade_to_stop(), fade - 1e-9)  # フェードの長さは待つ
        self.assertLessEqual(self.fade_to_stop(), fade + audio.POLL_SECONDS + 1e-9)
        self.assertEqual(self.mixer.calls[-3:][1:], [("stop",), ("quit",)])

    def test_a_fadeout_that_blocks_is_not_waited_for_twice(self):
        self.mixer.fade = "blocking"
        self.interrupt_while_playing()
        self.assertAlmostEqual(self.fade_to_stop(), audio.STOP_FADEOUT_MS / 1000.0)
        self.assertEqual(self.clock.sleeps, [audio.POLL_SECONDS])  # 停止要求までの 1 回だけ

    def test_the_wait_for_the_fade_is_bounded_when_the_music_never_goes_quiet(self):
        self.mixer.fade = None  # fadeout は呼ばれたが、get_busy が真のまま
        self.interrupt_while_playing()
        self.assertAlmostEqual(self.fade_to_stop(), self.BOUND, places=6)
        self.assertEqual(self.mixer.calls[-2:], [("stop",), ("quit",)])
        self.assertEqual(max(self.clock.sleeps), audio.POLL_SECONDS)  # 1 回の待ちは POLL_SECONDS 以下

    def test_a_blocking_fadeout_that_ate_the_time_leaves_the_rest_of_the_bound(self):
        # 上限は fadeout を呼ぶ前から数える。fadeout が 300 ms 戻らなかったら、残りは 100 ms。
        def blocking_but_still_busy(milliseconds):
            self.mixer.calls.append(("fadeout", milliseconds))
            self.mixer.times.append(("fadeout", self.clock.now))
            self.clock.now += milliseconds / 1000.0

        self.mixer.music.fadeout = blocking_but_still_busy
        self.interrupt_while_playing()
        self.assertAlmostEqual(self.fade_to_stop(), self.BOUND, places=6)

    def test_a_failing_fadeout_is_not_waited_for(self):
        self.mixer.stuck = True
        self.mixer.music.fadeout = mock.Mock(side_effect=RuntimeError("fadeout が失敗"))
        with captured_logs() as found, self.assertRaises(PlaybackInterrupted):
            self.make_player().play([Segment(self.first)], stop=self.stop_at_sleep(1))
        self.assertEqual(self.clock.sleeps, [audio.POLL_SECONDS])  # フェードの待ちはしない
        self.assertEqual(self.mixer.calls[-2:], [("stop",), ("quit",)])
        self.assertEqual(len(found), 1)

    def test_a_get_busy_that_fails_while_waiting_for_the_fade_does_not_replace_the_interruption(self):
        def break_get_busy(name):
            if name == "fadeout":
                self.mixer.music.get_busy = mock.Mock(side_effect=RuntimeError("get_busy が壊れた"))

        self.mixer.fade = "nonblocking"
        self.mixer.on_call = break_get_busy
        self.mixer.stuck = True
        with captured_logs() as found, self.assertRaises(PlaybackInterrupted):
            self.make_player().play([Segment(self.first)], stop=self.stop_at_sleep(1))
        self.assertEqual(self.mixer.calls[-2:], [("stop",), ("quit",)])
        self.assertEqual([level for level, _ in found], ["WARNING"])
        self.assertIn("フェードアウト", found[0][1])
        self.assertIn("RuntimeError", found[0][1])

    def test_the_wait_for_the_fade_is_a_real_wait_although_the_stop_request_is_already_set(self):
        # 本物の時計と待ち（差し替えなし）。停止要求の Event は立っているので、Event.wait で待つと
        # 待たずに戻って空回りする（フェードを待たず、CPU も食う）。フェードの長さだけ実際に待ち、
        # そのあいだ get_busy を何千回も呼ばないこと。音楽は 5 秒で自然に終わる（固まらない）。
        music = RealTimeMusic()
        mixer = types.SimpleNamespace(
            init=lambda **kwargs: None, quit=lambda: None, get_init=lambda: None, music=music)
        stop = threading.Event()
        music.on_play = lambda: threading.Timer(0.1, stop.set).start()
        player = PygamePlayer(dict(AUDIO), mixer_module=mixer)
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.first)], stop=stop)
        faded = music.stopped_at - music.faded_at
        self.assertGreaterEqual(faded, audio.STOP_FADEOUT_MS / 1000.0 - 0.01)
        self.assertLess(faded, 5.0)
        self.assertLess(music.busy_calls, 200)

    def test_the_fade_is_not_waited_for_when_the_timeout_cuts_the_music(self):
        self.mixer.stuck = True
        self.mixer.fade = "nonblocking"
        with self.assertRaises(PlaybackError) as caught:
            self.make_player().play([Segment(self.first)])
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertNotIn("fadeout", self.mixer.names())


class PygamePlayerCleanupTest(PygamePlayerTestCase):
    """後始末（``music.stop`` と ``mixer.quit``）の失敗が、再生の結果を隠さない。"""

    def test_a_failing_quit_does_not_turn_an_interruption_into_another_error(self):
        self.mixer.stuck = True
        self.mixer.quit_error = RuntimeError("mixer not initialized")
        with captured_logs() as found, self.assertRaises(PlaybackInterrupted) as caught:
            self.make_player().play([Segment(self.first)], stop=self.stop_at_sleep(2))
        self.assertEqual((caught.exception.played, caught.exception.started), (0, True))
        self.assertEqual(self.mixer.names().count("quit"), 1)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], "WARNING")
        self.assertIn("mixer の終了に失敗しました", found[0][1])
        self.assertIn("RuntimeError: mixer not initialized", found[0][1])

    def test_a_failing_quit_does_not_fail_a_broadcast_that_was_fully_heard(self):
        self.mixer.quit_error = RuntimeError("mixer not initialized")
        with captured_logs() as found:
            self.assertEqual(self.make_player().play([Segment(self.first), Segment(self.second)]), 2)
        self.assertEqual(self.mixer.names().count("quit"), 1)
        self.assertEqual([level for level, _ in found], ["WARNING"])

    def test_a_failing_quit_does_not_hide_the_timeout(self):
        self.mixer.stuck = True
        self.mixer.quit_error = RuntimeError("mixer not initialized")
        with captured_logs(), self.assertRaises(PlaybackError) as caught:
            self.make_player().play([Segment(self.first)])
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertIn(self.first, str(caught.exception))

    def test_a_failing_quit_does_not_hide_the_error_that_stopped_the_playback(self):
        self.mixer.quit_error = RuntimeError("mixer not initialized")
        self.mixer.music.load = mock.Mock(side_effect=ValueError("壊れた音源"))
        with captured_logs(), self.assertRaises(ValueError):
            self.make_player().play([Segment(self.first)])

    def test_quit_is_called_even_when_stop_fails_and_both_failures_are_logged(self):
        self.mixer.quit_error = OSError("quit")
        self.mixer.music.stop = mock.Mock(side_effect=RuntimeError("stop"))
        with captured_logs() as found:
            self.assertEqual(self.make_player().play([Segment(self.first)]), 1)
        self.assertEqual(self.mixer.names().count("quit"), 1)
        self.assertEqual(len(found), 2)
        self.assertIn("再生の停止に失敗しました", found[0][1])
        self.assertIn("mixer の終了に失敗しました", found[1][1])

    def test_the_player_is_closed_for_good_even_when_quit_failed(self):
        self.mixer.quit_error = RuntimeError("quit")
        player = self.make_player()
        with captured_logs():
            player.play([Segment(self.first)])
            player.close()  # 2 回目の close は何もしない（quit を重ねて呼ばない）
        self.assertEqual(self.mixer.names().count("quit"), 1)


class PygamePlayerMixerInitTest(PygamePlayerTestCase):
    """``mixer.init`` に渡す引数。buffer=4096 などの値が実機の音切れ対策なので、固定しておく。"""

    DEFAULTS = {"frequency": 44100, "size": -16, "channels": 2, "buffer": 4096}

    def init_calls(self):
        return [call[1] for call in self.mixer.calls if call[0] == "init"]

    def test_without_a_mixer_section_the_defaults_are_used(self):
        player = PygamePlayer({"gap_ms": 0}, mixer_module=self.mixer,
                              sleep=self.clock.sleep, clock=self.clock.time)
        player.play([Segment(self.first)])
        self.assertEqual(self.init_calls(), [self.DEFAULTS])

    def test_the_default_config_has_the_same_values(self):
        self.assertEqual(DEFAULT_CONFIG["audio"]["mixer"], self.DEFAULTS)
        self.make_player().play([Segment(self.first)])
        self.assertEqual(self.init_calls(), [self.DEFAULTS])

    def test_values_from_the_section_are_passed_through(self):
        mixer = {"frequency": 22050, "size": -8, "channels": 1, "buffer": 512}
        self.make_player(mixer=mixer).play([Segment(self.first)])
        self.assertEqual(self.init_calls(), [mixer])

    def test_each_parameter_falls_back_to_its_own_default(self):
        for key, value in (("frequency", 48000), ("size", 16), ("channels", 1), ("buffer", 1024)):
            self.mixer = FakeMixer()
            self.mixer.clock = self.clock
            with self.subTest(key=key):
                self.make_player(mixer={key: value}).play([Segment(self.first)])
                self.assertEqual(self.init_calls(), [dict(self.DEFAULTS, **{key: value})])

    def test_the_parameters_are_integers_and_init_is_called_with_keywords_only(self):
        self.make_player(mixer={"frequency": 22050.0, "size": "-16", "channels": 2.0,
                                "buffer": "2048"}).play([Segment(self.first)])
        params = self.init_calls()[0]
        self.assertEqual(params, {"frequency": 22050, "size": -16, "channels": 2, "buffer": 2048})
        self.assertTrue(all(type(value) is int for value in params.values()))
        self.assertEqual(self.mixer.calls[0], ("init", params))

    def test_the_same_parameters_are_used_for_every_retry(self):
        self.mixer.init_failures = 2
        with captured_logs():
            self.make_player(mixer={"buffer": 2048}).play([Segment(self.first)])
        self.assertEqual(self.init_calls(), [dict(self.DEFAULTS, buffer=2048)] * 3)


class PlayerStopTest(unittest.TestCase):
    """``Player.play`` の停止要求（バックエンドに依らない部分）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.first = make_wav(os.path.join(self.tmp.name, "1.wav"))
        self.second = make_wav(os.path.join(self.tmp.name, "2.wav"))
        self.third = make_wav(os.path.join(self.tmp.name, "3.wav"))

    def test_play_accepts_stop_and_returns_the_count(self):
        player = RecordingPlayer(AUDIO)
        self.assertEqual(player.play([Segment(self.first)], stop=threading.Event()), 1)
        self.assertEqual(player.play([Segment(self.first)], None), 1)

    def test_a_stop_request_set_during_a_segment_stops_before_the_next(self):
        stop = threading.Event()

        class StopsAfterFirst(RecordingPlayer):
            def play_one(self, segment):
                super().play_one(segment)
                if len(self.played) == 1:
                    stop.set()

        player = StopsAfterFirst(AUDIO)
        with self.assertRaises(PlaybackInterrupted) as caught:
            player.play([Segment(self.first), Segment(self.second), Segment(self.third)], stop=stop)
        self.assertEqual(caught.exception.played, 1)
        self.assertEqual(player.played, [self.first])
        self.assertEqual((player.opened, player.closed), (1, 1))

    def test_a_stop_request_that_is_already_set_opens_nothing(self):
        stop = threading.Event()
        stop.set()
        player = RecordingPlayer(AUDIO)
        with self.assertRaises(PlaybackInterrupted) as caught:
            player.play([Segment(self.first)], stop=stop)
        self.assertEqual(caught.exception.played, 0)
        self.assertEqual((player.opened, player.closed, player.played), (0, 0, []))

    def test_a_missing_required_file_is_still_reported_first(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(PlaybackError) as caught:
            RecordingPlayer(AUDIO).play([Segment("/nonexistent.wav")], stop=stop)
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)

    def test_the_gap_wait_stops_at_once_and_counts_the_finished_segment(self):
        stop = threading.Event()
        timers = []

        class StopsDuringTheGap(RecordingPlayer):
            def play_one(self, segment):
                super().play_one(segment)
                timer = threading.Timer(0.1, stop.set)  # 間隔の待ちに入ってから 0.1 秒後
                timers.append(timer)
                timer.start()

        player = StopsDuringTheGap(dict(AUDIO, gap_ms=20000))
        started = time.monotonic()
        try:
            with self.assertRaises(PlaybackInterrupted) as caught:
                player.play([Segment(self.first), Segment(self.second)], stop=stop)
        finally:
            for timer in timers:
                timer.cancel()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(caught.exception.played, 1)
        self.assertEqual(player.played, [self.first])
        self.assertEqual(player.closed, 1)

    def test_the_gap_wait_without_a_stop_request_is_time_sleep(self):
        player = RecordingPlayer(dict(AUDIO, gap_ms=250))
        with mock.patch("chime.audio.time.sleep") as sleep:
            player.play([Segment(self.first), Segment(self.second)])
        sleep.assert_called_once_with(0.25)

    def test_started_is_false_when_the_stop_request_was_set_before_the_device_was_opened(self):
        stop = threading.Event()
        stop.set()
        player = RecordingPlayer(AUDIO)
        with self.assertRaises(PlaybackInterrupted) as caught:
            player.play([Segment(self.first)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (0, False))

    def test_started_is_false_when_the_stop_request_comes_before_the_first_segment_is_played(self):
        stop = threading.Event()

        class StopsWhileOpening(RecordingPlayer):
            def open(self):
                super().open()
                stop.set()

        player = StopsWhileOpening(AUDIO)
        with self.assertRaises(PlaybackInterrupted) as caught:
            player.play([Segment(self.first), Segment(self.second)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (0, False))
        self.assertEqual((player.opened, player.closed, player.played), (1, 1, []))

    def test_started_is_true_once_a_segment_has_been_played(self):
        # バックエンドが自分で印を付けなくても、play_one が戻れば「音を出した」。
        stop = threading.Event()

        class StopsAfterFirst(RecordingPlayer):
            def play_one(self, segment):
                super().play_one(segment)
                stop.set()

        with self.assertRaises(PlaybackInterrupted) as caught:
            StopsAfterFirst(AUDIO).play([Segment(self.first), Segment(self.second)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (1, True))

    def test_started_is_true_when_the_stop_request_comes_in_the_gap(self):
        stop = threading.Event()
        player = RecordingPlayer(dict(AUDIO, gap_ms=5))
        real_wait = player._wait

        def stop_then_wait(seconds):
            stop.set()
            return real_wait(seconds)

        player._wait = stop_then_wait
        with self.assertRaises(PlaybackInterrupted) as caught:
            player.play([Segment(self.first), Segment(self.second)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (1, True))

    def test_the_start_is_forgotten_between_plays(self):
        player = RecordingPlayer(AUDIO)
        self.assertEqual(player.play([Segment(self.first)]), 1)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(PlaybackInterrupted) as caught:
            player.play([Segment(self.first)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (0, False))

    def test_an_interruption_a_backend_raises_by_itself_is_taken_as_started(self):
        # 自分で PlaybackInterrupted を出すバックエンドは、played だけ数え直され、
        # started は（分からないので）出したものとして、そのまま。
        class Raises(RecordingPlayer):
            def play_one(self, segment):
                raise PlaybackInterrupted("自前の中断")

        with self.assertRaises(PlaybackInterrupted) as caught:
            Raises(AUDIO).play([Segment(self.first)])
        self.assertEqual((caught.exception.played, caught.exception.started), (0, True))

    def test_the_stop_request_is_forgotten_after_play(self):
        player = RecordingPlayer(AUDIO)
        player.play([Segment(self.first)], stop=threading.Event())
        self.assertIsNone(player._stop)
        stop = threading.Event()

        class Failing(RecordingPlayer):
            def play_one(self, segment):
                raise PlaybackError("失敗")

        failing = Failing(AUDIO)
        with self.assertRaises(PlaybackError):
            failing.play([Segment(self.first)], stop=stop)
        self.assertIsNone(failing._stop)


class MockPlayerStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = make_wav(os.path.join(self.tmp.name, "a.wav"), 5.0)

    def test_it_sleeps_the_shorter_of_the_duration_and_mock_max_seconds(self):
        player = MockPlayer(dict(AUDIO, mock_max_seconds=0.5))
        with mock.patch("chime.audio.time.sleep") as sleep:
            player.play([Segment(self.path)])
        sleep.assert_called_once_with(0.5)

    def test_the_duration_wins_when_it_is_shorter(self):
        short = make_wav(os.path.join(self.tmp.name, "short.wav"), 0.25)
        player = MockPlayer(dict(AUDIO, mock_max_seconds=3.0))
        with mock.patch("chime.audio.time.sleep") as sleep:
            player.play([Segment(short)])
        sleep.assert_called_once_with(0.25)

    def test_a_stop_request_ends_the_sleep_at_once(self):
        player = MockPlayer(dict(AUDIO, mock_max_seconds=30.0))
        stop = threading.Event()
        real_wait = stop.wait
        timers = []

        def wait_and_stop_soon(timeout=None):
            timer = threading.Timer(0.1, stop.set)  # 眠りに入ってから 0.1 秒後
            timers.append(timer)
            timer.start()
            return real_wait(timeout)

        stop.wait = wait_and_stop_soon
        started = time.monotonic()
        try:
            with self.assertRaises(PlaybackInterrupted) as caught:
                player.play([Segment(self.path), Segment(self.path)], stop=stop)
        finally:
            for timer in timers:
                timer.cancel()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(caught.exception.played, 0)

    def test_a_stop_request_that_is_already_set_does_not_sleep(self):
        stop = threading.Event()
        stop.set()
        with mock.patch("chime.audio.time.sleep") as sleep:
            with self.assertRaises(PlaybackInterrupted):
                MockPlayer(AUDIO).play([Segment(self.path)], stop=stop)
        sleep.assert_not_called()

    def test_started_is_false_before_the_first_segment_and_true_during_it(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(PlaybackInterrupted) as caught:
            MockPlayer(AUDIO).play([Segment(self.path)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (0, False))

        with mock.patch.object(Player, "_wait", return_value=True):  # 眠っている間に停止要求
            with self.assertRaises(PlaybackInterrupted) as caught:
                MockPlayer(AUDIO).play([Segment(self.path)], stop=threading.Event())
        self.assertEqual((caught.exception.played, caught.exception.started), (0, True))

    def test_the_second_segment_is_not_played_after_a_stop(self):
        stop = threading.Event()
        player = MockPlayer(dict(AUDIO, mock_max_seconds=0.01))
        with logs_enabled(), self.assertLogs("chime.audio", level="INFO") as captured:
            with mock.patch.object(Player, "_wait", side_effect=[False, True]):
                with self.assertRaises(PlaybackInterrupted) as caught:
                    player.play([Segment(self.path, label="一"), Segment(self.path, label="二"),
                                 Segment(self.path, label="三")], stop=stop)
        self.assertEqual(caught.exception.played, 1)
        shown = [record.getMessage() for record in captured.records if "[MOCK]" in record.getMessage()]
        self.assertEqual(len(shown), 2)

    def test_the_log_line_is_unchanged_at_full_volume_and_shows_a_reduced_volume(self):
        player = MockPlayer(dict(AUDIO, volume={"time_signal": 1.0, "voice": 0.5, "music": 1.0}))
        with logs_enabled(), self.assertLogs("chime.audio", level="INFO") as captured:
            player.play([Segment(self.path, label="声", kind="voice"),
                         Segment(self.path, label="曲", kind="music")])
        shown = [record.getMessage() for record in captured.records if "[MOCK]" in record.getMessage()]
        self.assertEqual(shown, ["[MOCK] 声（約 5.0 秒 / fade_in=0ms / 音量 0.50）",
                                 "[MOCK] 曲（約 5.0 秒 / fade_in=0ms）"])


def spy_on_popen(testcase):
    """``subprocess.Popen`` で起動された子プロセスを、起動順に集める（本物の Popen のまま）。

    ``testcase.on_popen`` があれば、子を起こした直後に ``on_popen(process)`` を呼ぶ
    （停止要求を「子が動き出してから」立てるための掛け金）。
    """
    created = []
    real = subprocess.Popen

    def spy(*args, **kwargs):
        process = real(*args, **kwargs)
        created.append(process)
        hook = getattr(testcase, "on_popen", None)
        if hook is not None:
            hook(process)
        return process

    patcher = mock.patch("chime.audio.subprocess.Popen", spy)
    patcher.start()
    testcase.addCleanup(patcher.stop)

    def reap():
        for process in created:
            if process.poll() is None:
                process.kill()
            process.wait()

    testcase.addCleanup(reap)
    return created


SLEEPS_FOR_30_SECONDS = "import time; time.sleep(30)"
#: SIGTERM を無視して寝る子。準備ができたら ``{ready}`` というファイルを作る。
IGNORES_SIGTERM = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[2], 'w').close()\n"
    "time.sleep(30)\n")


class ReapedChildMixin(object):
    """実際の子プロセスを起こすテスト用。"""

    def assert_reaped(self, process):
        """子が回収済み（ゾンビで残っていない）こと。回収済みなら、もう一度 waitpid しても子がいない。"""
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)


class CommandPlayerProcessTest(ReapedChildMixin, WarnOnceTestCase):
    """実際の子プロセス（``python -c ...``）で、Popen と待ちのループを確かめる。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mp3 = os.path.join(self.tmp.name, "a.mp3")
        with open(self.mp3, "wb") as handle:
            handle.write(b"x")
        self.processes = spy_on_popen(self)

    def make_player(self, code, **settings):
        commands = {".mp3": [sys.executable, "-c", code, "{path}"]}
        return CommandPlayer(dict(AUDIO, commands=commands, **settings))

    def stop_after_start(self, seconds):
        """子を起こして ``seconds`` 秒後に立つ停止要求を返す。"""
        stop = threading.Event()

        def start_timer(process):
            timer = threading.Timer(seconds, stop.set)
            self.addCleanup(timer.cancel)
            timer.start()

        self.on_popen = start_timer
        return stop

    def test_a_command_that_exits_normally_is_one_played_segment(self):
        self.assertEqual(self.make_player("pass").play([Segment(self.mp3)]), 1)
        self.assertEqual(self.processes[0].returncode, 0)

    def test_the_command_gets_the_path(self):
        out = os.path.join(self.tmp.name, "argv.txt")
        code = "import sys; open(sys.argv[2], 'w').write(sys.argv[1])"
        player = CommandPlayer(dict(AUDIO, commands={".mp3": [sys.executable, "-c", code,
                                                              "{path}", out]}))
        player.play([Segment(self.mp3)])
        with open(out, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.mp3)

    def test_a_non_zero_exit_is_a_playback_error_with_the_exit_code(self):
        with self.assertRaises(PlaybackError) as caught:
            self.make_player("import sys; sys.exit(3)").play_one(Segment(self.mp3))
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertIn("終了コード 3", str(caught.exception))

    def test_a_command_that_cannot_be_started_is_a_playback_error(self):
        player = CommandPlayer(dict(AUDIO, commands={".mp3": ["/nonexistent/aplay", "{path}"]}))
        with mock.patch("chime.audio.env.has_command", return_value=True), \
                self.assertRaises(PlaybackError) as caught:
            player.play_one(Segment(self.mp3))
        self.assertIn("起動できませんでした", str(caught.exception))
        self.assertIn("/nonexistent/aplay", str(caught.exception))
        self.assertEqual(self.processes, [])

    def test_a_stop_request_terminates_the_child_and_raises(self):
        player = self.make_player(SLEEPS_FOR_30_SECONDS)
        stop = self.stop_after_start(0.3)
        started = time.monotonic()
        with self.assertRaises(PlaybackInterrupted) as caught:
            player.play([Segment(self.mp3)], stop=stop)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((caught.exception.played, caught.exception.started), (0, True))
        self.assertEqual(self.processes[0].returncode, -signal.SIGTERM)
        self.assert_reaped(self.processes[0])

    def test_a_stop_request_kills_a_child_that_ignores_sigterm_after_the_grace_period(self):
        ready = os.path.join(self.tmp.name, "ready")
        commands = {".mp3": [sys.executable, "-c", IGNORES_SIGTERM, "{path}", ready]}
        player = CommandPlayer(dict(AUDIO, commands=commands))
        player.terminate_grace_seconds = 0.4
        stop = threading.Event()

        def stop_when_ready():
            deadline = time.monotonic() + 20
            while not os.path.exists(ready) and time.monotonic() < deadline:
                time.sleep(0.02)
            stop.set()

        helper = threading.Thread(target=stop_when_ready)
        helper.start()
        self.addCleanup(helper.join)
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)
        self.assert_reaped(self.processes[0])

    def test_the_default_grace_period_is_two_seconds(self):
        self.assertEqual(self.make_player("pass").terminate_grace_seconds, 2.0)

    def test_a_command_that_runs_too_long_is_terminated_and_reported_with_the_file(self):
        player = self.make_player(SLEEPS_FOR_30_SECONDS, max_segment_seconds=0.3)
        started = time.monotonic()
        with self.assertRaises(PlaybackError) as caught:
            player.play([Segment(self.mp3)])
        self.assertLess(time.monotonic() - started, 10)
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertIn(self.mp3, str(caught.exception))
        self.assertEqual(self.processes[0].returncode, -signal.SIGTERM)

    def test_a_timed_out_child_that_ignores_sigterm_is_killed(self):
        ready = os.path.join(self.tmp.name, "ready")
        commands = {".mp3": [sys.executable, "-c", IGNORES_SIGTERM, "{path}", ready]}

        def clock():
            # 子が SIGTERM を無視する準備を終えたら、上限を一気に越える。
            return time.monotonic() + (1000.0 if os.path.exists(ready) else 0.0)

        player = CommandPlayer(dict(AUDIO, commands=commands, max_segment_seconds=60), clock=clock)
        player.terminate_grace_seconds = 0.4
        with self.assertRaises(PlaybackError) as caught:
            player.play([Segment(self.mp3)])
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)

    def test_the_timeout_is_not_applied_to_a_command_that_finishes_in_time(self):
        player = self.make_player("import time; time.sleep(0.2)", max_segment_seconds=20)
        self.assertEqual(player.play([Segment(self.mp3)]), 1)

    def test_the_child_is_not_left_running_when_the_wait_is_broken_off(self):
        player = CommandPlayer(
            dict(AUDIO, commands={".mp3": [sys.executable, "-c", SLEEPS_FOR_30_SECONDS, "{path}"]}),
            sleep=mock.Mock(side_effect=KeyboardInterrupt))
        with self.assertRaises(KeyboardInterrupt):
            player.play([Segment(self.mp3)])
        self.assertIsNotNone(self.processes[0].poll())

    def test_a_stop_between_two_commands_does_not_start_the_second(self):
        player = self.make_player("pass")
        stop = threading.Event()
        real_play_one = player.play_one

        def play_one_then_stop(segment):
            real_play_one(segment)
            stop.set()

        player.play_one = play_one_then_stop
        with self.assertRaises(PlaybackInterrupted) as caught:
            player.play([Segment(self.mp3), Segment(self.mp3)], stop=stop)
        self.assertEqual(caught.exception.played, 1)
        self.assertEqual(len(self.processes), 1)

    def test_volume_other_than_1_warns_once_per_process(self):
        player = self.make_player("pass", volume={"time_signal": 1.0, "voice": 0.5, "music": 1.0})
        with captured_logs() as found:
            player.play([Segment(self.mp3), Segment(self.mp3)])
            player.play([Segment(self.mp3)])
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0][0], "WARNING")
        self.assertIn("command 方式では音量を変えられません", found[0][1])
        self.assertEqual(len(self.processes), 3)  # 警告しても鳴らす

    def test_full_volume_does_not_warn(self):
        with captured_logs() as found:
            self.make_player("pass").play([Segment(self.mp3, kind="music")])
        self.assertEqual(found, [])

    def test_volume_of_a_kind_that_is_not_played_does_not_warn(self):
        player = self.make_player("pass", volume={"time_signal": 1.0, "voice": 1.0, "music": 0.2})
        with captured_logs() as found:
            player.play([Segment(self.mp3, kind="voice")])
        self.assertEqual(found, [])

    @unittest.skipUnless(shutil.which("true"), "true コマンドが必要")
    def test_ten_instant_commands_are_not_padded_up_to_the_poll_interval(self):
        # 終わった子は、次の確認（50 ms 先）を待たずに、すぐ次のセグメントへ進む。
        # 以前の poll + sleep では、1 つごとに最大 50 ms 余計にかかり、10 個で約 0.5 秒だった。
        player = CommandPlayer(dict(AUDIO, commands={".mp3": ["true"]}))
        started = time.monotonic()
        self.assertEqual(player.play([Segment(self.mp3) for _ in range(10)]), 10)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(len(self.processes), 10)

    def test_the_child_leads_its_own_process_group_and_session(self):
        # 停止のときにグループごと止められるよう、子は自分のグループ（と、ターミナルを持たない
        # 自分のセッション）の先頭として起動する。
        out = os.path.join(self.tmp.name, "ids.txt")
        code = ("import os, sys\n"
                "open(sys.argv[2], 'w').write('%d %d %d' % (os.getpid(), os.getpgrp(), os.getsid(0)))\n")
        player = CommandPlayer(dict(AUDIO, commands={".mp3": [sys.executable, "-c", code, "{path}", out]}))
        player.play([Segment(self.mp3)])
        with open(out, encoding="utf-8") as handle:
            pid, group, session = [int(part) for part in handle.read().split()]
        self.assertEqual((group, session), (pid, pid))
        self.assertNotEqual(group, os.getpgrp())


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.isdir("/proc") and shutil.which("sh"),
                     "Linux の /proc と sh が必要")
class CommandPlayerWrapperTest(ReapedChildMixin, WarnOnceTestCase):
    """ラッパー（``sh -c "..."``）の孫まで止める。止めたはずの再生コマンドが残ると、音声デバイスを
    握ったままになり、次の放送がその上に重なる。

    孫は ``sleep <目印>``。目印の秒数は、このテストプロセスの pid から作り（ほかのテストや
    ほかの ``sleep`` と混ざらない）、``/proc`` から argv が完全に一致するものを探す。
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mp3 = os.path.join(self.tmp.name, "a.mp3")
        with open(self.mp3, "wb") as handle:
            handle.write(b"x")
        self.marker = "41.%d" % os.getpid()
        self.addCleanup(self.kill_leftovers)
        self.processes = spy_on_popen(self)

    def sleepers(self):
        found = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as handle:
                    argv = handle.read().split(b"\0")[:-1]
            except OSError:
                continue
            if argv == [b"sleep", self.marker.encode()]:
                found.append(int(entry))
        return found

    def kill_leftovers(self):
        for pid in self.sleepers():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def assert_no_sleeper_left(self):
        # 信号は非同期に届くので、消えるまで少し待つ（最長 3 秒。残っていれば落ちる）。
        deadline = time.monotonic() + 3
        while self.sleepers() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.sleepers(), [])

    def make_player(self, script, **settings):
        return CommandPlayer(dict(AUDIO, commands={".mp3": ["sh", "-c", script]}, **settings))

    def wait_for_sleeper(self):
        deadline = time.monotonic() + 5
        while not self.sleepers() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotEqual(self.sleepers(), [], "孫の sleep が起動しなかった")

    def stop_when_the_grandchild_runs(self, delay=0.0):
        """孫の ``sleep`` が動き出して ``delay`` 秒後に立つ停止要求を返す。

        孫が動き出さなかったときも停止要求は立てる（立てないと、孫の ``sleep`` が終わるまで
        再生が戻らず、テストが固まる）。
        """
        stop = threading.Event()

        def run():
            try:
                self.wait_for_sleeper()
                time.sleep(delay)
            finally:
                stop.set()

        helper = threading.Thread(target=run)
        helper.start()
        self.addCleanup(helper.join)
        return stop

    def test_a_stop_request_leaves_no_process_behind(self):
        player = self.make_player("sleep %s; echo" % self.marker)
        stop = self.stop_when_the_grandchild_runs()
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        self.assertEqual(self.processes[0].returncode, -signal.SIGTERM)
        self.assert_no_sleeper_left()

    def test_a_timeout_leaves_no_process_behind(self):
        player = self.make_player("sleep %s; echo" % self.marker, max_segment_seconds=0.5)
        with self.assertRaises(PlaybackError) as caught:
            player.play([Segment(self.mp3)])
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assert_no_sleeper_left()

    def test_a_wrapper_that_ignores_sigterm_is_killed_with_its_child_after_the_grace_period(self):
        player = self.make_player("trap '' TERM; sleep %s; echo" % self.marker)
        player.terminate_grace_seconds = 0.3
        stop = self.stop_when_the_grandchild_runs(delay=0.1)  # sh が trap を済ませたあとに止める
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)
        self.assert_no_sleeper_left()

    # ラッパー（``sh``）は SIGTERM で終わるが、孫の再生コマンドが SIGTERM を無視する形。
    # ``exec`` までに ``trap`` が済むので、``sleep <目印>`` が見えれば孫は SIGTERM を無視している。
    @property
    def orphan_wrapper(self):
        return "(trap '' TERM; exec sleep %s) & wait" % self.marker

    def test_a_stop_request_kills_a_grandchild_that_ignores_sigterm_after_the_wrapper_exited(self):
        player = self.make_player(self.orphan_wrapper)
        player.terminate_grace_seconds = 0.5
        stop = self.stop_when_the_grandchild_runs()
        started = time.monotonic()
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.processes[0].returncode, -signal.SIGTERM)  # ラッパーは SIGTERM で終わった
        self.assert_reaped(self.processes[0])
        self.assert_no_sleeper_left()

    def test_a_timeout_kills_a_grandchild_that_ignores_sigterm_after_the_wrapper_exited(self):
        player = self.make_player(self.orphan_wrapper, max_segment_seconds=0.5)
        player.terminate_grace_seconds = 0.5
        with self.assertRaises(PlaybackError) as caught:
            player.play([Segment(self.mp3)])
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertEqual(self.processes[0].returncode, -signal.SIGTERM)
        self.assert_reaped(self.processes[0])
        self.assert_no_sleeper_left()

    def test_the_grandchild_gets_the_whole_grace_period_to_finish_on_its_own(self):
        # 孫は SIGTERM を受けて 0.4 秒かけて後始末をし、自分で終わる。猶予（5 秒）のうちなので SIGKILL
        # で切らない（後始末の印のファイルが残る）。
        done = os.path.join(self.tmp.name, "done")
        script = ("(trap 'sleep 0.4; echo finished > \"$0\"; exit 0' TERM; sleep %s & wait) & wait"
                  % self.marker)
        player = CommandPlayer(dict(AUDIO, commands={".mp3": ["sh", "-c", script, done]}))
        player.terminate_grace_seconds = 5
        stop = self.stop_when_the_grandchild_runs(delay=0.2)  # 孫の sh が trap を済ませたあとに止める
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        with open(done, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "finished")
        self.assert_no_sleeper_left()

    def test_a_group_that_is_gone_does_not_make_the_stop_wait_for_the_whole_grace_period(self):
        player = self.make_player("sleep %s; echo" % self.marker)
        player.terminate_grace_seconds = 20
        stop = self.stop_when_the_grandchild_runs()
        started = time.monotonic()
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        self.assertLess(time.monotonic() - started, 10)
        self.assert_no_sleeper_left()


class FakeChild(object):
    """``subprocess.Popen`` が返す子の偽物。実在しない pid（``os.killpg`` は必ず差し替えて使う）。

    ``returncode``: 終わっていれば終了コード（``None`` は動作中）。``on_first_poll(child)``: 最初の
    ``poll`` の中で呼ぶ（そこで停止要求を立てたり、子を終わらせたりする）。``exits_on_wait``:
    ``wait`` に入ると終わる子。``ignores_sigterm``: SIGTERM では終わらない。``wait_error``: 最初の
    ``wait`` が出す例外。``wait`` に渡された秒数は ``waits``、直接送られた信号は ``direct`` に残す。
    ``poll`` が ``max_polls`` 回、``wait`` が ``max_waits`` 回を超える・終わらない子を期限なしで待つと
    ``AssertionError``（退行して待ちが終わらなくなっても、テストが固まらずに落ちる）。

    グループの様子: ``orphan``: 子が終わったあとも、SIGTERM を無視する孫がグループに残る（SIGKILL
    で消える）。``orphan_until``: 孫が（時計がこの時刻になって）自分で終わる。``signal_group`` は
    ``os.killpg`` の代わり: 信号 0 は、グループが空なら ``ProcessLookupError``、残っていれば何も
    返さない。その回数は ``probes``、``max_probes`` を超えると ``AssertionError``。
    """

    pid = 2 ** 30  # pid の上限（2 ** 22）より大きく、実在しない

    def __init__(self, clock, returncode=None, on_first_poll=None, exits_on_wait=False,
                 ignores_sigterm=False, wait_error=None, max_waits=60, max_polls=60,
                 orphan=False, orphan_until=None, max_probes=60):
        self.clock = clock
        self.orphan = orphan
        self.orphan_until = orphan_until
        self.max_probes = max_probes
        self.probes = 0
        self.returncode = returncode
        self.on_first_poll = on_first_poll
        self.exits_on_wait = exits_on_wait
        self.ignores_sigterm = ignores_sigterm
        self.wait_error = wait_error
        self.max_waits = max_waits
        self.max_polls = max_polls
        self.polls = 0
        self.waits = []
        self.direct = []

    def poll(self):
        self.polls += 1
        if self.polls > self.max_polls:
            raise AssertionError("確認が終わらない（poll が {0} 回を超えた）".format(self.max_polls))
        if self.on_first_poll is not None:
            hook, self.on_first_poll = self.on_first_poll, None
            hook(self)
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) > self.max_waits:
            raise AssertionError("待ちが終わらない（wait が {0} 回を超えた）".format(self.max_waits))
        if self.wait_error is not None:
            error, self.wait_error = self.wait_error, None
            raise error
        if self.exits_on_wait and self.returncode is None:
            self.returncode = 0
        if self.returncode is None:
            if timeout is None:
                raise AssertionError("終わらない子を、期限なしで待った")
            self.clock.now += timeout
            raise subprocess.TimeoutExpired("fake-player", timeout)
        return self.returncode

    def group_alive(self):
        """グループにメンバーがいるか（動いている子、または SIGTERM を無視して残った孫）。"""
        if self.returncode is None:
            return True
        if not self.orphan:
            return False
        return self.orphan_until is None or self.clock.now < self.orphan_until

    def signal_group(self, signum):
        """``os.killpg(pid, signum)`` の代わり。信号 0 は、宛先があるかの確認だけ。"""
        if signum == 0:
            self.probes += 1
            if self.probes > self.max_probes:
                raise AssertionError("グループの確認が終わらない（信号 0 が {0} 回を超えた）".format(self.max_probes))
            if not self.group_alive():
                raise ProcessLookupError()
            return
        self.deliver(signum)

    def deliver(self, signum):
        """信号を受け取る（グループへ送られたものも、直接送られたものも同じ）。"""
        if signum == signal.SIGKILL:
            self.orphan = False
            if self.returncode is None:   # 終わって回収された子の終了コードは変わらない
                self.returncode = -signal.SIGKILL
        elif signum == signal.SIGTERM and not self.ignores_sigterm:
            self.returncode = -signal.SIGTERM

    def terminate(self):
        self.direct.append("TERM")
        self.deliver(signal.SIGTERM)

    def kill(self):
        self.direct.append("KILL")
        self.deliver(signal.SIGKILL)


class CommandPlayerFakeChildTest(WarnOnceTestCase):
    """偽の子で、終了の判定・待ち方・信号の送り方を確かめる（実プロセスでは作れない競合も）。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mp3 = os.path.join(self.tmp.name, "a.mp3")
        with open(self.mp3, "wb") as handle:
            handle.write(b"x")
        self.clock = FakeClock()
        self.killpg = self.patch("chime.audio.os.killpg")  # 本物のグループには絶対に送らない
        self.patch("chime.audio.env.has_command", return_value=True)
        # 待ちは実時間では待たず、偽の時計を進める（グループの確認が終わらなくても固まらない）。
        self.patch("chime.audio.time.sleep", side_effect=self.clock.sleep)
        self.child = None

    def patch(self, target, *args, **kwargs):
        patcher = mock.patch(target, *args, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def start(self, child, **settings):
        """``child`` を返す偽の Popen を差して、それを鳴らすプレーヤーを返す。"""
        self.child = child
        self.popen = self.patch("chime.audio.subprocess.Popen", return_value=child)
        self.killpg.side_effect = lambda group, signum: child.signal_group(signum)
        commands = {".mp3": ["fake-player", "{path}"]}
        return CommandPlayer(dict(AUDIO, commands=commands, **settings), clock=self.clock.time)

    def child_of(self, **kwargs):
        return FakeChild(self.clock, **kwargs)

    @staticmethod
    def to_group(signum):
        """``os.killpg`` が、子のグループへ ``signum`` を送る呼び出し。"""
        return mock.call(FakeChild.pid, signum)

    # -- 終了コードと停止要求 -----------------------------------------------
    def test_a_child_killed_by_the_same_sigterm_as_ours_is_an_interruption(self):
        # systemd（KillMode=control-group）や Ctrl+C は、python と子に同じ SIGTERM を送る。
        # 子が先に死んで、ループが普通に抜けても、停止要求があったのだから中断。
        stop = threading.Event()

        def sigterm_reaches_both(child):
            stop.set()
            child.returncode = -signal.SIGTERM

        player = self.start(self.child_of(on_first_poll=sigterm_reaches_both))
        with self.assertRaises(PlaybackInterrupted) as caught:
            player.play([Segment(self.mp3)], stop=stop)
        self.assertEqual((caught.exception.played, caught.exception.started), (0, True))
        self.assertIn("a.mp3", str(caught.exception))
        self.killpg.assert_not_called()  # もう死んでいる子には何も送らない
        self.assertEqual(self.child.direct, [])

    def test_any_non_zero_exit_while_a_stop_was_requested_is_an_interruption(self):
        for code in (1, 2, 143, -signal.SIGKILL, -signal.SIGINT):
            stop = threading.Event()

            def exits(child, code=code, stop=stop):
                stop.set()
                child.returncode = code

            with self.subTest(code=code):
                player = self.start(self.child_of(on_first_poll=exits))
                with self.assertRaises(PlaybackInterrupted):
                    player.play([Segment(self.mp3)], stop=stop)

    def test_a_non_zero_exit_without_a_stop_request_is_still_a_playback_error(self):
        for code in (1, 3, -signal.SIGSEGV):
            for stop in (None, threading.Event()):  # 停止要求を渡さない・渡したが立っていない
                def exits(child, code=code):
                    child.returncode = code

                with self.subTest(code=code, stop=stop):
                    player = self.start(self.child_of(on_first_poll=exits))
                    with self.assertRaises(PlaybackError) as caught:
                        player.play([Segment(self.mp3)], stop=stop)
                    self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
                    self.assertIn("終了コード {0}".format(code), str(caught.exception))
                    self.assertIn("fake-player", str(caught.exception))

    def test_a_child_that_finished_normally_is_a_played_segment_even_if_a_stop_was_requested(self):
        stop = threading.Event()

        def finishes_as_the_stop_comes(child):
            stop.set()
            child.returncode = 0

        player = self.start(self.child_of(on_first_poll=finishes_as_the_stop_comes))
        self.assertEqual(player.play([Segment(self.mp3)], stop=stop), 1)

    # -- 待ち方 --------------------------------------------------------------
    def test_an_exiting_child_ends_the_wait_at_once_through_process_wait(self):
        # 待ちは process.wait(timeout=POLL_SECONDS)。子が終わればその場で戻る。sleep で
        # 50 ms 刻みに待つ作りだと、終わった子をそのぶん遅れて気づく。
        for stop in (None, threading.Event()):
            with self.subTest(stop=stop), mock.patch("chime.audio.time.sleep", side_effect=AssertionError):
                player = self.start(self.child_of(exits_on_wait=True))
                children = []

                def next_child(*args, **kwargs):
                    children.append(self.child_of(exits_on_wait=True))
                    return children[-1]

                self.popen.side_effect = next_child
                self.assertEqual(player.play([Segment(self.mp3), Segment(self.mp3)], stop=stop), 2)
                self.assertEqual([child.waits for child in children], [[audio.POLL_SECONDS]] * 2)

    def test_the_wait_is_cut_into_poll_intervals_so_a_stop_request_is_seen_in_time(self):
        stop = threading.Event()
        child = self.child_of()
        child.max_waits = 10
        player = self.start(child)

        original = child.wait

        def wait_then_stop_at_the_third(timeout=None):
            if len(child.waits) == 2:
                stop.set()
            return original(timeout)

        child.wait = wait_then_stop_at_the_third
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        self.assertEqual(child.waits[:3], [audio.POLL_SECONDS] * 3)
        self.assertEqual(child.waits[3:], [player.terminate_grace_seconds])  # 以後は止める待ちだけ

    def test_an_injected_sleep_replaces_the_wait_for_the_child(self):
        slept = []
        child = self.child_of()
        player = self.start(child)
        player._sleep = lambda seconds: (slept.append(seconds), setattr(child, "returncode", 0))
        self.assertEqual(player.play([Segment(self.mp3)]), 1)
        self.assertEqual(slept, [audio.POLL_SECONDS])
        self.assertEqual(child.waits, [])

    def test_a_child_that_runs_too_long_is_stopped_through_the_group(self):
        player = self.start(self.child_of(), max_segment_seconds=1)
        with self.assertRaises(PlaybackError) as caught:
            player.play([Segment(self.mp3)])
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertIn(self.mp3, str(caught.exception))
        self.assertEqual(set(self.child.waits[:-1]), {audio.POLL_SECONDS})
        self.assertEqual(self.child.waits[-1], player.terminate_grace_seconds)
        self.assertGreaterEqual(self.clock.now, 1.0)
        self.assertEqual(self.killpg.call_args_list, [self.to_group(signal.SIGTERM), self.to_group(0)])

    # -- 起動 ----------------------------------------------------------------
    def test_the_child_is_started_as_a_session_leader(self):
        player = self.start(self.child_of(returncode=0))
        player.play([Segment(self.mp3)])
        self.popen.assert_called_once_with(["fake-player", self.mp3], start_new_session=True)

    def test_the_time_limit_is_decided_before_the_child_is_started(self):
        order = []
        player = self.start(self.child_of(returncode=0))
        self.popen.side_effect = lambda *args, **kwargs: (order.append("popen"), self.child)[1]
        real = audio.segment_time_limit

        def spy(segment, settings):
            order.append("limit")
            return real(segment, settings)

        with mock.patch.object(audio, "segment_time_limit", spy):
            player.play([Segment(self.mp3)])
        self.assertEqual(order, ["limit", "popen"])

        order.clear()
        with mock.patch.object(audio, "segment_time_limit", side_effect=RuntimeError("limit")):
            with self.assertRaises(RuntimeError):
                player.play([Segment(self.mp3)])
        self.assertEqual(order, [])  # 上限を決められなければ、起動しない

    # -- 止め方 --------------------------------------------------------------
    def stop_at_first_poll(self, **kwargs):
        stop = threading.Event()
        child = self.child_of(on_first_poll=lambda child: stop.set(), **kwargs)
        return self.start(child), stop

    def test_a_stop_request_sends_sigterm_to_the_whole_group_and_reaps_the_child(self):
        player, stop = self.stop_at_first_poll()
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        # 子が終わったあと、グループが空かを 1 度確かめる（空なので、待たずに戻る）。
        self.assertEqual(self.killpg.call_args_list, [self.to_group(signal.SIGTERM), self.to_group(0)])
        self.assertEqual(self.child.direct, [])
        self.assertEqual(self.child.waits, [player.terminate_grace_seconds])
        self.assertEqual(self.child.returncode, -signal.SIGTERM)
        self.assertEqual(self.clock.sleeps, [])

    def test_a_group_that_ignores_sigterm_gets_sigkill_after_the_grace_period_and_is_reaped(self):
        player, stop = self.stop_at_first_poll(ignores_sigterm=True)
        player.terminate_grace_seconds = 0.5
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        self.assertEqual(self.killpg.call_args_list, [
            mock.call(FakeChild.pid, signal.SIGTERM), mock.call(FakeChild.pid, signal.SIGKILL)])
        self.assertEqual(self.child.waits, [0.5, None])  # 猶予を待ち、kill のあとは回収まで待つ
        self.assertEqual(self.child.returncode, -signal.SIGKILL)
        self.assertEqual(self.child.direct, [])

    # -- 子が先に終わっても、グループに残った孫を止める --------------------------------
    def test_a_grandchild_that_ignores_sigterm_gets_sigkill_after_the_grace_period_although_the_child_exited(self):
        player, stop = self.stop_at_first_poll(orphan=True)
        player.terminate_grace_seconds = 0.5
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        calls = self.killpg.call_args_list
        self.assertEqual((calls[0], calls[-1]), (self.to_group(signal.SIGTERM), self.to_group(signal.SIGKILL)))
        self.assertEqual(calls[1:-1], [self.to_group(0)] * (len(calls) - 2))   # 間は、グループが空かの確認だけ
        self.assertGreater(len(calls), 3)
        self.assertEqual(self.child.returncode, -signal.SIGTERM)  # 子は SIGTERM で終わり、回収されている
        self.assertEqual(self.child.waits, [0.5, None])
        self.assertEqual(self.child.direct, [])                   # 回収済みの子には直接送らない
        self.assertFalse(self.child.group_alive())
        # 確認の間隔は POLL_SECONDS。猶予をちょうど使い切ってから送る（早く切らない）。
        self.assertLessEqual(max(self.clock.sleeps), audio.POLL_SECONDS)
        self.assertAlmostEqual(sum(self.clock.sleeps), 0.5)
        self.assertAlmostEqual(self.clock.now, 0.5)

    def test_a_grandchild_that_ends_within_the_grace_period_is_not_killed(self):
        player, stop = self.stop_at_first_poll(orphan=True, orphan_until=0.2)
        player.terminate_grace_seconds = 5
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        calls = self.killpg.call_args_list
        self.assertEqual(calls[0], self.to_group(signal.SIGTERM))
        self.assertEqual(calls[1:], [self.to_group(0)] * (len(calls) - 1))   # SIGKILL は送らない
        self.assertGreater(len(calls), 2)
        self.assertEqual(self.child.waits, [5])
        self.assertAlmostEqual(self.clock.now, 0.2)               # 空になったその場で戻る

    def test_a_grandchild_that_ignores_sigterm_after_the_child_exited_in_time_is_killed_on_timeout_too(self):
        child = self.child_of(orphan=True)
        player = self.start(child, max_segment_seconds=1)
        player.terminate_grace_seconds = 0.5
        with self.assertRaises(PlaybackError) as caught:
            player.play([Segment(self.mp3)])
        self.assertNotIsInstance(caught.exception, PlaybackInterrupted)
        self.assertEqual(self.killpg.call_args_list[-1], self.to_group(signal.SIGKILL))
        self.assertFalse(child.group_alive())

    def test_a_group_that_cannot_be_checked_ends_the_wait_without_a_kill(self):
        # 権限がない（EPERM）などで確かめられないとき。待っても分からないので、待たず、SIGKILL も送らない。
        for error in (PermissionError(), OSError("EINVAL")):
            with self.subTest(error=type(error).__name__):
                self.killpg.reset_mock()
                player, stop = self.stop_at_first_poll(orphan=True)
                child = self.child

                def cannot_check(group, signum, child=child, error=error):
                    if signum == 0:
                        raise error
                    return child.signal_group(signum)

                self.killpg.side_effect = cannot_check
                with self.assertRaises(PlaybackInterrupted):
                    player.play([Segment(self.mp3)], stop=stop)
                self.assertEqual(self.killpg.call_args_list, [self.to_group(signal.SIGTERM), self.to_group(0)])
                self.assertEqual(child.waits, [player.terminate_grace_seconds])

    def test_a_group_that_vanished_just_before_the_kill_is_not_an_error(self):
        player, stop = self.stop_at_first_poll(orphan=True)
        player.terminate_grace_seconds = 0.5
        child = self.child

        def vanishes_before_sigkill(group, signum):
            if signum == signal.SIGKILL:
                raise ProcessLookupError()
            return child.signal_group(signum)

        self.killpg.side_effect = vanishes_before_sigkill
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        self.assertEqual(child.direct, [])   # 回収済みの子には、直接も送らない（pid の再利用）

    def test_a_child_still_running_when_the_group_cannot_be_killed_is_killed_directly(self):
        player, stop = self.stop_at_first_poll(ignores_sigterm=True)
        player.terminate_grace_seconds = 0.5
        child = self.child

        def refuses_sigkill(group, signum):
            if signum == signal.SIGKILL:
                raise PermissionError()
            return child.signal_group(signum)

        self.killpg.side_effect = refuses_sigkill
        with self.assertRaises(PlaybackInterrupted):
            player.play([Segment(self.mp3)], stop=stop)
        self.assertEqual(child.direct, ["KILL"])
        self.assertEqual(child.returncode, -signal.SIGKILL)

    def test_when_the_group_cannot_be_signalled_the_child_alone_is_stopped(self):
        for error in (ProcessLookupError(), PermissionError(), OSError("EINVAL")):
            for ignores in (False, True):
                with self.subTest(error=type(error).__name__, ignores_sigterm=ignores):
                    player, stop = self.stop_at_first_poll(ignores_sigterm=ignores)
                    self.killpg.side_effect = error
                    player.terminate_grace_seconds = 0.5
                    with self.assertRaises(PlaybackInterrupted):
                        player.play([Segment(self.mp3)], stop=stop)
                    self.assertEqual(self.child.direct, ["TERM", "KILL"] if ignores else ["TERM"])
                    self.assertIsNotNone(self.child.returncode)

    def test_without_killpg_the_child_alone_is_stopped(self):
        player, stop = self.stop_at_first_poll()
        with mock.patch("chime.audio.os.killpg", None):
            with self.assertRaises(PlaybackInterrupted):
                player.play([Segment(self.mp3)], stop=stop)
        self.assertEqual(self.child.direct, ["TERM"])

    def test_a_child_that_is_already_gone_gets_no_signal(self):
        # 回収済みの子の pid は、別のプロセスに再利用されているかもしれない。信号を送らない。
        player = self.start(self.child_of(returncode=-signal.SIGTERM))
        player._terminate(self.child)
        self.killpg.assert_not_called()
        self.assertEqual(self.child.direct, [])

    def test_a_broken_off_wait_still_stops_the_group(self):
        # 待ちの途中で Ctrl+C など（KeyboardInterrupt）。呼び出し側へ通しつつ、子と孫は止める。
        player = self.start(self.child_of(wait_error=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            player.play([Segment(self.mp3)])
        self.assertEqual(self.killpg.call_args_list, [self.to_group(signal.SIGTERM), self.to_group(0)])
        self.assertEqual(self.child.returncode, -signal.SIGTERM)


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


#: 本物の pygame と同じ振る舞いをする偽物。import すると、環境変数
#: ``PYGAME_HIDE_SUPPORT_PROMPT`` が（値に関わらず）設定されていなければ、
#: 標準出力にバナーを出す。
FAKE_PYGAME = (
    "import os\n"
    "if 'PYGAME_HIDE_SUPPORT_PROMPT' not in os.environ:\n"
    "    print('pygame 9.9.9 (SDL 2.0.0, Python 3)')\n"
    "    print('Hello from the pygame community. https://www.pygame.org/contribute.html')\n"
)

BANNER_MARKER = "Hello from the pygame community"


class PygameBannerTest(unittest.TestCase):
    """``import chime.audio`` が pygame のバナーを標準出力に出さないこと。

    バナーは ``--print-config`` の JSON 出力に混ざって壊す。import の順序が
    問題なので、新しいインタプリタ（子プロセス）で import して標準出力を調べる。
    pygame は偽物（上の ``FAKE_PYGAME``）を ``PYTHONPATH`` の先頭に置いて差し替える。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake = os.path.join(self.tmp.name, "pygame")
        os.makedirs(fake)
        with open(os.path.join(fake, "__init__.py"), "w", encoding="utf-8") as handle:
            handle.write(FAKE_PYGAME)

    def run_python(self, code, **env_overrides):
        """``code`` を子プロセスで実行し、``(標準出力, 終了コード)`` を返す。"""
        env = {key: value for key, value in os.environ.items()
               if key != "PYGAME_HIDE_SUPPORT_PROMPT"}
        env["PYTHONPATH"] = os.pathsep.join([self.tmp.name, REPO_ROOT])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(env_overrides)
        result = subprocess.run([sys.executable, "-c", code], env=env, cwd=self.tmp.name,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_fake_pygame_prints_the_banner_unless_told_not_to(self):
        # 対照: この偽物が、変数なしの import でバナーを出すこと（出さなければ、
        # 下のテストは何も確かめていない）。
        self.assertIn(BANNER_MARKER, self.run_python("import pygame"))
        self.assertEqual(self.run_python("import pygame", PYGAME_HIDE_SUPPORT_PROMPT="1"), "")

    def test_importing_chime_audio_prints_nothing(self):
        self.assertEqual(self.run_python("import chime.audio"), "")

    def test_the_real_import_picks_up_the_fake_pygame(self):
        # 偽物が実際に使われている（chime.audio.pygame が偽物のモジュール）。
        out = self.run_python("import chime.audio; print(chime.audio.pygame.__file__)")
        self.assertTrue(out.strip().splitlines()[-1].startswith(self.tmp.name), out)

    def test_importing_the_app_and_cli_prints_nothing(self):
        # chime.audio を引き込む入口（app / cli）からでも同じ。
        self.assertEqual(self.run_python("import chime.app, chime.cli"), "")

    def test_the_variable_is_set_to_1_when_unset(self):
        out = self.run_python(
            "import chime.audio, os; print(os.environ['PYGAME_HIDE_SUPPORT_PROMPT'])")
        self.assertEqual(out.strip(), "1")

    def test_a_value_the_user_set_is_kept(self):
        out = self.run_python(
            "import chime.audio, os; print(os.environ['PYGAME_HIDE_SUPPORT_PROMPT'])",
            PYGAME_HIDE_SUPPORT_PROMPT="custom")
        self.assertEqual(out.strip(), "custom")


if __name__ == "__main__":
    unittest.main()
