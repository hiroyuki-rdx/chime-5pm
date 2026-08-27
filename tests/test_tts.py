"""音声合成のフォールバック・キャッシュのテスト。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from chime.tts import (OpenJTalkEngine, PrerecordedEngine, TTSEngine, TTSError,
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
    """open_jtalk のように、失敗時でも出力ファイルを書きかけで残すエンジン。"""

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
        # open_jtalk 等が失敗時に書きかけの出力ファイルを残すケースを再現する。
        service = make_service(
            [PartiallyWritingEngine({}, "/tmp"), FakeEngine("fallback")], self.cache)
        path = service.synthesize("文言")
        self.assertTrue(os.path.exists(path))
        leftovers = [name for name in os.listdir(self.cache) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "失敗したエンジンの一時ファイルが残っている")

    def test_describe_lists_engines(self):
        service = make_service([FakeEngine("a"), FakeEngine("b", available=False)], self.cache)
        described = service.describe()
        self.assertIn("a(利用可)", described)
        self.assertIn("b(利用不可)", described)

    def test_engines_are_built_from_settings(self):
        service = TTSService({"engines": ["prerecorded", "voicevox", "open_jtalk", "???"]},
                             "/tmp", self.cache, self.cache)
        self.assertEqual([engine.name for engine in service.engines],
                         ["prerecorded", "voicevox", "open_jtalk"])


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


class EngineConfigurationTest(unittest.TestCase):
    def test_open_jtalk_is_unavailable_without_binary(self):
        engine = OpenJTalkEngine({"binary": "definitely-not-installed"}, "/tmp")
        self.assertFalse(engine.available())

    def test_open_jtalk_voice_id_reflects_settings(self):
        slow = OpenJTalkEngine({"speed": 0.8}, "/tmp").voice_id()
        fast = OpenJTalkEngine({"speed": 1.2}, "/tmp").voice_id()
        self.assertNotEqual(slow, fast)

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
