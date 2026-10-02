"""音声合成のフォールバック・キャッシュのテスト。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from tests.support import REPO_ROOT

from chime.tts import (PrerecordedEngine, TTSEngine, TTSError,
                       TTSService, VoicevoxEngine, _digest)


class FakeEngine(TTSEngine):
    """テスト用のダミーエンジン。"""

    def __init__(self, name, available=True, fail=False):
        super().__init__({}, "/tmp")
        self.name = name
        self._available = available
        self._fail = fail
        self.calls = []

    def available(self):
        return self._available

    def synthesize(self, text, out_path):
        self.calls.append(text)
        if self._fail:
            raise TTSError("わざと失敗")
        with open(out_path, "wb") as handle:
            handle.write(b"RIFF" + self.name.encode("utf-8"))


class PartiallyWritingEngine(TTSEngine):
    """失敗時でも出力ファイルを書きかけで残すエンジン。"""

    name = "partial"

    def available(self):
        return True

    def synthesize(self, text, out_path):
        with open(out_path, "wb") as handle:
            handle.write(b"\x00")
        raise TTSError("途中まで書いて失敗")


def make_service(engines, cache_dir, prerecorded_dir=""):
    service = TTSService({"engines": []}, "/tmp", cache_dir,
                         prerecorded_dir or os.path.join(cache_dir, "voice"))
    service.engines = engines
    return service


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "cache")

    def tearDown(self):
        self.tmp.cleanup()

    def test_uses_the_first_available_engine(self):
        first, second = FakeEngine("first"), FakeEngine("second")
        service = make_service([first, second], self.cache)
        service.synthesize("こんにちは")
        self.assertEqual(first.calls, ["こんにちは"])
        self.assertEqual(second.calls, [])

    def test_skips_unavailable_engines(self):
        first, second = FakeEngine("first", available=False), FakeEngine("second")
        make_service([first, second], self.cache).synthesize("こんにちは")
        self.assertEqual(second.calls, ["こんにちは"])

    def test_falls_back_when_an_engine_fails(self):
        first, second = FakeEngine("first", fail=True), FakeEngine("second")
        path = make_service([first, second], self.cache).synthesize("こんにちは")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(second.calls, ["こんにちは"])

    def test_raises_when_every_engine_fails(self):
        service = make_service([FakeEngine("a", fail=True),
                                FakeEngine("b", available=False)], self.cache)
        with self.assertRaises(TTSError) as caught:
            service.synthesize("こんにちは")
        self.assertIn("a:", str(caught.exception))
        self.assertIn("b:", str(caught.exception))

    def test_empty_text_raises(self):
        with self.assertRaises(TTSError):
            make_service([FakeEngine("a")], self.cache).synthesize("   ")

    def test_result_is_cached(self):
        engine = FakeEngine("first")
        service = make_service([engine], self.cache)
        first = service.synthesize("同じ文言")
        second = service.synthesize("同じ文言")
        self.assertEqual(first, second)
        self.assertEqual(engine.calls, ["同じ文言"], "2 回目は合成し直さない")

    def test_cache_key_depends_on_voice(self):
        alpha, beta = FakeEngine("alpha"), FakeEngine("beta")
        service_a = make_service([alpha], self.cache)
        service_b = make_service([beta], self.cache)
        self.assertNotEqual(service_a.synthesize("文言"), service_b.synthesize("文言"))

    def test_digest_does_not_collide_across_part_boundaries(self):
        # 空白区切りだと ("A", "B C") と ("A B", "C") が同じ文字列になり
        # 衝突してしまう。境界をまたいでも別のダイジェストになること。
        self.assertNotEqual(_digest("A", "B C"), _digest("A B", "C"))

    def test_no_temporary_file_is_left_behind(self):
        make_service([FakeEngine("first")], self.cache).synthesize("文言")
        self.assertEqual([name for name in os.listdir(self.cache) if name.endswith(".tmp")], [])

    def test_temporary_file_is_removed_when_engine_fails_after_writing(self):
        # 合成が途中で失敗し、書きかけの出力ファイルが残るケースを再現する。
        service = make_service(
            [PartiallyWritingEngine({}, "/tmp"), FakeEngine("fallback")], self.cache)
        path = service.synthesize("文言")
        self.assertTrue(os.path.exists(path))
        leftovers = [name for name in os.listdir(self.cache) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "失敗したエンジンの一時ファイルが残っている")

    def _prerecorded_dir(self):
        """空の作り置きディレクトリ（目録なし）を作って返す。"""
        directory = os.path.join(self.tmp.name, "voice")
        os.makedirs(directory)
        return directory

    def test_error_leads_with_the_missing_prerecorded_phrase(self):
        # Pi では VOICEVOX ENGINE が動いていないのが正常。「作り置きに無い」が
        # 主な原因なので、それを先頭に置く。
        service = TTSService({"engines": ["prerecorded", "voicevox"]}, "/tmp",
                             self.cache, self._prerecorded_dir())
        with mock.patch("chime.tts.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("接続できない")):
            with self.assertRaises(TTSError) as caught:
                service.synthesize("これはどこにも作り置きの無い文言です")
        message = str(caught.exception)
        self.assertTrue(message.startswith("作り置き（assets/voice/）にこの文言がありません"),
                        message)
        self.assertIn("（VOICEVOX ENGINE も使えません。Pi ではこれが正常）", message)

    def test_error_says_the_folder_is_missing_when_it_is(self):
        # assets/voice/ そのものが無いときに「この文言がありません」と出すと、
        # 原因（git pull が届いていない・設置場所の違い）に辿り着けない。
        missing = os.path.join(self.cache, "no-such-voice-dir")
        service = TTSService({"engines": ["prerecorded", "voicevox"]}, "/tmp",
                             self.cache, missing)
        with mock.patch("chime.tts.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("接続できない")):
            with self.assertRaises(TTSError) as caught:
                service.synthesize("正午をお知らせしたのだ。")
        message = str(caught.exception)
        self.assertTrue(message.startswith("作り置きのフォルダ（assets/voice/）が見つかりません"),
                        message)
        self.assertNotIn("この文言がありません", message)

    def test_error_keeps_the_details_of_each_engine(self):
        service = TTSService({"engines": ["prerecorded", "voicevox"]}, "/tmp",
                             self.cache, self._prerecorded_dir())
        with mock.patch("chime.tts.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("接続できない")):
            with self.assertRaises(TTSError) as caught:
                service.synthesize("これはどこにも作り置きの無い文言です")
        message = str(caught.exception)
        self.assertIn("prerecorded:", message)
        self.assertIn("voicevox: 利用不可", message)

    def test_error_does_not_say_voicevox_is_down_when_it_was_up(self):
        # VOICEVOX が動いていて合成に失敗した場合は「も使えません」と言わない
        # （原因は別にあるので、エンジンごとの詳細を読んでもらう）。
        service = make_service([PrerecordedEngine({}, "/tmp", self._prerecorded_dir()),
                                FakeEngine("voicevox", fail=True)], self.cache)
        with self.assertRaises(TTSError) as caught:
            service.synthesize("これはどこにも作り置きの無い文言です")
        message = str(caught.exception)
        self.assertTrue(message.startswith("作り置き（assets/voice/）にこの文言がありません"),
                        message)
        self.assertNotIn("も使えません", message)
        self.assertIn("voicevox: わざと失敗", message)

    def test_error_without_prerecorded_engine_does_not_blame_the_recordings(self):
        service = make_service([FakeEngine("voicevox", available=False)], self.cache)
        with self.assertRaises(TTSError) as caught:
            service.synthesize("こんにちは")
        message = str(caught.exception)
        self.assertTrue(message.startswith("音声合成に失敗しました"), message)
        self.assertNotIn("作り置き", message)

    def test_describe_lists_engines(self):
        service = make_service([FakeEngine("a"), FakeEngine("b", available=False)], self.cache)
        described = service.describe()
        self.assertIn("a(利用可)", described)
        self.assertIn("b(利用不可)", described)

    def test_engines_are_built_from_settings(self):
        service = TTSService({"engines": ["prerecorded", "voicevox", "???"]},
                             "/tmp", self.cache, self.cache)
        self.assertEqual([engine.name for engine in service.engines],
                         ["prerecorded", "voicevox"])

    def test_open_jtalk_engine_name_is_ignored_with_a_warning(self):
        # Open JTalk はコードごと削除済み（v5.0.0）。古い config.json が
        # "open_jtalk" を残していても、既存の「未知のエンジンは警告して
        # 無視する」経路に乗って安全に無視され、男性音声が復活しないこと。
        # tests/__init__.py がテスト全体でログを抑制している（logging.disable
        # (logging.CRITICAL)）ため、assertLogs で拾えるよう tests/test_sequence.py
        # と同じ手順でこのテストの間だけ一時的に解除する。
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("chime.tts", level="WARNING") as cm:
                service = TTSService(
                    {"engines": ["prerecorded", "voicevox", "open_jtalk"]},
                    "/tmp", self.cache, self.cache)
        finally:
            logging.disable(logging.CRITICAL)
        self.assertEqual([engine.name for engine in service.engines],
                         ["prerecorded", "voicevox"])
        self.assertTrue(any("open_jtalk" in message for message in cm.output))

    def test_synthesize_raises_when_no_fallback_exists(self):
        # Open JTalk 削除により、作り置きにも VOICEVOX にも無い文言は
        # 合成できず、他人の声に化けることなく TTSError になること
        # （放送そのものは chime.sequence 側でこの文言だけ落として続く）。
        service = TTSService({"engines": ["prerecorded", "voicevox"]},
                             "/tmp", self.cache, os.path.join(self.cache, "voice"))
        with mock.patch("chime.tts.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("接続できない")):
            with self.assertRaises(TTSError) as caught:
                service.synthesize("これはどこにも作り置きの無い文言です")
        # 作り置き先（self.cache/voice）は作っていないので、フォルダが無いと案内される
        self.assertIn("作り置きのフォルダ（assets/voice/）が見つかりません", str(caught.exception))


class PrerecordedEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, name):
        path = os.path.join(self.directory, name)
        with open(path, "wb") as handle:
            handle.write(b"RIFF")
        return path

    def test_lookup_by_manifest(self):
        self._touch("hour_10.wav")
        with open(os.path.join(self.directory, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"午前10時をお知らせしました。": "hour_10.wav"}, handle, ensure_ascii=False)
        engine = PrerecordedEngine({}, "/tmp", self.directory)
        self.assertTrue(engine.lookup("午前10時をお知らせしました。").endswith("hour_10.wav"))

    def test_lookup_by_digest_filename(self):
        text = "こんにちは"
        expected = self._touch(_digest(text) + ".wav")
        engine = PrerecordedEngine({}, "/tmp", self.directory)
        self.assertEqual(engine.lookup(text), expected)

    def test_lookup_returns_none_when_absent(self):
        engine = PrerecordedEngine({}, "/tmp", self.directory)
        self.assertIsNone(engine.lookup("ありません"))

    def test_non_utf8_manifest_does_not_crash(self):
        # manifest.json が UTF-8 として読めなくても、放送を止めない。
        # 目録が空扱いになるだけで、ダイジェスト名のファイルは引ける。
        text = "こんにちは"
        expected = self._touch(_digest(text) + ".wav")
        with open(os.path.join(self.directory, "manifest.json"), "wb") as handle:
            handle.write(json.dumps({"あいさつ": "greeting.wav"},
                                    ensure_ascii=False).encode("shift_jis"))
        engine = PrerecordedEngine({}, "/tmp", self.directory)
        self.assertEqual(engine.manifest(), {})
        self.assertEqual(engine.lookup(text), expected)
        self.assertIsNone(engine.lookup("あいさつ"))

    def _load_manifest_capturing_logs(self, engine):
        """``manifest()`` を呼び、WARNING 以上のログを集めて返す。

        tests/__init__.py がログを抑制しているため、tests/test_sequence.py と
        同じ手順でこの間だけ一時的に解除する。``assertLogs`` は 1 件も出ないと
        失敗するので、判定用のダミーを先に出す。
        """
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("chime.tts", level="WARNING") as captured:
                logging.getLogger("chime.tts").warning("dummy")
                manifest = engine.manifest()
        finally:
            logging.disable(logging.CRITICAL)
        return manifest, [record.getMessage() for record in captured.records
                          if record.getMessage() != "dummy"]

    def test_non_utf8_manifest_is_a_warning_with_guidance(self):
        manifest_path = os.path.join(self.directory, "manifest.json")
        with open(manifest_path, "wb") as handle:
            handle.write("{\"あいさつ\": \"greeting.wav\"}".encode("shift_jis"))
        manifest, messages = self._load_manifest_capturing_logs(
            PrerecordedEngine({}, "/tmp", self.directory))
        self.assertEqual(manifest, {})
        self.assertEqual(len(messages), 1)
        self.assertIn(manifest_path, messages[0])
        self.assertIn("UTF-8", messages[0])

    def test_broken_manifest_is_treated_as_empty_with_a_warning(self):
        with open(os.path.join(self.directory, "manifest.json"), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        manifest, messages = self._load_manifest_capturing_logs(
            PrerecordedEngine({}, "/tmp", self.directory))
        self.assertEqual(manifest, {})
        self.assertEqual(len(messages), 1)
        self.assertIn("1 行", messages[0])

    def test_missing_manifest_is_not_a_warning(self):
        manifest, messages = self._load_manifest_capturing_logs(
            PrerecordedEngine({}, "/tmp", self.directory))
        self.assertEqual(manifest, {})
        self.assertEqual(messages, [])

    def test_manifest_that_is_not_an_object_is_treated_as_empty(self):
        with open(os.path.join(self.directory, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(["配列は想定外"], handle, ensure_ascii=False)
        self.assertEqual(PrerecordedEngine({}, "/tmp", self.directory).manifest(), {})

    def test_utf8_bom_manifest_is_readable(self):
        path = self._touch("greeting.wav")
        with open(os.path.join(self.directory, "manifest.json"), "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + json.dumps(
                {"こんにちは": "greeting.wav"}, ensure_ascii=False).encode("utf-8"))
        engine = PrerecordedEngine({}, "/tmp", self.directory)
        self.assertEqual(engine.lookup("こんにちは"), path)

    def test_synthesize_always_raises(self):
        engine = PrerecordedEngine({}, "/tmp", self.directory)
        with self.assertRaises(TTSError):
            engine.synthesize("ありません", "/tmp/out.wav")

    def test_manifest_is_used_by_the_service(self):
        path = self._touch("greeting.wav")
        with open(os.path.join(self.directory, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"こんにちは": "greeting.wav"}, handle, ensure_ascii=False)
        service = TTSService({"engines": ["prerecorded"]}, "/tmp",
                             os.path.join(self.directory, "cache"), self.directory)
        self.assertEqual(service.synthesize("こんにちは"), path)


class PrerecordedLookupTest(unittest.TestCase):
    """``TTSService.prerecorded_lookup`` / ``known_phrases``。

    作り置きだけを引き、VOICEVOX には触れない（Pi 上でも、エンジンが
    動いていなくても、放送で無音になる文言を調べられるようにするため）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.voice = os.path.join(self.tmp.name, "voice")
        self.cache = os.path.join(self.tmp.name, "cache")
        os.makedirs(self.voice)

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, name):
        path = os.path.join(self.voice, name)
        with open(path, "wb") as handle:
            handle.write(b"RIFF")
        return path

    def _write_manifest(self, manifest):
        with open(os.path.join(self.voice, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False)

    def _service(self, engines=("prerecorded", "voicevox")):
        return TTSService({"engines": list(engines)}, "/tmp", self.cache, self.voice)

    def test_returns_the_path_of_a_phrase_in_the_manifest(self):
        path = self._touch("greeting.wav")
        self._write_manifest({"こんにちは": "greeting.wav"})
        self.assertEqual(self._service().prerecorded_lookup("こんにちは"), path)

    def test_returns_the_path_of_a_digest_named_file(self):
        path = self._touch(_digest("こんにちは") + ".wav")
        self.assertEqual(self._service().prerecorded_lookup("こんにちは"), path)

    def test_returns_none_for_an_unknown_phrase(self):
        self._write_manifest({})
        self.assertIsNone(self._service().prerecorded_lookup("ありません"))

    def test_returns_none_when_the_listed_file_is_gone(self):
        self._write_manifest({"こんにちは": "greeting.wav"})
        self.assertIsNone(self._service().prerecorded_lookup("こんにちは"))

    def test_does_not_use_voicevox(self):
        self._write_manifest({})
        service = self._service()
        with mock.patch("chime.tts.urllib.request.urlopen") as urlopen, \
                mock.patch.object(VoicevoxEngine, "available") as available, \
                mock.patch.object(VoicevoxEngine, "synthesize") as synthesize:
            self.assertIsNone(service.prerecorded_lookup("作り置きに無い文言"))
        urlopen.assert_not_called()
        available.assert_not_called()
        synthesize.assert_not_called()

    def test_does_not_use_any_other_engine(self):
        self._touch(_digest("こんにちは") + ".wav")
        other = FakeEngine("voicevox")
        service = self._service()
        service.engines.append(other)
        service.prerecorded_lookup("こんにちは")
        service.prerecorded_lookup("作り置きに無い文言")
        self.assertEqual(other.calls, [])

    def test_does_not_create_the_cache_directory(self):
        self._service().prerecorded_lookup("作り置きに無い文言")
        self.assertFalse(os.path.exists(self.cache))

    def test_returns_none_without_a_prerecorded_engine(self):
        self._touch(_digest("こんにちは") + ".wav")
        self.assertIsNone(self._service(engines=["voicevox"]).prerecorded_lookup("こんにちは"))

    def test_returns_none_when_the_directory_is_missing(self):
        service = TTSService({"engines": ["prerecorded"]}, "/tmp", self.cache,
                             os.path.join(self.tmp.name, "nothing"))
        self.assertIsNone(service.prerecorded_lookup("こんにちは"))

    def test_ignores_surrounding_whitespace_like_synthesize(self):
        # synthesize は前後の空白を落としてから引く。放送で鳴るかどうかを
        # 事前に調べる用途なので、同じ扱いにする。
        path = self._touch(_digest("こんにちは") + ".wav")
        service = self._service()
        self.assertEqual(service.prerecorded_lookup("  こんにちは\n"), path)
        self.assertEqual(service.prerecorded_lookup("  こんにちは\n"),
                         service.synthesize("  こんにちは\n"))

    def test_empty_text_returns_none(self):
        service = self._service()
        self.assertIsNone(service.prerecorded_lookup(""))
        self.assertIsNone(service.prerecorded_lookup("   "))
        self.assertIsNone(service.prerecorded_lookup(None))

    def test_known_phrases_lists_the_manifest_in_file_order(self):
        self._write_manifest({"ふたつめ": "b.wav", "ひとつめ": "a.wav", "みっつめ": "c.wav"})
        self.assertEqual(self._service().known_phrases(),
                         ["ふたつめ", "ひとつめ", "みっつめ"])

    def test_known_phrases_returns_a_copy(self):
        self._write_manifest({"ひとつめ": "a.wav"})
        service = self._service()
        service.known_phrases().append("書き換え")
        self.assertEqual(service.known_phrases(), ["ひとつめ"])

    def test_known_phrases_is_empty_without_a_manifest(self):
        self.assertEqual(self._service().known_phrases(), [])

    def test_known_phrases_is_empty_without_a_prerecorded_engine(self):
        self._write_manifest({"ひとつめ": "a.wav"})
        self.assertEqual(self._service(engines=["voicevox"]).known_phrases(), [])

    def test_known_phrases_survives_an_unreadable_manifest(self):
        with open(os.path.join(self.voice, "manifest.json"), "wb") as handle:
            handle.write("{\"あ\": \"a.wav\"}".encode("shift_jis"))
        self.assertEqual(self._service().known_phrases(), [])

    def test_shipped_manifest_knows_the_broadcast_phrases(self):
        service = TTSService({"engines": ["prerecorded"]}, REPO_ROOT, self.cache,
                             os.path.join(REPO_ROOT, "assets", "voice"))
        phrases = service.known_phrases()
        self.assertIn("正午をお知らせしたのだ。", phrases)
        self.assertEqual(len(phrases), len(set(phrases)))
        self.assertIsNotNone(service.prerecorded_lookup("正午をお知らせしたのだ。"))


class EngineConfigurationTest(unittest.TestCase):
    def test_voicevox_is_unavailable_without_url(self):
        self.assertFalse(VoicevoxEngine({"base_url": ""}, "/tmp").available())

    def test_voicevox_voice_id_includes_speaker(self):
        self.assertEqual(VoicevoxEngine({"speaker": 3}, "/tmp").voice_id(), "voicevox|3")

    def test_voicevox_probe_timeout_defaults_to_two_seconds(self):
        # 放送直前（実行時）に呼ばれるため既定は短く、エンジンが落ちて
        # いた場合に即座に次のエンジンへフォールバックできること。
        self.assertEqual(VoicevoxEngine({}, "/tmp").probe_timeout, 2.0)

    def test_voicevox_probe_timeout_is_configurable(self):
        # 事前生成スクリプト（起動待ち）など、長めのタイムアウトが
        # 必要な用途のために設定可能であること。
        engine = VoicevoxEngine({"probe_timeout_seconds": 30.0}, "/tmp")
        self.assertEqual(engine.probe_timeout, 30.0)

    def test_voicevox_available_uses_probe_timeout(self):
        engine = VoicevoxEngine(
            {"base_url": "http://example.invalid:50021", "probe_timeout_seconds": 9.5}, "/tmp")
        with mock.patch("chime.tts.urllib.request.urlopen") as mocked:
            mocked.return_value.__enter__.return_value.status = 200
            self.assertTrue(engine.available())
        self.assertEqual(mocked.call_args.kwargs.get("timeout"), 9.5)


if __name__ == "__main__":
    unittest.main()
