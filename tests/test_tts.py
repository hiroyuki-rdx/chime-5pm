"""音声合成のフォールバック・キャッシュのテスト。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

from tests.support import VOICE_DIR, logs_enabled

from chime.tts import (PrerecordedEngine, TTSEngine, TTSError,
                       TTSService, VoicevoxEngine, digest)


class FakeEngine(TTSEngine):
    """テスト用のダミーエンジン。"""

    def __init__(self, name, available=True, fail=False):
        super().__init__({})
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
    service = TTSService({"engines": []}, cache_dir,
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
        self.assertNotEqual(digest("A", "B C"), digest("A B", "C"))

    def test_no_temporary_file_is_left_behind(self):
        make_service([FakeEngine("first")], self.cache).synthesize("文言")
        self.assertEqual([name for name in os.listdir(self.cache) if name.endswith(".tmp")], [])

    def test_temporary_file_is_removed_when_engine_fails_after_writing(self):
        # 合成が途中で失敗し、書きかけの出力ファイルが残るケースを再現する。
        service = make_service(
            [PartiallyWritingEngine({}), FakeEngine("fallback")], self.cache)
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
        service = TTSService({"engines": ["prerecorded", "voicevox"]},
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
        service = TTSService({"engines": ["prerecorded", "voicevox"]},
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
        service = TTSService({"engines": ["prerecorded", "voicevox"]},
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
        service = make_service([PrerecordedEngine({}, self._prerecorded_dir()),
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

    # -- 合成の副作用: キャッシュ用ディレクトリ・一時ファイル・ログ -------------
    def _synthesize_watching_makedirs(self, service, text):
        """``synthesize`` を呼び、``os.makedirs`` の呼び出しを集めて返す（失敗は握りつぶす）。"""
        with mock.patch("chime.tts.os.makedirs", wraps=os.makedirs) as makedirs:
            try:
                service.synthesize(text)
            except TTSError:
                pass
        return makedirs

    def test_a_prerecorded_miss_creates_the_cache_directory(self):
        # 現状の挙動を固定する: 利用できるエンジンが「既存ファイルなし・キャッシュなし」の
        # ときは合成に進むため、作り置きの取りこぼし（合成を持たない prerecorded 単独でも）
        # でキャッシュ用ディレクトリが作られる。エンジン 1 つにつき 1 回。
        prerecorded = PrerecordedEngine({}, self._prerecorded_dir())
        only = self._synthesize_watching_makedirs(
            make_service([prerecorded], self.cache), "作り置きに無い文言")
        self.assertEqual(only.call_args_list, [mock.call(self.cache, exist_ok=True)])
        self.assertTrue(os.path.isdir(self.cache))

        with_voicevox = self._synthesize_watching_makedirs(
            make_service([prerecorded, FakeEngine("voicevox", fail=True)], self.cache),
            "作り置きに無い文言")
        self.assertEqual(with_voicevox.call_args_list,
                         [mock.call(self.cache, exist_ok=True)] * 2)

    def test_the_cache_directory_is_not_created_without_a_miss(self):
        voice = self._prerecorded_dir()
        with open(os.path.join(voice, digest("ある文言") + ".wav"), "wb") as handle:
            handle.write(b"RIFF")

        # 作り置きにある
        hit = self._synthesize_watching_makedirs(
            make_service([PrerecordedEngine({}, voice), FakeEngine("voicevox")], self.cache),
            "ある文言")
        hit.assert_not_called()
        # 作り置きのフォルダが無い（エンジンが利用不可）
        missing = self._synthesize_watching_makedirs(
            make_service([PrerecordedEngine({}, os.path.join(self.tmp.name, "nothing"))],
                         self.cache), "ない文言")
        missing.assert_not_called()
        # 空の文言
        empty = self._synthesize_watching_makedirs(
            make_service([FakeEngine("a")], self.cache), "  ")
        empty.assert_not_called()
        self.assertFalse(os.path.exists(self.cache))

        # キャッシュにある（1 回目で作ったあとの 2 回目は作りに行かない）
        service = make_service([FakeEngine("a")], self.cache)
        service.synthesize("同じ文言")
        cached = self._synthesize_watching_makedirs(service, "同じ文言")
        cached.assert_not_called()

    def test_the_temporary_file_is_named_after_the_cache_file_pid_and_thread(self):
        engine = FakeEngine("first")
        recorded = []
        original = engine.synthesize

        def record(text, out_path):
            recorded.append(out_path)
            original(text, out_path)

        engine.synthesize = record
        path = make_service([engine], self.cache).synthesize("文言")
        self.assertEqual(recorded, ["{0}.{1}.{2}.tmp".format(
            path, os.getpid(), threading.get_ident())])
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(recorded[0]))

    def test_a_failure_to_move_the_file_is_an_unexpected_error_and_leaves_no_temporary_file(self):
        service = make_service([FakeEngine("first"), FakeEngine("second", fail=True)],
                               self.cache)
        with mock.patch("chime.tts.os.replace", side_effect=OSError("書き込めない")):
            with self.assertRaises(TTSError) as caught:
                service.synthesize("文言")
        self.assertEqual(
            str(caught.exception),
            "音声合成に失敗しました（first: 予期しないエラー: 書き込めない"
            " / second: わざと失敗）")
        self.assertEqual(os.listdir(self.cache), [])

    def test_logs_which_engine_provided_the_voice(self):
        voice = self._prerecorded_dir()
        recorded = os.path.join(voice, digest("ある文言") + ".wav")
        with open(recorded, "wb") as handle:
            handle.write(b"RIFF")
        service = make_service(
            [PrerecordedEngine({}, voice), FakeEngine("voicevox")], self.cache)

        # tests/__init__.py がログを抑制しているため、このテストの間だけ解除する。
        with logs_enabled(), self.assertLogs("chime.tts", level="DEBUG") as captured:
            service.synthesize("ある文言")
            synthesized = service.synthesize("ない文言")
            service.synthesize("ない文言")
        self.assertEqual(
            [(record.levelname, record.getMessage()) for record in captured.records],
            [("DEBUG", "既存音声を使用[prerecorded]: {0}".format(recorded)),
             ("INFO", "音声合成[voicevox]: ない文言"),
             ("DEBUG", "キャッシュを使用[voicevox]: {0}".format(synthesized))])

    def test_describe_lists_engines(self):
        service = make_service([FakeEngine("a"), FakeEngine("b", available=False)], self.cache)
        described = service.describe()
        self.assertIn("a(利用可)", described)
        self.assertIn("b(利用不可)", described)

    def test_engines_are_built_from_settings(self):
        service = TTSService({"engines": ["prerecorded", "voicevox", "???"]},
                             self.cache, self.cache)
        self.assertEqual([engine.name for engine in service.engines],
                         ["prerecorded", "voicevox"])

    def test_open_jtalk_engine_name_is_ignored_with_a_warning(self):
        # Open JTalk はコードごと削除済み（v5.0.0）。古い config.json が
        # "open_jtalk" を残していても、既存の「未知のエンジンは警告して
        # 無視する」経路に乗って安全に無視され、男性音声が復活しないこと。
        # tests/__init__.py がテスト全体でログを抑制している（logging.disable
        # (logging.CRITICAL)）ため、assertLogs で拾えるよう logs_enabled() で
        # このテストの間だけ一時的に解除する。
        with logs_enabled(), self.assertLogs("chime.tts", level="WARNING") as cm:
            service = TTSService(
                {"engines": ["prerecorded", "voicevox", "open_jtalk"]},
                self.cache, self.cache)
        self.assertEqual([engine.name for engine in service.engines],
                         ["prerecorded", "voicevox"])
        self.assertTrue(any("open_jtalk" in message for message in cm.output))

    def test_synthesize_raises_when_no_fallback_exists(self):
        # Open JTalk 削除により、作り置きにも VOICEVOX にも無い文言は
        # 合成できず、他人の声に化けることなく TTSError になること
        # （放送そのものは chime.sequence 側でこの文言だけ落として続く）。
        service = TTSService({"engines": ["prerecorded", "voicevox"]},
                             self.cache, os.path.join(self.cache, "voice"))
        with mock.patch("chime.tts.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("接続できない")):
            with self.assertRaises(TTSError) as caught:
                service.synthesize("これはどこにも作り置きの無い文言です")
        # 作り置き先（self.cache/voice）は作っていないので、フォルダが無いと案内される
        self.assertIn("作り置きのフォルダ（assets/voice/）が見つかりません", str(caught.exception))


class FailureMessageTest(unittest.TestCase):
    """``TTSService._failure``: 全エンジンが失敗したときのエラー文。

    原因の見立て（作り置きのフォルダが無い／文言が無い／作り置きを使っていない）と、
    VOICEVOX ENGINE が使えないことの補足を、組み合わせごとに全文で固定する。
    ``_failure`` は例外を返すだけで、送出は ``synthesize`` が行う。
    """

    DETAIL_MISS = "prerecorded: 事前生成済み音声にこの文言はありません。"
    HINT = "（VOICEVOX ENGINE も使えません。Pi ではこれが正常）"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = os.path.join(tmp.name, "cache")
        self.prerecorded = make_service(
            [PrerecordedEngine({}, os.path.join(tmp.name, "voice"))], self.cache)
        self.without_prerecorded = make_service([FakeEngine("voicevox")], self.cache)

    def test_returns_the_error_without_raising(self):
        error = self.prerecorded._failure(["prerecorded: 利用不可"], ["prerecorded"])
        self.assertIsInstance(error, TTSError)

    def test_without_a_prerecorded_engine_the_details_are_the_whole_message(self):
        error = self.without_prerecorded._failure(["a: わざと失敗", "b: 利用不可"], ["b"])
        self.assertEqual(str(error), "音声合成に失敗しました（a: わざと失敗 / b: 利用不可）")

    def test_without_a_prerecorded_engine_unavailable_engines_add_no_hint(self):
        # 作り置きを使わない設定では「作り置きに無い」とは言わない（補足も付けない）
        error = self.without_prerecorded._failure(
            ["voicevox: 利用不可"], ["voicevox", "prerecorded"])
        self.assertEqual(str(error), "音声合成に失敗しました（voicevox: 利用不可）")

    def test_without_any_engine_the_details_are_empty(self):
        service = make_service([], self.cache)
        self.assertEqual(str(service._failure([], [])), "音声合成に失敗しました（）")

    def test_phrase_missing_from_the_recordings(self):
        error = self.prerecorded._failure([self.DETAIL_MISS], [])
        self.assertEqual(
            str(error),
            "作り置き（assets/voice/）にこの文言がありません。エンジンごとの詳細: "
            + self.DETAIL_MISS)

    def test_phrase_missing_while_voicevox_is_down_adds_the_hint(self):
        error = self.prerecorded._failure(
            [self.DETAIL_MISS, "voicevox: 利用不可"], ["voicevox"])
        self.assertEqual(
            str(error),
            "作り置き（assets/voice/）にこの文言がありません" + self.HINT
            + "。エンジンごとの詳細: " + self.DETAIL_MISS + " / voicevox: 利用不可")

    def test_phrase_missing_while_voicevox_failed_is_not_blamed_on_voicevox_being_down(self):
        error = self.prerecorded._failure(
            [self.DETAIL_MISS, "voicevox: わざと失敗"], [])
        self.assertEqual(
            str(error),
            "作り置き（assets/voice/）にこの文言がありません。エンジンごとの詳細: "
            + self.DETAIL_MISS + " / voicevox: わざと失敗")

    def test_missing_folder(self):
        error = self.prerecorded._failure(["prerecorded: 利用不可"], ["prerecorded"])
        self.assertEqual(
            str(error),
            "作り置きのフォルダ（assets/voice/）が見つかりません。"
            "エンジンごとの詳細: prerecorded: 利用不可")

    def test_missing_folder_while_voicevox_is_down_adds_the_hint(self):
        error = self.prerecorded._failure(
            ["prerecorded: 利用不可", "voicevox: 利用不可"], ["prerecorded", "voicevox"])
        self.assertEqual(
            str(error),
            "作り置きのフォルダ（assets/voice/）が見つかりません" + self.HINT
            + "。エンジンごとの詳細: prerecorded: 利用不可 / voicevox: 利用不可")

    def test_only_an_engine_named_voicevox_adds_the_hint(self):
        error = self.prerecorded._failure([self.DETAIL_MISS, "other: 利用不可"], ["other"])
        self.assertNotIn("VOICEVOX ENGINE も使えません", str(error))

    def test_synthesize_raises_what_failure_builds(self):
        service = make_service([FakeEngine("voicevox", available=False)], self.cache)
        with self.assertRaises(TTSError) as caught:
            service.synthesize("こんにちは")
        self.assertEqual(str(caught.exception),
                         str(service._failure(["voicevox: 利用不可"], ["voicevox"])))
        self.assertEqual(str(caught.exception), "音声合成に失敗しました（voicevox: 利用不可）")


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
        engine = PrerecordedEngine({}, self.directory)
        self.assertTrue(engine.lookup("午前10時をお知らせしました。").endswith("hour_10.wav"))

    def test_lookup_by_digest_filename(self):
        text = "こんにちは"
        expected = self._touch(digest(text) + ".wav")
        engine = PrerecordedEngine({}, self.directory)
        self.assertEqual(engine.lookup(text), expected)

    def test_lookup_returns_none_when_absent(self):
        engine = PrerecordedEngine({}, self.directory)
        self.assertIsNone(engine.lookup("ありません"))

    def test_non_utf8_manifest_does_not_crash(self):
        # manifest.json が UTF-8 として読めなくても、放送を止めない。
        # 目録が空扱いになるだけで、ダイジェスト名のファイルは引ける。
        text = "こんにちは"
        expected = self._touch(digest(text) + ".wav")
        with open(os.path.join(self.directory, "manifest.json"), "wb") as handle:
            handle.write(json.dumps({"あいさつ": "greeting.wav"},
                                    ensure_ascii=False).encode("shift_jis"))
        engine = PrerecordedEngine({}, self.directory)
        self.assertEqual(engine.manifest(), {})
        self.assertEqual(engine.lookup(text), expected)
        self.assertIsNone(engine.lookup("あいさつ"))

    def _load_manifest_capturing_logs(self, engine):
        """``manifest()`` を呼び、WARNING 以上のログを集めて返す。

        tests/__init__.py がログを抑制しているため、tests/support.py の
        ``logs_enabled()`` でこの間だけ一時的に解除する。``assertLogs`` は 1 件も出ないと
        失敗するので、判定用のダミーを先に出す。
        """
        with logs_enabled(), self.assertLogs("chime.tts", level="WARNING") as captured:
            logging.getLogger("chime.tts").warning("dummy")
            manifest = engine.manifest()
        return manifest, [record.getMessage() for record in captured.records
                          if record.getMessage() != "dummy"]

    def test_non_utf8_manifest_is_a_warning_with_guidance(self):
        manifest_path = os.path.join(self.directory, "manifest.json")
        with open(manifest_path, "wb") as handle:
            handle.write("{\"あいさつ\": \"greeting.wav\"}".encode("shift_jis"))
        manifest, messages = self._load_manifest_capturing_logs(
            PrerecordedEngine({}, self.directory))
        self.assertEqual(manifest, {})
        self.assertEqual(len(messages), 1)
        self.assertIn(manifest_path, messages[0])
        self.assertIn("UTF-8", messages[0])

    def test_broken_manifest_is_treated_as_empty_with_a_warning(self):
        with open(os.path.join(self.directory, "manifest.json"), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        manifest, messages = self._load_manifest_capturing_logs(
            PrerecordedEngine({}, self.directory))
        self.assertEqual(manifest, {})
        self.assertEqual(len(messages), 1)
        self.assertIn("1 行", messages[0])

    def test_missing_manifest_is_not_a_warning(self):
        manifest, messages = self._load_manifest_capturing_logs(
            PrerecordedEngine({}, self.directory))
        self.assertEqual(manifest, {})
        self.assertEqual(messages, [])

    def test_manifest_that_is_not_an_object_is_treated_as_empty(self):
        with open(os.path.join(self.directory, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(["配列は想定外"], handle, ensure_ascii=False)
        self.assertEqual(PrerecordedEngine({}, self.directory).manifest(), {})

    def test_utf8_bom_manifest_is_readable(self):
        path = self._touch("greeting.wav")
        with open(os.path.join(self.directory, "manifest.json"), "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + json.dumps(
                {"こんにちは": "greeting.wav"}, ensure_ascii=False).encode("utf-8"))
        engine = PrerecordedEngine({}, self.directory)
        self.assertEqual(engine.lookup("こんにちは"), path)

    def test_synthesize_always_raises(self):
        engine = PrerecordedEngine({}, self.directory)
        with self.assertRaises(TTSError):
            engine.synthesize("ありません", "/tmp/out.wav")

    def test_manifest_is_used_by_the_service(self):
        path = self._touch("greeting.wav")
        with open(os.path.join(self.directory, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"こんにちは": "greeting.wav"}, handle, ensure_ascii=False)
        service = TTSService({"engines": ["prerecorded"]},
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
        return TTSService({"engines": list(engines)}, self.cache, self.voice)

    def test_returns_the_path_of_a_phrase_in_the_manifest(self):
        path = self._touch("greeting.wav")
        self._write_manifest({"こんにちは": "greeting.wav"})
        self.assertEqual(self._service().prerecorded_lookup("こんにちは"), path)

    def test_returns_the_path_of_a_digest_named_file(self):
        path = self._touch(digest("こんにちは") + ".wav")
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
        self._touch(digest("こんにちは") + ".wav")
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
        self._touch(digest("こんにちは") + ".wav")
        self.assertIsNone(self._service(engines=["voicevox"]).prerecorded_lookup("こんにちは"))

    def test_returns_none_when_the_directory_is_missing(self):
        service = TTSService({"engines": ["prerecorded"]}, self.cache,
                             os.path.join(self.tmp.name, "nothing"))
        self.assertIsNone(service.prerecorded_lookup("こんにちは"))

    def test_ignores_surrounding_whitespace_like_synthesize(self):
        # synthesize は前後の空白を落としてから引く。放送で鳴るかどうかを
        # 事前に調べる用途なので、同じ扱いにする。
        path = self._touch(digest("こんにちは") + ".wav")
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
        service = TTSService({"engines": ["prerecorded"]}, self.cache, VOICE_DIR)
        phrases = service.known_phrases()
        self.assertIn("正午をお知らせしたのだ。", phrases)
        self.assertEqual(len(phrases), len(set(phrases)))
        self.assertIsNotNone(service.prerecorded_lookup("正午をお知らせしたのだ。"))


class EngineConfigurationTest(unittest.TestCase):
    def test_voicevox_is_unavailable_without_url(self):
        self.assertFalse(VoicevoxEngine({"base_url": ""}).available())

    def test_voicevox_voice_id_includes_speaker(self):
        self.assertEqual(VoicevoxEngine({"speaker": 3}).voice_id(), "voicevox|3")

    def test_voicevox_probe_timeout_defaults_to_two_seconds(self):
        # 放送直前（実行時）に呼ばれるため既定は短く、エンジンが落ちて
        # いた場合に即座に次のエンジンへフォールバックできること。
        self.assertEqual(VoicevoxEngine({}).probe_timeout, 2.0)

    def test_voicevox_probe_timeout_is_configurable(self):
        # 事前生成スクリプト（起動待ち）など、長めのタイムアウトが
        # 必要な用途のために設定可能であること。
        engine = VoicevoxEngine({"probe_timeout_seconds": 30.0})
        self.assertEqual(engine.probe_timeout, 30.0)

    def test_voicevox_available_uses_probe_timeout(self):
        engine = VoicevoxEngine(
            {"base_url": "http://example.invalid:50021", "probe_timeout_seconds": 9.5})
        with mock.patch("chime.tts.urllib.request.urlopen") as mocked:
            mocked.return_value.__enter__.return_value.status = 200
            self.assertTrue(engine.available())
        self.assertEqual(mocked.call_args.kwargs.get("timeout"), 9.5)


if __name__ == "__main__":
    unittest.main()
