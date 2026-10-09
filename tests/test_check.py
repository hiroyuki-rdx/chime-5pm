"""設置状態の点検（``chime/check.py``、``--check``）のテスト。

現地で人が実行して結果を読むものなので、次を固定する。

* 何も書かない（ファイルも、まだ無いフォルダも作らない。時報音も作らない）。
* NG と警告の分け方。NG があれば終了コード 1、警告だけなら 0。NG にするのは、放送を
  実際に妨げるものだけ（書けない状態フォルダ、無い・壊れた音源や作り置き）。履歴・音声
  キャッシュが書けないのと、root 所有のファイルが置き場所に残っているだけ（アプリは
  一時ファイルから置き換えて保存するので困らない）は、NG にしない。
* 直し方が出る（同梱の音源は ``git checkout``、時報音は ``setup.sh``、書けない場所は
  ``chown`` / ``chmod``）。``chown -R`` は設定が指す場所そのものにだけ使い、まだ無い場所
  には、その 1 つだけを作って渡す（実在する親を巻き込まない）。
* 置いてあるのに壊れているファイル（空・途中で切れた WAV・短すぎる MP3・フォルダ）を見逃さない。
  MP3 は MPEG のフレームを最後までたどるので、先頭が正しいまま途中で切れた蛍の光や、途中に
  壊れた所がある蛍の光も NG になる（同梱の ``assets/hotaru.mp3`` は OK）。ID3 タグは正しいのに
  そのあとにフレームが無い（電源断で、タグまでしか書けなかった）ものも NG。先頭がフレームに
  見えない・タグを読めない形のものは、今までどおり先頭の確かめだけ。
* ``tts.engines`` に ``prerecorded`` が無いと、声がそろっていても Pi では全部が無音なので NG
  （``TTSService`` と同じ読み方）。
* sticky ビットのフォルダにある、他人の ``state.json`` は置き換えて保存できないので NG。
* 絶対パスで書く「まだ無い」場所（``/nonexistent`` や ``/etc/chime``）は、この機械に実在するか
  どうかに関わらず同じ結果になる（``only_these_exist``）。

点検される「設置場所」は一時フォルダに作る。同梱の音声は大きいので、作り置きは
``tests/support.make_wav`` の短い無音で、全文言ぶん作る。root で動くテスト環境でも
そうでない環境でも同じ結果になるよう、所有者・書き込み可否・実行している利用者は
差し替える（``chime.check._uid_of`` / ``_can_write``、``euid`` 引数）。
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from tests.support import ASSETS_DIR, SHIPPED_QUOTES, block_network, logs_enabled, make_wav

from chime import check, configcheck
from chime.check import (INFO, NG, OK, WARNING, Result, check_settings, check_sounds, check_voices,
                         check_writable, display_width, pad, prerecorded_coverage, render_section,
                         run_check, shown_path, summarize)
from chime.config import DEFAULT_CONFIG, Config, deep_merge
from chime.phrases import CoverageTooLarge, collect_phrases
from chime.tts import MANIFEST_FILENAME, TTSService, prerecorded_filename

#: 普通の利用者（サービスを動かす ``pi`` のつもり）の uid。
PI_UID = 1000

#: 実物の形をした MP3 の部品。フレームのヘッダー（4 バイト）と、そのフレームの長さ。
#: 長さは MPEG オーディオの規格の式（レイヤー III の MPEG-1 は 144 * ビットレート / 周波数）を
#: 手で計算した値で、``chime.check`` の計算とは別に決めてある。
#: (名前, ヘッダー, パディング無しの長さ)
MPEG_FRAMES = (
    ("MPEG-1 レイヤー III 128 kbps 44.1 kHz", b"\xff\xfb\x90\x00", 417),
    ("MPEG-1 レイヤー II 192 kbps 48 kHz", b"\xff\xfd\xa4\x00", 576),
    ("MPEG-1 レイヤー I 384 kbps 44.1 kHz", b"\xff\xff\xc0\x00", 416),
    ("MPEG-2 レイヤー III 80 kbps 22.05 kHz", b"\xff\xf3\x90\x00", 261),
    ("MPEG-2.5 レイヤー III 80 kbps 11.025 kHz", b"\xff\xe3\x90\x00", 522),
)
FRAME_HEADER, FRAME_LENGTH = MPEG_FRAMES[0][1:]
#: パディングのビットが立っているときの、MPEG-1 レイヤー III 128 kbps 44.1 kHz のヘッダー（長さ 418）。
PADDED_FRAME_HEADER = b"\xff\xfb\x92\x00"


def mpeg_frames(count, header=FRAME_HEADER, length=FRAME_LENGTH, body=b"\x00"):
    """``count`` 個のフレーム（ヘッダーと中身。中身は音になっていなくてよい）。"""
    return (header + body * (length - len(header))) * count


def id3v2(size, version=3, footer=False):
    """中身 ``size`` バイトの ID3v2 タグ（大きさは syncsafe 整数）。``footer`` は ID3v2.4 のフッター（10 バイト）。"""
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    flags = 0x10 if footer else 0
    tag = b"ID3" + bytes([version, 0, flags]) + syncsafe + b"\x00" * size
    if footer:
        tag += b"3DI" + bytes([version, 0, flags]) + syncsafe
    return tag


ID3V1 = b"TAG" + b"\x00" * 125


def make_mp3_bytes(frames=12, tag=100, trailer=b""):
    """ID3v2 タグ、フレーム、あとに付くもの。``MIN_MP3_BYTES`` を超える大きさになる既定。"""
    return id3v2(tag) + mpeg_frames(frames) + trailer


#: MP3 のつもりのファイル（点検が「途中で切れている」と見ない、フレームが最後までそろった大きさ）。
FAKE_MP3 = make_mp3_bytes()


def make_config(base_dir, override=None, sources=None):
    return Config(deep_merge(DEFAULT_CONFIG, override or {}), base_dir=base_dir,
                  sources=sources or ["<defaults>"])


def levels(results):
    return [result.level for result in results]


def only(results, title):
    """``title`` の項目（ちょうど 1 つあること）。"""
    found = [result for result in results if result.title == title]
    assert len(found) == 1, (title, [result.title for result in results])
    return found[0]


class InstallCase(unittest.TestCase):
    """一時フォルダに、すべて正常な設置場所を作る。"""

    def setUp(self):
        block_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        # 所有者は「普通の利用者」とする。root で動くテスト環境でも同じ結果にする。
        self.owners = {}
        patcher = mock.patch("chime.check._uid_of",
                             side_effect=lambda path: self.owners.get(path, PI_UID))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = self.install()

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def install(self, override=None):
        """音源・全文言の作り置き・ひとことを置き、その設定を返す。"""
        os.makedirs(self.path("assets", "voice"), exist_ok=True)
        make_wav(self.path("assets", "announce.wav"))
        with open(self.path("assets", "hotaru.mp3"), "wb") as handle:
            handle.write(FAKE_MP3)
        shutil.copy(SHIPPED_QUOTES, self.path("assets", "quotes.json"))
        config = make_config(self.root, override)
        manifest = {}
        for text in collect_phrases(config, include_quotes=True):
            filename = prerecorded_filename(text)
            make_wav(self.path("assets", "voice", filename), 0.01)
            manifest[text] = filename
        self.write_json(self.path("assets", "voice", MANIFEST_FILENAME), manifest)
        os.makedirs(self.path("assets", "generated"))
        make_wav(self.path("assets", "generated", "time_signal.wav"))
        os.makedirs(self.path("cache", "tts"))
        return config

    @staticmethod
    def write_json(path, data):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)

    def run_check(self, config=None, euid=PI_UID, account="pi:pi"):
        """(終了コード, 出力)。"""
        out = io.StringIO()
        code = run_check(config or self.config, out=out, euid=euid, account=account)
        return code, out.getvalue()


class AllGoodTest(InstallCase):
    def test_a_healthy_install_passes_with_exit_0(self):
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertNotIn("NG", output)
        self.assertNotIn("警告", output)
        self.assertIn("結果: すべて OK です。", output)

    def test_the_sections_come_in_order(self):
        _, output = self.run_check()
        headings = [line for line in output.splitlines() if line.startswith("== ")]
        self.assertEqual(headings, ["== 設定 ==", "== 作り置きの音声 ==", "== 音源 ==", "== 書き込み =="])

    def test_the_voice_summary_counts_every_phrase(self):
        _, output = self.run_check()
        total = len(collect_phrases(self.config, include_quotes=True))
        self.assertIn("{0} 件すべてそろっています".format(total), output)
        self.assertIn("時刻アナウンス 7/7", output)

    def test_the_shipped_assets_pass_the_sound_checks(self):
        """同梱の音源（実ファイル）が、点検の基準を満たすこと。"""
        config = make_config(os.path.dirname(ASSETS_DIR))
        results = check_sounds(config)
        self.assertEqual(levels(results[:2]), [OK, OK], [r.detail for r in results])


class SettingsSectionTest(InstallCase):
    def test_sources_are_listed_in_the_order_they_were_read(self):
        config = make_config(self.root, sources=["<defaults>", "/a/config.json", "/b/extra.json"])
        with mock.patch("chime.check.configcheck.check_config", return_value=[]):
            results = check_settings(config)
        self.assertEqual(results[0].detail, "既定値 → /a/config.json → /b/extra.json")

    def test_defaults_only_is_said_plainly(self):
        results = check_settings(self.config)
        self.assertEqual(results[0].detail, "既定値だけ（config.json はありません）")

    def test_an_error_is_ng_with_its_hint_and_source(self):
        path = self.path("config.json")
        self.write_json(path, {"timezone": "Asia/Tokio"})
        config = make_config(self.root, {"timezone": "Asia/Tokio"}, ["<defaults>", path])
        results = check_settings(config)
        error = only(results, "timezone")
        self.assertEqual(error.level, NG)
        self.assertIn(path, error.detail)
        self.assertIn("Asia/Tokyo", error.fix)
        self.assertNotIn("設定の書き方", [result.title for result in results])

    def test_an_unreadable_second_look_is_a_warning_not_a_failure(self):
        config = make_config(self.root, sources=["<defaults>", self.path("gone.json")])
        results = check_settings(config)
        self.assertEqual(only(results, "設定ファイル").level, WARNING)

    def test_redundant_keys_are_info_and_capped(self):
        copied = {"audio": {"gap_ms": 350, "fade_in_ms": 2000, "mock_max_seconds": 3.0},
                  "quotes": {"avoid_recent": 8},
                  "weather": {"timeout_seconds": 8.0, "cache_minutes": 60},
                  "closing": {"extra_text": ""}}
        path = self.path("config.json")
        self.write_json(path, copied)
        config = make_config(self.root, copied, ["<defaults>", path])
        results = check_settings(config)
        infos = [result for result in results if result.level == INFO]
        self.assertEqual(len(infos), check.INFO_LIMIT + 1)
        self.assertIn("あと", infos[-1].detail)
        self.assertEqual(levels(results).count(NG), 0)
        # 情報は問題ではない。
        self.assertEqual(only(results, "設定の書き方").level, OK)


class VoiceSectionTest(InstallCase):
    def remove_voices(self, count):
        """作り置きの声を ``count`` 件消し、消した文言を返す（列挙の順）。"""
        texts = collect_phrases(self.config, include_quotes=True)[:count]
        for text in texts:
            os.remove(self.path("assets", "voice", prerecorded_filename(text)))
        return texts

    def test_missing_voices_are_ng_and_listed(self):
        texts = self.remove_voices(3)
        [result] = check_voices(self.config)
        self.assertEqual(result.level, NG)
        self.assertIn("3 件の声がありません", result.detail)
        self.assertEqual(list(result.lines), ["「{0}」".format(text) for text in texts])
        self.assertIn("docs/SETUP.md 8 章", result.fix)

    def test_at_most_ten_missing_voices_are_listed(self):
        texts = self.remove_voices(14)
        [result] = check_voices(self.config)
        self.assertEqual(list(result.lines[:10]), ["「{0}」".format(text) for text in texts[:10]])
        self.assertEqual(result.lines[-1], "ほか 4 件")
        self.assertEqual(len(result.lines), 11)

    def test_exactly_ten_missing_voices_need_no_overflow_line(self):
        self.remove_voices(10)
        [result] = check_voices(self.config)
        self.assertEqual(len(result.lines), 10)
        self.assertFalse(any("ほか" in line for line in result.lines))

    def test_a_missing_voice_folder_is_ng_without_listing_every_phrase(self):
        shutil.rmtree(self.path("assets", "voice"))
        [result] = check_voices(self.config)
        self.assertEqual(result.level, NG)
        self.assertIn("assets/voice", result.detail)
        self.assertEqual(result.lines, ())

    def test_the_wav_named_by_the_digest_counts_even_without_a_manifest_entry(self):
        """実行時の照合（``PrerecordedEngine.lookup``）と同じく、目録に無くても名前で引ける。"""
        os.remove(self.path("assets", "voice", MANIFEST_FILENAME))
        [result] = check_voices(self.config)
        self.assertEqual(result.level, OK)

    def test_only_the_prerecorded_voices_count_even_when_a_synthesizer_is_configured(self):
        """VOICEVOX の設定があっても、数えるのは作り置きだけ（合成エンジンには問い合わせない）。"""
        self.remove_voices(1)
        requested = block_network(self)
        config = make_config(self.root, {"tts": {"engines": ["voicevox", "prerecorded"]}})
        found = prerecorded_coverage(config)
        self.assertEqual(len(found.missing), 1)
        self.assertEqual(requested, [])

    def test_a_failure_while_counting_is_reported_not_raised(self):
        with mock.patch("chime.check.prerecorded_coverage", side_effect=KeyError("bad")):
            [result] = check_voices(self.config)
        self.assertEqual(result.level, NG)
        self.assertIn("KeyError", result.detail)

    def test_a_count_that_is_too_large_is_reported_in_words_not_by_a_class_name(self):
        """文言が多すぎて数えないとき（``CoverageTooLarge``）は、日本語のメッセージだけを出す。
        例外の型名は利用者に見せない（``--status`` と同じ）。直す設定のキーはメッセージが述べている。"""
        prefix = "読み上げる文言を数えられません: "
        for override, key in (
                ({"weather": {"prerecord": {"temp_max": 10 ** 9}}}, "weather.prerecord.temp_min〜temp_max"),
                ({"weather": {"enabled": False, "sentence_weather": "{label:>200000000}"}},
                 "weather.sentence_weather"),
                ({"weather": {"open_meteo": {"locations": [
                    {"label": "あ" * 100_000, "latitude": 35.0, "longitude": 135.0}]}}},
                 "weather.open_meteo.locations")):
            with self.subTest(key=key):
                config = make_config(self.root, override)
                [result] = check_voices(config)
                self.assertEqual(result.level, NG)
                self.assertTrue(result.detail.startswith(prefix), result.detail)
                self.assertNotIn("CoverageTooLarge", result.detail)
                self.assertNotIn("ValueError", result.detail)
                self.assertIn(key, result.detail)
                self.assertEqual(result.fix, "time_signal・closing・weather の文言の設定を確認してください")
                code, output = self.run_check(config)
                self.assertEqual(code, 1)
                self.assertNotIn("CoverageTooLarge", output)

    def test_the_message_of_a_too_large_count_is_shown_as_it_is(self):
        message = "作り置きする文言が多すぎます（約 9 件。上限は 1 件）。時報の時刻を減らしてください"
        with mock.patch("chime.check.prerecorded_coverage", side_effect=CoverageTooLarge(message)):
            [result] = check_voices(self.config)
        self.assertEqual(result.detail, "読み上げる文言を数えられません: " + message)

    def test_any_other_failure_while_counting_keeps_its_type_name_to_find_the_cause(self):
        """想定していない例外は、メッセージだけでは何のことか分からないので、型名も添える（``--status`` と同じ）。"""
        with mock.patch("chime.check.prerecorded_coverage", side_effect=KeyError("bad")):
            [result] = check_voices(self.config)
        self.assertEqual(result.detail, "読み上げる文言を数えられません: KeyError: 'bad'")

    # -- tts.engines に prerecorded が無いと、Pi では全部が無音 -----------------------
    SILENT = "Pi では読み上げがすべて無音になります"

    def engines_config(self, engines):
        return make_config(self.root, {"tts": {"engines": engines}})

    def test_engines_without_prerecorded_are_ng_even_though_every_voice_is_on_disk(self):
        for engines in (["voicevox"], [], (), ["voicevox", "unknown"], ["Prerecorded"], [" prerecorded"]):
            with self.subTest(engines=engines):
                [result] = check_voices(self.engines_config(engines))
                self.assertEqual(result.level, NG)
                self.assertEqual(result.title, "作り置き")
                self.assertIn(self.SILENT, result.detail)
                self.assertIn("tts.engines", result.fix)
                self.assertIn('"prerecorded"', result.fix)
                self.assertEqual(result.lines, ())

    def test_engines_that_are_not_a_list_are_read_the_way_the_service_reads_them(self):
        """リストでない値は、``TTSService`` が 1 文字（1 キー）ずつの名前として読む。作り置きは使われない。"""
        for engines in ("prerecorded", "prerecorded,voicevox", "", {}, {"voicevox": True}, 5, None, 1.5, True):
            with self.subTest(engines=engines):
                [result] = check_voices(self.engines_config(engines))
                self.assertEqual(result.level, NG)
                self.assertIn(self.SILENT, result.detail)

    def test_engines_with_prerecorded_anywhere_in_the_list_are_not_reported(self):
        for engines in (["prerecorded"], ["voicevox", "prerecorded"], ["prerecorded", "voicevox"],
                        ("prerecorded",), ["x", "prerecorded", "y"]):
            with self.subTest(engines=engines):
                [result] = check_voices(self.engines_config(engines))
                self.assertEqual(result.level, OK)
                self.assertNotIn(self.SILENT, result.detail)

    def test_prerecorded_listed_agrees_with_what_the_service_would_look_up(self):
        """``TTSService.prerecorded_lookup`` が声を返すかどうかと、同じ答えになる。"""
        logging.disable(logging.CRITICAL)  # 知らないエンジン名の警告を出させない
        # 戻す先は tests/__init__.py が全体に掛けている状態（CRITICAL）。NOTSET に戻すと、
        # このあとに動く他のテストのログが端末へ漏れる。
        self.addCleanup(logging.disable, logging.CRITICAL)
        text = collect_phrases(self.config, include_quotes=True)[0]
        for engines in (["prerecorded"], ["voicevox", "prerecorded"], ["voicevox"], [], ("prerecorded",),
                        "prerecorded", "p", {"prerecorded": 1}, {"voicevox": 1}, ["prerecorded", "nonsense"],
                        [7, "prerecorded"], ["prerecorded\n"], ["Prerecorded"]):
            config = self.engines_config(engines)
            service = TTSService(config.section("tts"), self.path("cache", "tts"),
                                 config.path("tts.prerecorded_dir"))
            with self.subTest(engines=engines):
                self.assertEqual(check.prerecorded_listed(config), service.prerecorded_lookup(text) is not None)

    def test_prerecorded_listed_does_not_raise_on_values_the_service_cannot_even_start_with(self):
        for engines in (5, None, 1.5, True, 10 ** 5000):
            with self.subTest(engines=type(engines).__name__):
                self.assertFalse(check.prerecorded_listed(self.engines_config(engines)))

    def test_a_tts_section_that_is_not_a_mapping_lists_nothing(self):
        config = make_config(self.root, {"tts": "prerecorded"})
        self.assertFalse(check.prerecorded_listed(config))

    def test_the_default_settings_list_prerecorded(self):
        self.assertTrue(check.prerecorded_listed(self.config))
        self.assertTrue(check.prerecorded_listed(make_config(self.root)))

    def test_the_whole_check_fails_with_exit_1_when_prerecorded_is_not_listed(self):
        code, output = self.run_check(self.engines_config(["voicevox"]))
        self.assertEqual(code, 1, output)
        self.assertIn(self.SILENT, output)
        self.assertIn("NG 1 件", output)
        self.assertNotIn("件すべてそろっています", output)

    def test_the_voice_section_is_checked_for_the_settings_the_service_runs_with(self):
        """書き方が壊れた ``tts.engines`` は、サービスの既定（prerecorded あり）で動くので、無音とは言わない。"""
        sections = dict(check.collect(self.engines_config(5), euid=PI_UID, account="pi:pi"))
        self.assertEqual([r.level for r in sections["作り置きの音声"]], [OK])

    def test_a_missing_voice_folder_is_still_reported_when_prerecorded_is_listed(self):
        shutil.rmtree(self.path("assets", "voice"))
        [result] = check_voices(self.engines_config(["prerecorded"]))
        self.assertIn("assets/voice", result.detail)

    # -- 置いてあるのに壊れている声は、無いものとして数える ------------------
    def voice_path(self, index=0):
        """``index`` 番目の文言と、その声のファイルのパス。"""
        text = collect_phrases(self.config, include_quotes=True)[index]
        return text, self.path("assets", "voice", prerecorded_filename(text))

    def test_an_empty_voice_file_counts_as_missing(self):
        text, path = self.voice_path()
        open(path, "wb").close()
        [result] = check_voices(self.config)
        self.assertEqual(result.level, NG)
        self.assertIn("1 件の声がありません", result.detail)
        self.assertEqual(list(result.lines), ["「{0}」".format(text)])

    def test_a_voice_file_that_is_not_a_wav_counts_as_missing(self):
        _, path = self.voice_path()
        with open(path, "wb") as handle:
            handle.write(b"garbage")
        [result] = check_voices(self.config)
        self.assertEqual(result.level, NG)
        self.assertIn("1 件の声がありません", result.detail)

    def test_a_voice_file_cut_short_counts_as_missing(self):
        _, path = self.voice_path()
        make_wav(path, seconds=1.0)
        with open(path, "r+b") as handle:
            handle.truncate(os.path.getsize(path) // 2)
        [result] = check_voices(self.config)
        self.assertEqual(result.level, NG)
        self.assertIn("1 件の声がありません", result.detail)

    def test_a_voice_file_that_cannot_be_read_counts_as_missing(self):
        _, path = self.voice_path()
        real_open = check.wave.open

        def deny(target, *args, **kwargs):
            if target == path:
                raise PermissionError(13, "Permission denied")
            return real_open(target, *args, **kwargs)

        with mock.patch("chime.check.wave.open", deny):
            [result] = check_voices(self.config)
        self.assertEqual(result.level, NG)
        self.assertIn("1 件の声がありません", result.detail)

    def test_broken_files_are_said_to_be_broken_and_get_the_git_fix(self):
        _, path = self.voice_path(0)
        open(path, "wb").close()
        os.remove(self.voice_path(1)[1])
        [result] = check_voices(self.config)
        self.assertIn("2 件の声がありません", result.detail)
        self.assertIn("うち 1 件はファイルがあるのに使えません（空・壊れている・読めない）", result.detail)
        self.assertIn("git checkout -- assets/voice", result.fix)
        self.assertIn("docs/SETUP.md 8 章", result.fix)

    def test_a_voice_that_is_only_missing_gets_no_git_fix(self):
        self.remove_voices(1)
        [result] = check_voices(self.config)
        self.assertNotIn("壊れて", result.detail)
        self.assertNotIn("git checkout", result.fix)

    def test_a_broken_voice_file_is_counted_missing_even_when_the_caller_passes_no_list(self):
        """``broken`` を渡さない呼び出し（``chime.status``）でも、壊れたファイルは「無い」ものとして数える。"""
        text = collect_phrases(self.config, include_quotes=True)[0]
        open(self.path("assets", "voice", prerecorded_filename(text)), "wb").close()  # 空のファイル
        found = prerecorded_coverage(self.config)
        self.assertEqual(found.missing, [text])

    def test_the_good_voice_files_are_not_counted_as_broken(self):
        broken = []
        found = prerecorded_coverage(self.config, broken)
        self.assertTrue(found.ok)
        self.assertEqual(broken, [])

    def test_the_broken_files_are_collected_for_the_caller(self):
        text, path = self.voice_path()
        open(path, "wb").close()
        broken = []
        found = prerecorded_coverage(self.config, broken)
        self.assertEqual(broken, [text])
        self.assertEqual(found.missing, [text])


class QuotesSectionTest(InstallCase):
    """ひとこと（``assets/quotes.json``）。読めなくても内蔵の予備で動くので、警告にとどめる。"""

    def test_a_healthy_quotes_file_adds_no_row(self):
        self.assertEqual(check.check_quotes(self.config), [])
        self.assertEqual(len(check_voices(self.config)), 1)

    def test_a_missing_quotes_file_is_a_warning_with_the_git_fix(self):
        os.remove(self.path("assets", "quotes.json"))
        [result] = check.check_quotes(self.config)
        self.assertEqual(result.level, WARNING)
        self.assertEqual(result.title, "ひとこと")
        # リポジトリの中なので、相対パスで言う（絶対パスにしない）。
        self.assertTrue(result.detail.startswith("assets/quotes.json が見つかりません（"), result.detail)
        self.assertIn("内蔵の予備 3 件だけを使います", result.detail)
        self.assertEqual(result.fix, "git checkout -- assets/quotes.json")

    def test_a_quotes_file_that_is_not_json_is_a_warning(self):
        with open(self.path("assets", "quotes.json"), "w", encoding="utf-8") as handle:
            handle.write("{")
        [result] = check.check_quotes(self.config)
        self.assertEqual(result.level, WARNING)
        self.assertIn("JSON の書き方が正しくありません", result.detail)
        self.assertIn("内蔵の予備", result.detail)

    def test_a_quotes_file_that_cannot_be_read_is_a_warning(self):
        os.remove(self.path("assets", "quotes.json"))
        os.mkdir(self.path("assets", "quotes.json"))
        [result] = check.check_quotes(self.config)
        self.assertEqual(result.level, WARNING)
        self.assertIn("読めません", result.detail)

    def test_a_quotes_file_without_any_quote_is_a_warning(self):
        for body in ([], {}, {"general": [], "by_hour": {}}, {"general": "x"}, "text", None):
            self.write_json(self.path("assets", "quotes.json"), body)
            [result] = check.check_quotes(self.config)
            self.assertEqual(result.level, WARNING, body)
            self.assertIn("使えるひとことがありません", result.detail)

    def test_every_shape_the_app_accepts_is_a_usable_quotes_file(self):
        for body in (["あ。"], {"general": ["あ。"]}, {"by_hour": {"12": ["あ。"]}},
                     {"general": [], "by_hour": {"12": ["あ。"]}}):
            self.write_json(self.path("assets", "quotes.json"), body)
            self.assertEqual(check.check_quotes(self.config), [], body)

    def test_a_custom_path_points_at_the_setting_not_git(self):
        config = make_config(self.root, {"quotes": {"file": self.path("elsewhere", "q.json")}})
        [result] = check.check_quotes(config)
        self.assertIn("quotes.file", result.fix)
        self.assertNotIn("git", result.fix)

    def test_an_empty_path_means_not_configured(self):
        config = make_config(self.root, {"quotes": {"file": ""}})
        self.assertEqual(check.check_quotes(config), [])

    def test_the_warning_keeps_the_exit_code_at_0(self):
        os.remove(self.path("assets", "quotes.json"))
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn("結果: 警告 1 件。", output)
        # 作り置きの節に出る
        voices = output[output.index("== 作り置きの音声 =="):output.index("== 音源 ==")]
        self.assertIn("ひとこと", voices)


class SoundSectionTest(InstallCase):
    def sounds(self):
        results = check_sounds(self.config)
        return {result.title: result for result in results}

    def test_the_default_files_are_named_with_their_git_fix(self):
        os.remove(self.path("assets", "announce.wav"))
        result = self.sounds()["閉館アナウンス"]
        self.assertEqual(result.level, NG)
        self.assertIn("見つかりません", result.detail)
        self.assertEqual(result.fix, "git checkout -- assets/announce.wav")

    def test_an_empty_wav_is_ng(self):
        open(self.path("assets", "announce.wav"), "wb").close()
        result = self.sounds()["閉館アナウンス"]
        self.assertEqual(result.level, NG)
        self.assertIn("空のファイル", result.detail)

    def test_a_wav_with_no_frames_is_ng(self):
        make_wav(self.path("assets", "announce.wav"), seconds=0)
        result = self.sounds()["閉館アナウンス"]
        self.assertEqual(result.level, NG)
        self.assertIn("0 フレーム", result.detail)

    def test_a_file_that_is_not_a_wav_is_ng(self):
        with open(self.path("assets", "announce.wav"), "wb") as handle:
            handle.write(b"this is not a wave file at all")
        result = self.sounds()["閉館アナウンス"]
        self.assertEqual(result.level, NG)
        self.assertIn("WAV として開けません", result.detail)

    def test_a_truncated_wav_header_is_ng(self):
        with open(self.path("assets", "announce.wav"), "wb") as handle:
            handle.write(b"RIFF")
        self.assertEqual(self.sounds()["閉館アナウンス"].level, NG)

    def test_an_mp3_with_an_id3_header_is_ok(self):
        self.assertEqual(self.sounds()["蛍の光"].level, OK)

    def test_an_mp3_that_starts_with_an_mpeg_frame_sync_is_ok(self):
        """タグ無しで、最初のフレームから始まる MP3（版もレイヤーもいろいろ）。"""
        for name, header, length in MPEG_FRAMES:
            with open(self.path("assets", "hotaru.mp3"), "wb") as handle:
                handle.write(mpeg_frames(check.MIN_MP3_BYTES // length + 2, header, length))
            self.assertEqual(self.sounds()["蛍の光"].level, OK, name)

    def test_an_mp3_with_neither_header_is_ng(self):
        for body in (b"RIFF....WAVE", b"\xff\x00\x00\x00", b"\x00\x00\x00\x00", b"\xff"):
            with open(self.path("assets", "hotaru.mp3"), "wb") as handle:
                handle.write(body)
            result = self.sounds()["蛍の光"]
            self.assertEqual(result.level, NG, body)
            self.assertIn("MP3 として読めません", result.detail)

    def test_an_empty_mp3_is_ng(self):
        open(self.path("assets", "hotaru.mp3"), "wb").close()
        self.assertEqual(self.sounds()["蛍の光"].level, NG)

    def test_a_missing_time_signal_is_a_warning_with_the_setup_command(self):
        os.remove(self.path("assets", "generated", "time_signal.wav"))
        result = self.sounds()["時報音"]
        self.assertEqual(result.level, WARNING)
        self.assertEqual(result.fix, "bash scripts/setup.sh --no-apt")
        self.assertIn("自動で作ります", result.detail)

    def test_a_missing_time_signal_alone_keeps_the_exit_code_at_0(self):
        os.remove(self.path("assets", "generated", "time_signal.wav"))
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn("結果: 警告 1 件。", output)

    def test_a_time_signal_that_cannot_be_opened_is_ng(self):
        with open(self.path("assets", "generated", "time_signal.wav"), "wb") as handle:
            handle.write(b"garbage")
        result = self.sounds()["時報音"]
        self.assertEqual(result.level, NG)
        self.assertEqual(result.fix, "bash scripts/setup.sh --no-apt")

    def test_a_file_at_a_custom_path_points_at_the_setting_not_git(self):
        config = make_config(self.root, {"closing": {"announce_file": self.path("elsewhere", "x.wav")}})
        result = {r.title: r for r in check_sounds(config)}["閉館アナウンス"]
        self.assertEqual(result.level, NG)
        self.assertIn("closing.announce_file", result.fix)
        self.assertNotIn("git", result.fix)

    def test_an_empty_path_means_not_configured(self):
        config = make_config(self.root, {"closing": {"music_file": ""}})
        result = {r.title: r for r in check_sounds(config)}["蛍の光"]
        self.assertEqual(result.level, OK)
        self.assertIn("指定なし", result.detail)

    def test_a_directory_in_place_of_a_file_is_ng(self):
        os.remove(self.path("assets", "announce.wav"))
        os.mkdir(self.path("assets", "announce.wav"))
        self.assertEqual(self.sounds()["閉館アナウンス"].level, NG)

    # -- ヘッダーは正しいが、中身が最後までそろっていないファイル ---------------
    def truncate(self, name, size):
        with open(self.path("assets", name), "r+b") as handle:
            handle.truncate(size)

    def test_a_wav_cut_short_after_the_header_is_ng(self):
        """電源断で途中までしか書けなかった WAV。``wave`` はヘッダーの宣言どおりに開いてしまう。"""
        make_wav(self.path("assets", "announce.wav"), seconds=1.0)
        size = os.path.getsize(self.path("assets", "announce.wav")) // 2
        self.truncate("announce.wav", size)
        result = self.sounds()["閉館アナウンス"]
        self.assertEqual(result.level, NG)
        self.assertIn("途中で切れています", result.detail)
        # ヘッダー 44 バイトのあとの、16 ビット・モノラルのフレームだけが残っている。
        self.assertIn("宣言 8000 フレームのうち {0} フレーム".format((size - 44) // 2), result.detail)
        self.assertEqual(result.fix, "git checkout -- assets/announce.wav")

    def test_a_wav_with_only_its_44_byte_header_is_ng(self):
        make_wav(self.path("assets", "announce.wav"), seconds=1.0)
        self.truncate("announce.wav", 44)
        result = self.sounds()["閉館アナウンス"]
        self.assertEqual(result.level, NG)
        self.assertIn("途中で切れています", result.detail)

    def test_a_wav_missing_one_byte_is_ng(self):
        make_wav(self.path("assets", "announce.wav"), seconds=0.5)
        self.truncate("announce.wav", os.path.getsize(self.path("assets", "announce.wav")) - 1)
        self.assertEqual(self.sounds()["閉館アナウンス"].level, NG)

    def test_a_complete_wav_is_ok_even_when_longer_than_one_read(self):
        """1 回に読む量（``_READ_FRAMES``）を超える長さでも、最後までそろっていれば OK。"""
        seconds = (check._READ_FRAMES * 2.5) / 8000
        make_wav(self.path("assets", "announce.wav"), seconds=seconds)
        self.assertEqual(self.sounds()["閉館アナウンス"].level, OK)
        self.truncate("announce.wav", os.path.getsize(self.path("assets", "announce.wav")) - 2)
        self.assertEqual(self.sounds()["閉館アナウンス"].level, NG)

    def test_a_wav_cut_inside_its_header_is_ng_whatever_the_length(self):
        make_wav(self.path("assets", "announce.wav"), seconds=0.01)
        with open(self.path("assets", "announce.wav"), "rb") as handle:
            whole = handle.read()
        for size in range(1, 45):
            with open(self.path("assets", "announce.wav"), "wb") as handle:
                handle.write(whole[:size])
            result = self.sounds()["閉館アナウンス"]
            self.assertEqual(result.level, NG, size)

    def test_a_wav_that_cannot_be_read_is_ng_with_the_reason(self):
        with mock.patch("chime.check.wave.open", side_effect=PermissionError(13, "Permission denied")):
            result = self.sounds()["閉館アナウンス"]
        self.assertEqual(result.level, NG)
        self.assertIn("読めません", result.detail)
        self.assertIn("Permission denied", result.detail)

    def test_an_mp3_cut_short_after_a_valid_start_is_ng(self):
        with open(self.path("assets", "hotaru.mp3"), "wb") as handle:
            handle.write(b"ID3" + b"\x00" * 997)  # 1000 バイト
        result = self.sounds()["蛍の光"]
        self.assertEqual(result.level, NG)
        self.assertIn("小さすぎます", result.detail)
        self.assertIn("1000 バイト", result.detail)
        self.assertEqual(result.fix, "git checkout -- assets/hotaru.mp3")

    def test_the_smallest_acceptable_mp3_is_exactly_the_minimum(self):
        """MP3 の形（タグ + フレーム + 小さな 0 埋め）のまま、大きさだけを 1 バイトずつ変える。"""
        whole = id3v2(100) + mpeg_frames(9)
        self.assertLess(check.MIN_MP3_BYTES - len(whole), check._MP3_MAX_PADDING)
        for size, level in ((check.MIN_MP3_BYTES - 1, NG), (check.MIN_MP3_BYTES, OK)):
            with open(self.path("assets", "hotaru.mp3"), "wb") as handle:
                handle.write(whole + b"\x00" * (size - len(whole)))
            result = self.sounds()["蛍の光"]
            self.assertEqual(result.level, level, size)
            if level == NG:
                self.assertIn("小さすぎます", result.detail)

    def test_a_two_byte_file_with_a_frame_sync_is_too_small_not_unreadable(self):
        """先頭が 2 バイトしかなくても、フレーム同期（0xFF 0xFB）なら MP3 の始まりとして読む
        （「MP3 として読めません」ではなく「小さすぎます」）。"""
        with open(self.path("assets", "hotaru.mp3"), "wb") as handle:
            handle.write(b"\xff\xfb")
        self.assertIn("小さすぎます", self.sounds()["蛍の光"].detail)

    def test_an_mp3_that_cannot_be_opened_is_ng(self):
        real_open = open

        def deny(file, *args, **kwargs):
            if str(file) == self.path("assets", "hotaru.mp3"):
                raise PermissionError(13, "Permission denied")
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", deny):
            result = self.sounds()["蛍の光"]
        self.assertEqual(result.level, NG)
        self.assertIn("読めません", result.detail)

    def test_the_shipped_music_is_far_above_the_minimum(self):
        self.assertGreater(os.path.getsize(os.path.join(ASSETS_DIR, "hotaru.mp3")),
                           check.MIN_MP3_BYTES * 10)


class Mp3FramesTest(InstallCase):
    """蛍の光の MP3 を、フレームの最後までたどって確かめる（切れている・途中に壊れた所がある）。"""

    CUT = "途中で切れているか壊れています"

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ASSETS_DIR, "hotaru.mp3"), "rb") as handle:
            cls.shipped = handle.read()

    def music(self, data):
        """``data`` を蛍の光として置き、その行を返す。"""
        with open(self.path("assets", "hotaru.mp3"), "wb") as handle:
            handle.write(data)
        return {result.title: result for result in check_sounds(self.config)}["蛍の光"]

    def assert_cut(self, data, label=None):
        result = self.music(data)
        self.assertEqual(result.level, NG, label)
        self.assertIn(self.CUT, result.detail, label)
        self.assertEqual(result.fix, "git checkout -- assets/hotaru.mp3", label)
        return result

    def assert_fine(self, data, label=None):
        self.assertEqual(self.music(data).level, OK, label)

    NOT_A_FRAME = "フレームでないデータ"
    NO_FRAMES = "ID3 タグのあとに MP3 のフレームがありません"

    # ``walk``（下）はファイルを置かず、フレームをたどる部分だけに渡す（大きさの下限も通らない）。
    def assert_walk_fine(self, data, label=None):
        self.assertIsNone(self.walk(data), label)

    def assert_walk_cut(self, data, label=None, text=None):
        message = self.walk(data)
        self.assertIsNotNone(message, label)
        self.assertIn(text or self.CUT, message, label)

    def assert_no_frames(self, data, label=None):
        """NG で、理由は「タグのあとにフレームが無い」。直し方は ``git checkout``。"""
        result = self.assert_cut(data, label)
        self.assertIn(self.NO_FRAMES, result.detail, label)
        return result

    # -- 同梱の実物 -------------------------------------------------------------
    def test_the_shipped_music_passes_and_is_checked_quickly(self):
        started = time.perf_counter()
        problem = check._sound_problem(os.path.join(ASSETS_DIR, "hotaru.mp3"))
        elapsed = time.perf_counter() - started
        self.assertIsNone(problem)
        self.assertLess(elapsed, 0.5)

    def test_the_shipped_music_cut_anywhere_but_at_a_frame_boundary_is_ng(self):
        size = len(self.shipped)
        for cut in (50000, 1000000, size // 2, size - 1000, size - 129, size - 128 - 1, size - 100,
                    size - 64, size - 1):
            self.assert_cut(self.shipped[:cut], cut)

    @staticmethod
    def walk(data):
        """ファイルを介さずにフレームをたどる（多くの切れ目を速く試す用）。"""
        return check._mp3_frame_problem(io.BytesIO(data), len(data))

    def test_the_shipped_music_cut_at_many_offsets_is_ng_except_at_frame_boundaries(self):
        """切れ目がフレームの境目に当たらない限り、どこで切っても NG（境目は、短いだけの正しい MP3）。"""
        boundaries = set()
        position = 38732  # ID3v2 タグの直後
        while position < len(self.shipped) - 128:
            boundaries.add(position)
            position += check._mpeg_frame_length(self.shipped[position + 1], self.shipped[position + 2])
        cuts = list(range(38700, len(self.shipped), 40009))
        cuts += [len(self.shipped) - back for back in (1, 2, 3, 50, 100, 127, 129, 130, 200, 400)]
        cuts += sorted(boundaries)[1000:1010] + [boundary + 1 for boundary in sorted(boundaries)[1000:1010]]
        for cut in cuts:
            problem = self.walk(self.shipped[:cut])
            if cut in boundaries:
                self.assertIsNone(problem, cut)
            else:
                self.assertIn(self.CUT, problem or "", cut)

    def test_garbage_inserted_at_many_offsets_of_the_shipped_music_is_ng(self):
        for at in range(39000, len(self.shipped) - 128, 400003):
            for garbage in (b"garbage", b"\x00" * 4, os.urandom(33)):
                problem = self.walk(self.shipped[:at] + garbage + self.shipped[at:])
                self.assertIn(self.CUT, problem or "", (at, garbage[:4]))

    def test_garbage_inserted_in_the_middle_of_the_shipped_music_is_ng(self):
        middle = len(self.shipped) // 2
        for garbage in (b"garbage", b"\x00", b"\x00" * 100, b"\xff" * 7, bytes(range(256))):
            self.assert_cut(self.shipped[:middle] + garbage + self.shipped[middle:], garbage[:4])

    def test_the_message_says_where_it_goes_wrong(self):
        result = self.assert_cut(self.shipped[:1000000])
        self.assertIn("999993 バイト目", result.detail)
        result = self.assert_cut(self.shipped[:len(self.shipped) // 2] + b"garbage" + self.shipped[len(self.shipped) // 2:])
        self.assertIn("フレームでないデータ", result.detail)

    # -- 合成した MP3 ------------------------------------------------------------
    def test_a_whole_mp3_is_ok_whatever_the_version_and_layer(self):
        for name, header, length in MPEG_FRAMES:
            count = check.MIN_MP3_BYTES // length + 3
            self.assert_fine(id3v2(100) + mpeg_frames(count, header, length), name)
            self.assert_fine(mpeg_frames(count, header, length), name + "（タグ無し）")

    def test_every_offset_of_a_small_mp3_is_ng_except_the_frame_boundaries(self):
        whole = make_mp3_bytes(frames=12)
        boundaries = {100 + 10 + FRAME_LENGTH * count for count in range(13)}
        for cut in range(check.MIN_MP3_BYTES, len(whole) + 1):
            self.assertEqual(self.walk(whole[:cut]) is None, cut in boundaries, cut)
        self.assert_fine(whole)
        self.assert_cut(whole[:-1])

    def test_the_padding_bit_makes_the_frame_one_byte_longer(self):
        padded = PADDED_FRAME_HEADER + b"\x00" * (FRAME_LENGTH + 1 - 4)
        plain = mpeg_frames(1)
        self.assert_fine(id3v2(10) + plain * 5 + padded + plain * 5 + padded)
        # 長さを 1 つ誤ると、次のフレームの位置がずれて見つからない。
        wrong = PADDED_FRAME_HEADER + b"\x00" * (FRAME_LENGTH - 4)
        self.assert_cut(id3v2(10) + plain * 5 + wrong + plain * 12)

    def test_a_xing_frame_is_an_ordinary_frame(self):
        xing = FRAME_HEADER + b"\x00" * 32 + b"Xing" + b"\x00" * (FRAME_LENGTH - 4 - 32 - 4)
        self.assert_fine(id3v2(100) + xing + mpeg_frames(12))
        self.assert_cut((id3v2(100) + xing + mpeg_frames(12))[:-5])

    def test_a_frame_longer_than_the_chunk_read_and_a_frame_across_chunks_are_followed(self):
        data = id3v2(100) + mpeg_frames(60)
        for chunk in (4, 5, 7, 100, 417, 418, 1000, check._MP3_CHUNK):
            with mock.patch("chime.check._MP3_CHUNK", chunk):
                self.assert_fine(data, chunk)
                self.assert_cut(data[:-1], chunk)

    def test_a_file_longer_than_several_chunks_is_followed_to_the_end(self):
        count = check._MP3_CHUNK * 3 // FRAME_LENGTH + 5
        data = id3v2(100) + mpeg_frames(count)
        self.assertGreater(len(data), check._MP3_CHUNK * 3)
        self.assert_fine(data)
        self.assert_cut(data[:-1])
        self.assert_cut(data[:check._MP3_CHUNK * 2 + 1])

    def test_the_check_reads_the_file_in_chunks_and_each_part_once(self):
        data = id3v2(100) + mpeg_frames(500)
        self.music(data)
        real_open = open
        reads = []

        class Watching:
            def __init__(self, handle):
                self.handle = handle

            def read(self, count=-1):
                reads.append(count)
                return self.handle.read(count)

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return self.handle.__exit__(*exc_info)

        def watching_open(file, *args, **kwargs):
            handle = real_open(file, *args, **kwargs)
            return Watching(handle) if str(file) == self.path("assets", "hotaru.mp3") else handle

        with mock.patch("builtins.open", watching_open):
            self.assertIsNone(check._sound_problem(self.path("assets", "hotaru.mp3")))
        self.assertTrue(reads)
        self.assertNotIn(-1, reads)
        self.assertLessEqual(max(reads), check._MP3_CHUNK)
        self.assertLess(len(reads), 20)  # フレームごとには読まない

    # -- 先頭のタグ ----------------------------------------------------------------
    def test_a_large_id3v2_tag_is_skipped_by_its_syncsafe_size(self):
        for size in (127, 128, 300, 16383, 16384, 38722):
            self.assert_fine(id3v2(size) + mpeg_frames(12), size)

    def test_the_id3v2_4_footer_is_skipped_too(self):
        self.assert_fine(id3v2(200, version=4, footer=True) + mpeg_frames(12))

    def test_the_footer_flag_means_nothing_before_id3v2_4(self):
        """ID3v2.3 では 0x10 のビットは定義されていない。フッターがあるものとして 10 バイト飛ばさない。"""
        tag = bytearray(id3v2(200, version=3))
        tag[5] = 0x10
        self.assert_fine(bytes(tag) + mpeg_frames(12))

    def test_two_id3v2_tags_in_a_row_are_skipped(self):
        self.assert_fine(id3v2(20) + id3v2(30) + mpeg_frames(12))
        self.assert_walk_fine(id3v2(20) * 4 + mpeg_frames(12))

    def test_a_cut_behind_stacked_tags_is_found(self):
        """2 つ目以降のタグの大きさは、そのタグの先頭からの長さ。ファイルの先頭からの位置に足していく
        （そのまま位置にすると、2 つ目のタグから先へ進めず、フレームの切れ・壊れを見逃す）。"""
        cut = mpeg_frames(12)[:-5]
        self.assert_walk_cut(id3v2(20) + id3v2(30) + cut)
        self.assert_walk_cut(id3v2(20) * 4 + cut)
        self.assert_walk_cut(id3v2(500) + id3v2(3000) + id3v2(20) + cut, "長さがそれぞれ違う")
        self.assert_walk_cut(id3v2(20) + id3v2(30) + mpeg_frames(6) + b"garbage" + mpeg_frames(6),
                             "途中の壊れ", self.NOT_A_FRAME)
        self.assert_cut(id3v2(20) + id3v2(30) + cut, "ファイルとして")

    def test_a_second_tag_that_runs_past_the_end_is_a_cut_file(self):
        """ファイルの終わりを越えるかどうかは、先頭からの位置（前のタグの分も足す）で見る。"""
        data = id3v2(20) + id3v2(60000)[:100]
        self.assert_walk_cut(data)
        message = self.walk(data)
        self.assertIn("60040 バイト", message)  # 30 + 10 + 60000
        self.assertIn("ファイルは 130 バイト", message)

    def test_a_second_tag_longer_than_what_is_left_is_a_cut_file_even_if_it_is_shorter_than_the_file(self):
        """切れかどうかは「残り」で決まる。タグの長さが、ファイル全体より短くても、残りより長ければ切れ。"""
        data = id3v2(20) + id3v2(100)[:80]  # 2 つ目のタグは 110 バイト必要で、残りは 80 バイト（ファイル全体は 110 バイト）
        self.assertLessEqual(110, len(data))
        self.assert_walk_cut(data, text="あるはずですが")  # 「フレームが無い」ではなく、タグが切れている

    def test_a_second_tag_that_ends_exactly_at_the_end_of_the_file_is_not_a_cut_file(self):
        """タグがちょうどファイルの終わりで終わるのは、切れではない（フレームが無い、と言う）。"""
        message = self.walk(id3v2(20) + id3v2(30))
        self.assertIn(self.NO_FRAMES, message)
        self.assertNotIn("あるはずですが", message)

    def test_only_four_stacked_tags_are_skipped(self):
        """読み飛ばすタグは 4 つまで。5 つ目以降は読み飛ばさず、その先にフレームがあれば形が分からないとする。"""
        cut = mpeg_frames(12)[:-5]
        self.assert_walk_cut(id3v2(20) * 4 + cut)
        self.assert_walk_fine(id3v2(20) * 5 + cut)
        self.assert_walk_fine(id3v2(20) * 5 + mpeg_frames(12))

    def test_a_tag_ending_in_the_last_nine_bytes_of_the_file_does_not_raise(self):
        """タグの目印のあとが 10 バイトに満たないとき（ヘッダーを読み切れない）も、例外にならない。"""
        message = self.walk(id3v2(5000) + b"ID3\x03\x00\x00\x00\x00\x00")
        self.assertIn(self.NO_FRAMES, message)

    def test_the_syncsafe_size_uses_all_four_bytes(self):
        """2 MiB 以上のタグ（大きさの最上位の 7 ビットが効く）。"""
        tag = id3v2(2 ** 21 + 77)
        self.assert_walk_fine(tag + mpeg_frames(12))
        self.assert_walk_cut(tag + mpeg_frames(12)[:-5])

    def test_the_v24_footer_is_skipped_only_when_the_version_and_the_flag_say_so(self):
        cut = mpeg_frames(12)[:-5]
        with_footer = id3v2(200, version=4, footer=True)
        self.assert_walk_fine(with_footer + mpeg_frames(12))
        self.assert_walk_cut(with_footer + cut)
        # ID3v2.4 でもフッターのフラグが無ければ、飛ばすものは無い
        self.assert_walk_cut(id3v2(200, version=4) + cut)
        # ID3v2.3 では 0x10 のビットはフッターの印ではない
        v3 = b"ID3" + bytes([3, 0, 0x10]) + bytes([0, 0, 1, 72]) + b"\x00" * 200
        self.assert_walk_fine(v3 + mpeg_frames(12))
        self.assert_walk_cut(v3 + cut)

    def test_an_id3v2_tag_that_runs_past_the_end_of_the_file_is_ng(self):
        result = self.assert_cut((id3v2(60000) + mpeg_frames(12))[:20000])
        self.assertIn("ID3 タグ", result.detail)

    def test_a_file_cut_inside_the_first_frame_is_ng(self):
        tag = id3v2(check.MIN_MP3_BYTES)
        for kept in (1, 2, 3, 4, 100, FRAME_LENGTH - 1):
            self.assert_cut(tag + mpeg_frames(1)[:kept], kept)

    # -- 形が分からないときは、先頭の確かめだけ（今までと同じ） --------------------------
    def test_an_mp3_whose_frames_cannot_be_recognised_keeps_the_result_of_the_head_check(self):
        """タグの先でなく、先頭そのものがフレームに見えない・タグの大きさを読めないものは、
        フレームをたどれない。MP3 の形が分からないので、先頭の確かめの結果（OK）のまま。"""
        for label, data in (
                ("フリーフォーマット（ビットレート指数 0）", b"\xff\xfb\x00\x00" + b"\x00" * check.MIN_MP3_BYTES),
                ("予約された版", b"\xff\xeb\x90\x00" + b"\x00" * check.MIN_MP3_BYTES),
                ("予約されたレイヤー", b"\xff\xf9\x90\x00" + b"\x00" * check.MIN_MP3_BYTES),
                ("ビットレート指数 15", b"\xff\xfb\xf0\x00" + b"\x00" * check.MIN_MP3_BYTES),
                ("予約された周波数", b"\xff\xfb\x9c\x00" + b"\x00" * check.MIN_MP3_BYTES),
                ("syncsafe でない大きさ", b"ID3\x03\x00\x00\xff\xff\xff\xff" + b"\x00" * check.MIN_MP3_BYTES)):
            self.assert_fine(data, label)

    def test_one_non_syncsafe_size_byte_means_the_shape_is_unknown(self):
        """大きさの 4 バイトのうち 1 つでも最上位ビットが立っていれば、タグとして読まない（どれか 1 つで足りる）。"""
        head = b"ID3\x03\x00\x00" + b"\x00\x00\x00\x80"
        self.assert_walk_fine(head + b"\x00" * 128 + mpeg_frames(12)[:-5])

    # -- ID3 タグは正しいのに、そのあとにフレームが無い（電源断で、タグまでしか書けなかった） ------
    def test_a_tag_followed_by_zeros_to_the_full_size_is_ng(self):
        """大きさだけが記録され、最初のブロック（タグ）しかディスクに届かなかったファイル。"""
        self.assert_no_frames(id3v2(60000) + b"\x00" * 100000, "タグ + 0")
        self.assert_no_frames(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * check.MIN_MP3_BYTES, "大きさ 0 のタグ + 0")

    def test_a_tag_followed_by_data_that_is_not_a_frame_is_ng(self):
        self.assert_no_frames(id3v2(100) + b"x" * check.MIN_MP3_BYTES)
        self.assert_no_frames(id3v2(100) + b"\xff" * check.MIN_MP3_BYTES, "同期だけが並ぶ")

    def test_a_file_that_is_only_its_tag_is_ng(self):
        self.assert_no_frames(id3v2(check.MIN_MP3_BYTES), "タグだけ")
        for extra in (b"\x00", b"\x00\x00\x00", b"\xff\xfb\x90"):
            self.assert_no_frames(id3v2(check.MIN_MP3_BYTES) + extra, extra)

    def test_stacked_tags_with_nothing_behind_them_are_ng(self):
        self.assert_no_frames(id3v2(20) + id3v2(30) + b"\x00" * check.MIN_MP3_BYTES)
        self.assert_no_frames(id3v2(check.MIN_MP3_BYTES // 4) * 4, "タグだけが 4 つ")

    def test_the_shipped_music_with_only_its_first_blocks_written_is_ng(self):
        """同梱の蛍の光が、先頭のブロックだけ書けて残りが 0 のとき（タグの途中・ちょうど終わり・直後まで）。"""
        sizes = self.shipped[6:10]
        tag_end = 10 + ((sizes[0] << 21) | (sizes[1] << 14) | (sizes[2] << 7) | sizes[3])
        for kept in (4096, 8192, tag_end - 1, tag_end, tag_end + 1, tag_end + 2):
            self.assert_no_frames(self.shipped[:kept] + b"\x00" * (len(self.shipped) - kept), kept)
        # 最初のフレームのヘッダーまで書けていれば、フレームをたどって「フレームでないデータ」で見つかる
        kept = tag_end + 4
        self.assert_cut(self.shipped[:kept] + b"\x00" * (len(self.shipped) - kept), kept)

    def test_the_message_names_the_git_fix(self):
        result = self.assert_no_frames(id3v2(100) + b"x" * check.MIN_MP3_BYTES)
        self.assertIn(self.CUT, result.detail)
        self.assertEqual(result.fix, "git checkout -- assets/hotaru.mp3")

    # -- タグのあとに少し余分なデータがあっても、フレームが続けて 2 つ見つかれば MP3 の形 -------------
    def test_junk_between_the_tag_and_the_frames_is_not_judged(self):
        """タグの大きさに数えられていない 0 埋めや余分なデータが挟まる MP3。フレームは見つかるので、
        形が分からないとして先頭の確かめの結果（OK）のまま。"""
        for junk in (b"\x00" * 3, b"\x00" * 500, b"x" * 5000, b"\xff\x00" * 100):
            self.assert_fine(id3v2(100) + junk + mpeg_frames(12), len(junk))
        self.assert_fine(id3v2(20) + id3v2(30) + b"\x00" * 500 + mpeg_frames(12), "タグが 2 つ")

    def test_the_frames_are_searched_for_in_a_bounded_window(self):
        """タグの直後の ``_MP3_FRAME_SEARCH_BYTES`` バイトの中に、2 つのヘッダーがどちらも収まること。"""
        window = check._MP3_FRAME_SEARCH_BYTES
        last_inside = window - FRAME_LENGTH - 4  # 2 つ目のヘッダーの 4 バイトが、ちょうど窓の終わりに収まる
        self.assert_fine(id3v2(100) + b"\x00" * last_inside + mpeg_frames(12), "ちょうど収まる")
        self.assert_no_frames(id3v2(100) + b"\x00" * (last_inside + 1) + mpeg_frames(12), "1 バイト外")
        self.assert_no_frames(id3v2(100) + b"\x00" * (window + 1000) + mpeg_frames(12), "窓の外")
        # 範囲そのものは 64 KiB（数字で固定する）
        self.assert_fine(id3v2(100) + b"\x00" * 60000 + mpeg_frames(12), "60000 バイト先")
        self.assert_no_frames(id3v2(100) + b"\x00" * 70000 + mpeg_frames(12), "70000 バイト先")

    def test_a_stray_ff_just_before_the_frames_does_not_hide_them(self):
        """ヘッダーの形でない 0xFF のすぐ後ろから本物のヘッダーが始まっても、次の 0xFF から探し直す。"""
        self.assert_walk_fine(id3v2(100) + b"\xff" + mpeg_frames(2))
        self.assert_walk_fine(id3v2(100) + b"\xff\xff\xff" + mpeg_frames(2))

    def test_a_header_shape_without_the_sync_byte_is_not_a_frame_to_the_search(self):
        """次のヘッダーの位置に 0xFF が無ければ、その後ろが 0xFB 0x90 でも数えない。"""
        self.assert_no_frames(id3v2(100) + b"\x00" + mpeg_frames(1) + b"\x00\xfb\x90\x00" * 1500)

    def test_one_frame_header_alone_is_not_enough(self):
        """フレームの形のバイトが 1 つあるだけでは（偶然の一致かもしれないので）MP3 の形とみなさない。"""
        self.assert_no_frames(id3v2(100) + b"\x00" * 10 + mpeg_frames(1) + b"\x00" * check.MIN_MP3_BYTES)

    def test_two_headers_that_do_not_follow_each_other_are_not_enough(self):
        """最初のヘッダーが示す長さの先に、次のヘッダーがなければ数えない（1 バイトずれていてもだめ）。"""
        self.assert_no_frames(id3v2(100) + b"\x00" + (mpeg_frames(1) + b"\x00") * 12)

    def test_a_first_frame_followed_by_zeros_is_a_broken_mp3(self):
        """最初のフレームが正しければ、フレームをたどる。その先が 0 ばかりなら、切れている・壊れている。"""
        self.assert_cut(FRAME_HEADER + b"\x00" * check.MIN_MP3_BYTES)

    # -- ファイルの終わりに付いていてよいもの ------------------------------------------
    def test_a_trailing_id3v1_tag_is_fine(self):
        self.assert_fine(make_mp3_bytes(trailer=ID3V1))

    def test_an_id3v1_tag_that_is_not_exactly_128_bytes_is_not_fine(self):
        for size in (1, 3, 28, 100, 127, 129, 200):
            self.assert_cut(make_mp3_bytes(trailer=(ID3V1 + b"x" * 80)[:size]), size)

    def test_an_id3v1_tag_in_the_middle_of_the_stream_is_not_fine(self):
        self.assert_cut(make_mp3_bytes(frames=6) + ID3V1 + mpeg_frames(6))

    def test_a_small_zero_padding_is_fine(self):
        for size in (1, 2, 3, 4, 100, check._MP3_MAX_PADDING):
            self.assert_fine(make_mp3_bytes(trailer=b"\x00" * size), size)
            self.assert_fine(make_mp3_bytes(trailer=b"\x00" * size + ID3V1), size)

    def test_the_zero_padding_limit_is_exactly_1024_bytes(self):
        """定数ではなく数字そのもので固定する（1024 バイトまでが 0 埋め、1025 バイトからは切れ）。"""
        whole = make_mp3_bytes(frames=12)
        self.assert_walk_fine(whole + b"\x00" * 1024)
        self.assert_walk_cut(whole + b"\x00" * 1025)

    def test_a_long_zero_tail_is_a_file_that_was_extended_but_not_written(self):
        """電源断のあと、長さだけ伸びて中身が 0 のファイル（ブロック単位）。"""
        for size in (check._MP3_MAX_PADDING + 1, 4096, 8192):
            self.assert_cut(make_mp3_bytes(trailer=b"\x00" * size), size)

    def test_zeros_between_frames_are_not_padding(self):
        self.assert_cut(make_mp3_bytes(frames=6) + b"\x00" * 4 + mpeg_frames(6))

    def test_non_zero_bytes_after_the_last_frame_are_not_fine(self):
        for trailer in (b"x", b"garbage", b"\x00\x00\x01", b"\xff\xff\xff\xff"):
            self.assert_cut(make_mp3_bytes(trailer=trailer), trailer)

    @staticmethod
    def ape(items=b"\x00" * 40, header=True):
        size = len(items) + 32  # 大きさは、項目とフッターの分（ヘッダーは入れない）
        block = b"APETAGEX" + (2000).to_bytes(4, "little") + size.to_bytes(4, "little") + b"\x00" * 16
        return (block if header else b"") + items + block

    def test_a_trailing_ape_tag_is_fine_with_or_without_a_header_and_before_id3v1(self):
        for header in (True, False):
            for id3v1 in (b"", ID3V1):
                data = make_mp3_bytes(trailer=self.ape(header=header) + id3v1)
                self.assert_fine(data, (header, bool(id3v1)))

    def test_an_ape_tag_that_starts_with_its_header_is_metadata_whatever_it_says_about_its_size(self):
        self.assert_fine(make_mp3_bytes(trailer=b"APETAGEX" + b"\x00" * 24))
        self.assert_fine(make_mp3_bytes(trailer=b"APETAGEX" + b"x" * 100))

    def test_a_headerless_ape_tag_must_end_with_a_footer_that_reaches_back_to_the_last_frame(self):
        footer_only = self.ape(header=False)
        self.assert_cut(make_mp3_bytes(trailer=b"x" + footer_only))  # フッターの大きさが 1 バイト足りない
        self.assert_cut(make_mp3_bytes(trailer=footer_only[:-1]))  # フッターが欠けている

    def test_a_cut_ape_header_is_not_fine(self):
        self.assert_cut(make_mp3_bytes(trailer=self.ape()[:20]))

    def test_an_apev1_style_footer_with_a_nonzero_item_count_is_fine(self):
        """フッターの項目数（12〜16 バイト目）は、大きさに混ぜて読まない。"""
        items = b"\x00" * 48
        footer = (b"APETAGEX" + (1000).to_bytes(4, "little") + (len(items) + 32).to_bytes(4, "little")
                  + (3).to_bytes(4, "little") + b"\x00" * 12)
        self.assertEqual(len(footer), 32)
        self.assert_walk_fine(make_mp3_bytes() + items + footer)

    def test_a_trailer_that_has_the_size_of_an_ape_footer_but_not_its_magic_is_garbage(self):
        footer = b"XXXXXXXX" + b"\x00" * 4 + (40).to_bytes(4, "little") + b"\x00" * 16
        self.assertEqual(len(footer), 32)
        self.assert_walk_cut(make_mp3_bytes() + b"\x00" * 8 + footer, text=self.NOT_A_FRAME)

    # -- フレームの長さ・チャンクの境目 ----------------------------------------------------
    def test_the_padding_bit_of_a_layer_1_frame_adds_four_bytes(self):
        header = b"\xff\xff\xc2\x00"  # MPEG-1 レイヤー I、384 kbps、44.1 kHz、パディングあり
        self.assertEqual(check._mpeg_frame_length(header[1], header[2]), 420)
        self.assert_walk_fine(mpeg_frames(12, header, 420))

    def test_the_padding_bit_of_an_mpeg2_layer_3_frame_adds_one_byte(self):
        header = b"\xff\xf3\x92\x00"  # MPEG-2 レイヤー III、80 kbps、22.05 kHz、パディングあり
        self.assertEqual(check._mpeg_frame_length(header[1], header[2]), 262)
        self.assert_walk_fine(mpeg_frames(12, header, 262))

    def test_frames_of_different_layers_in_one_stream_each_use_their_own_length(self):
        """ヘッダーの 2 バイト目だけが違うフレーム（レイヤー III と II）が混ざっても、長さを取り違えない。"""
        layer3 = (b"\xff\xfb\x90\x00", 417)
        layer2 = (b"\xff\xfd\x90\x00", 522)
        self.assert_walk_fine((mpeg_frames(1, *layer3) + mpeg_frames(1, *layer2)) * 6)

    def test_every_valid_header_has_the_length_the_standard_gives(self):
        """規格（ISO/IEC 11172-3、13818-3）の表を、ここに別に書き写して照らす（``chime.check`` の表は読まない）。"""
        mpeg1 = {1: (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
                 2: (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
                 3: (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)}
        mpeg2 = {1: (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
                 2: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
                 3: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)}
        rates = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
        checked = 0
        for version in (3, 2, 0):
            for layer in (1, 2, 3):
                table = (mpeg1 if version == 3 else mpeg2)[layer]
                for bitrate_index in range(1, 15):
                    for rate_index in range(3):
                        for padding in (0, 1):
                            second = 0xE0 | (version << 3) | ((4 - layer) << 1) | 1
                            third = (bitrate_index << 4) | (rate_index << 2) | (padding << 1)
                            bitrate = table[bitrate_index - 1] * 1000
                            rate = rates[version][rate_index]
                            if layer == 1:
                                expected = (12 * bitrate // rate + padding) * 4
                            elif layer == 3 and version != 3:
                                expected = 72 * bitrate // rate + padding
                            else:
                                expected = 144 * bitrate // rate + padding
                            self.assertEqual(check._mpeg_frame_length(second, third), expected,
                                             (version, layer, bitrate_index, rate_index, padding))
                            checked += 1
        self.assertEqual(checked, 3 * 3 * 14 * 3 * 2)

    def test_a_header_split_by_the_chunk_boundary_is_never_mistaken_for_garbage(self):
        """読む単位（チャンク）の境目がヘッダーの途中に来る大きさを、すべて試す（417 バイトごと + 3 で差が出る）。"""
        data = id3v2(100) + mpeg_frames(6)
        for chunk in range(4, 1300):
            with mock.patch("chime.check._MP3_CHUNK", chunk):
                self.assertIsNone(self.walk(data), chunk)

    def test_one_to_three_trailing_bytes_that_look_like_a_frame_start_are_garbage_not_a_crash(self):
        for kept in (1, 2, 3):
            self.assert_walk_cut(make_mp3_bytes() + b"\xff\xfb\x90"[:kept], kept, self.NOT_A_FRAME)

    # -- 周辺 ---------------------------------------------------------------------
    def test_an_mp3_under_the_minimum_size_is_still_too_small_even_when_it_is_whole(self):
        data = id3v2(10) + mpeg_frames(3)
        self.assertLess(len(data), check.MIN_MP3_BYTES)
        result = self.music(data)
        self.assertEqual(result.level, NG)
        self.assertIn("小さすぎます", result.detail)

    def test_a_walk_that_cannot_read_is_ng_with_the_reason(self):
        data = make_mp3_bytes()
        self.music(data)
        real_open = open

        def failing(file, *args, **kwargs):
            handle = real_open(file, *args, **kwargs)
            if str(file) == self.path("assets", "hotaru.mp3"):
                handle.seek = mock.Mock(side_effect=OSError(5, "Input/output error"))
            return handle

        with mock.patch("builtins.open", failing):
            result = {r.title: r for r in check_sounds(self.config)}["蛍の光"]
        self.assertEqual(result.level, NG)
        self.assertIn("読めません", result.detail)

    def test_the_frame_length_of_every_valid_header_is_known_and_an_invalid_one_is_zero(self):
        for name, header, length in MPEG_FRAMES:
            self.assertEqual(check._mpeg_frame_length(header[1], header[2]), length, name)
        self.assertEqual(check._mpeg_frame_length(PADDED_FRAME_HEADER[1], PADDED_FRAME_HEADER[2]), FRAME_LENGTH + 1)
        invalid = {
            "同期が足りない": (0xDB, 0x90), "予約された版": (0xEB, 0x90), "予約されたレイヤー": (0xF9, 0x90),
            "フリーフォーマット": (0xFB, 0x00), "ビットレート指数 15": (0xFB, 0xF0),
            "予約された周波数": (0xFB, 0x9C)}
        for name, (second, third) in invalid.items():
            self.assertEqual(check._mpeg_frame_length(second, third), 0, name)

    def test_every_valid_header_has_a_positive_length(self):
        """ヘッダーの組み合わせ（2 バイト目の下位 5 ビット × 3 バイト目）を全部試し、例外が出ないこと。"""
        for second in range(0xE0, 0x100):
            for third in range(256):
                length = check._mpeg_frame_length(second, third)
                self.assertGreaterEqual(length, 0)
                if length:
                    self.assertGreaterEqual(length, 4)


class TimeSignalCreationTest(InstallCase):
    """時報音が無いとき、放送で自動で作れるか（作る場所に書けるか）。"""

    def setUp(self):
        super().setUp()
        os.remove(self.path("assets", "generated", "time_signal.wav"))
        self.generated = self.path("assets", "generated")

    def result(self, config=None, unwritable=()):
        with mock.patch("chime.check._can_write", side_effect=lambda p: p not in unwritable):
            results = check_sounds(config or self.config, euid=PI_UID, account="pi:pi")
        return {r.title: r for r in results}["時報音"]

    def test_missing_with_a_writable_folder_is_only_a_warning(self):
        result = self.result()
        self.assertEqual(result.level, WARNING)
        self.assertEqual(result.fix, "bash scripts/setup.sh --no-apt")

    def test_missing_when_the_folder_does_not_exist_yet_but_can_be_made_is_a_warning(self):
        os.rmdir(self.generated)
        self.assertEqual(self.result().level, WARNING)

    def test_missing_in_a_folder_that_cannot_be_written_is_ng_because_it_cannot_be_made(self):
        self.owners[self.generated] = 0
        result = self.result(unwritable={self.generated})
        self.assertEqual(result.level, NG)
        self.assertIn("作れません", result.detail)
        self.assertIn("時報音が鳴りません", result.detail)
        self.assertEqual(result.fix, "sudo chown -R pi:pi {0}".format(self.generated))

    def test_missing_in_a_folder_to_be_made_under_an_unwritable_parent_creates_only_that_folder(self):
        os.rmdir(self.generated)
        assets = self.path("assets")
        result = self.result(unwritable={assets})
        self.assertEqual(result.level, NG)
        self.assertEqual(result.fix, "sudo mkdir -p {0} && sudo chown pi:pi {0}".format(self.generated))

    def test_a_file_in_the_way_of_the_folder_is_ng(self):
        os.rmdir(self.generated)
        open(self.generated, "w").close()
        result = self.result()
        self.assertEqual(result.level, NG)
        self.assertIn("フォルダではない", result.detail)

    def test_an_existing_time_signal_needs_no_writable_folder(self):
        make_wav(self.path("assets", "generated", "time_signal.wav"))
        result = self.result(unwritable={self.generated})
        self.assertEqual(result.level, OK)


class WritableSectionTest(InstallCase):
    def results(self, euid=PI_UID, account="pi:pi", config=None):
        return check_writable(config or self.config, euid=euid, account=account)

    def unwritable(self, *paths):
        """``paths`` だけ書けないことにする（os.access の結果の差し替え）。"""
        return mock.patch("chime.check._can_write", side_effect=lambda p: p not in paths)

    def only_these_exist(self, *directories):
        """絶対パスで書いた場所は、``directories``（と祖先）だけが実在することにする。

        ``/nonexistent`` や ``/etc/chime`` のように「まだ無い」前提の場所が、この機械にたまたま
        あっても（以前の実行や別のソフトが作ったもの）、結果が変わらないようにする。一時フォルダの
        中は実際のファイルシステムで調べる。
        """
        existing = {"/"}
        for directory in directories:
            while directory not in existing:
                existing.add(directory)
                directory = os.path.dirname(directory)
        real = check._nearest_existing

        def nearest(path):
            if path == self.root or path.startswith(self.root + os.sep):
                return real(path)
            while path not in existing:
                path = os.path.dirname(path)
            return path

        return mock.patch("chime.check._nearest_existing", side_effect=nearest)

    def test_writable_directories_are_ok(self):
        results = self.results()
        self.assertEqual(only(results, "cache/").level, OK)
        self.assertEqual(only(results, "cache/tts/").level, OK)
        self.assertEqual(levels(results), [OK, OK])

    def test_no_row_is_about_owners_because_ownership_alone_blocks_nothing(self):
        """所有者の行は無い。書けないことは、フォルダや履歴の行が、その場所を挙げて言う。"""
        titles = [result.title for result in self.results()]
        self.assertNotIn("所有者", titles)

    def test_a_directory_that_does_not_exist_yet_is_ok_when_its_parent_is_writable(self):
        shutil.rmtree(self.path("cache"))
        result = only(self.results(), "cache/")
        self.assertEqual(result.level, OK)
        self.assertIn("初回の放送で作ります", result.detail)

    def test_a_cache_only_directory_that_does_not_exist_yet_says_when_it_is_made(self):
        shutil.rmtree(self.path("cache", "tts"))
        result = only(self.results(), "cache/tts/")
        self.assertEqual(result.level, OK)
        self.assertIn("合成した音声を保存するときに作ります", result.detail)

    # -- 直し方：設定が指す場所そのものにしか chown -R しない ---------------------
    def test_a_directory_that_cannot_be_written_is_ng_with_chown_on_that_directory_only(self):
        self.owners[self.path("cache")] = 0
        with self.unwritable(self.path("cache")):
            result = only(self.results(), "cache/")
        self.assertEqual(result.level, NG)
        self.assertEqual(result.fix, "sudo chown -R pi:pi {0}".format(self.path("cache")))

    def test_an_unwritable_directory_the_user_owns_is_a_mode_problem_fixed_with_chmod(self):
        """持ち主が自分なのに書けないのは権限（モード）の問題。chown ではなく chmod で直す。"""
        self.owners[self.path("cache")] = PI_UID
        with self.unwritable(self.path("cache")):
            result = only(self.results(), "cache/")
        self.assertEqual(result.level, NG)
        self.assertEqual(result.fix, "chmod u+rwx {0}".format(self.path("cache")))

    def test_a_missing_directory_under_an_unwritable_parent_is_made_and_given_away_only_itself(self):
        shutil.rmtree(self.path("cache"))
        with self.unwritable(self.root):
            result = only(self.results(), "cache/")
        self.assertEqual(result.level, NG)
        self.assertEqual(result.fix, "sudo mkdir -p {0} && sudo chown pi:pi {0}".format(self.path("cache")))
        self.assertIn("まだ無く", result.detail)
        self.assertIn("作れません", result.detail)

    def test_a_missing_target_never_gets_a_recursive_chown_whichever_setting_points_at_it(self):
        """どの設定が指していても、まだ無い場所は、その 1 つだけを作って渡す（``/`` などの祖先は巻き込まない）。"""
        cases = {"state.file": ({"state": {"file": "/nonexistent/a/state.json"}}, "/nonexistent/a"),
                 "state.history_file": ({"state": {"history_file": "/nonexistent/b/h.jsonl"}},
                                        "/nonexistent/b"),
                 "tts.cache_dir": ({"tts": {"cache_dir": "/nonexistent/c/tts"}}, "/nonexistent/c/tts")}
        for key, (override, directory) in cases.items():
            config = make_config(self.root, override)
            with self.subTest(key=key), self.only_these_exist("/"), \
                    mock.patch("chime.check._can_write", return_value=False):
                result = only(self.results(config=config), directory + "/")
            self.assertNotEqual(result.level, OK)
            self.assertEqual(result.fix, "sudo mkdir -p {0} && sudo chown pi:pi {0}".format(directory))
            self.assertNotIn("-R", result.fix)

    def test_a_state_file_under_a_missing_top_level_directory_makes_only_that_directory(self):
        config = make_config(self.root, {"state": {"file": "/nonexistent/dir/state.json"}})
        with self.only_these_exist("/"), self.unwritable("/"):
            results = self.results(config=config)
        result = only(results, "/nonexistent/dir/")
        self.assertEqual(result.level, NG)
        self.assertEqual(result.fix,
                         "sudo mkdir -p /nonexistent/dir && sudo chown pi:pi /nonexistent/dir")

    def test_a_cache_dir_under_var_lib_does_not_take_over_var_lib(self):
        config = make_config(self.root, {"tts": {"cache_dir": "/var/lib/chime/tts"}})
        with self.only_these_exist("/var/lib"), self.unwritable("/var/lib"):
            result = only(self.results(config=config), "/var/lib/chime/tts/")
        self.assertEqual(result.fix,
                         "sudo mkdir -p /var/lib/chime/tts && sudo chown pi:pi /var/lib/chime/tts")

    def test_a_history_file_under_etc_does_not_take_over_etc(self):
        config = make_config(self.root, {"state": {"history_file": "/etc/chime/history.jsonl"}})
        with self.only_these_exist("/etc"), self.unwritable("/etc"):
            result = only(self.results(config=config), "/etc/chime/")
        self.assertEqual(result.fix, "sudo mkdir -p /etc/chime && sudo chown pi:pi /etc/chime")

    def test_an_existing_operating_system_directory_is_never_given_away(self):
        """設定が ``/`` などを指していても、持ち主を変える案内（再帰でも、なしでも）は出さない。"""
        for directory in ("/", "/etc", "/var/lib", "/usr"):
            config = make_config(self.root, {"state": {"file": directory.rstrip("/") + "/state.json"}})
            with self.subTest(directory=directory), self.only_these_exist(directory), self.unwritable(directory):
                result = only(self.results(config=config), directory.rstrip("/") + "/")
            self.assertEqual(result.level, NG)
            self.assertNotIn("chown", result.fix)
            self.assertNotIn("chmod", result.fix)
            self.assertIn("state.file", result.fix)
            self.assertIn("設定", result.fix)

    def test_paths_with_spaces_are_quoted_in_the_advice(self):
        config = make_config(self.root, {"state": {"file": "/no such/dir/state.json"}})
        with self.only_these_exist("/"), self.unwritable("/"):
            result = only(self.results(config=config), "/no such/dir/")
        self.assertEqual(
            result.fix, "sudo mkdir -p '/no such/dir' && sudo chown pi:pi '/no such/dir'")

    def test_a_file_in_the_way_of_the_directory_is_ng(self):
        shutil.rmtree(self.path("cache"))
        open(self.path("cache"), "w").close()
        result = only(self.results(), "cache/")
        self.assertEqual(result.level, NG)
        self.assertIn("フォルダではありません", result.detail)

    # -- NG にするのは、再生済みの記録を残せなくなるものだけ ---------------------
    def test_a_state_directory_that_cannot_be_written_says_what_goes_wrong(self):
        with self.unwritable(self.path("cache")):
            result = only(self.results(), "cache/")
        self.assertEqual(result.level, NG)
        self.assertIn("再生済みの記録を保存できません", result.detail)
        self.assertIn("同じ回が鳴り直すことがあります", result.detail)

    def test_a_history_only_directory_that_cannot_be_written_is_a_warning(self):
        config = make_config(self.root, {"state": {"history_file": self.path("logs", "history.jsonl")}})
        os.makedirs(self.path("logs"))
        with self.unwritable(self.path("logs")):
            result = only(self.results(config=config), "logs/")
        self.assertEqual(result.level, WARNING)
        self.assertIn("放送は止まりません", result.detail)

    def test_a_cache_only_directory_that_cannot_be_written_is_a_warning(self):
        with self.unwritable(self.path("cache", "tts")):
            result = only(self.results(), "cache/tts/")
        self.assertEqual(result.level, WARNING)
        self.assertIn("作り置きだけで放送する Pi には影響しません", result.detail)

    def test_a_directory_shared_by_state_and_history_is_ng_as_the_heavier_role(self):
        self.assertEqual(check.write_roles(self.config)[self.path("cache")], (check.STATE, check.HISTORY))
        with self.unwritable(self.path("cache")):
            self.assertEqual(only(self.results(), "cache/").level, NG)

    def test_an_unwritable_cache_directory_alone_keeps_the_exit_code_at_0(self):
        out = io.StringIO()
        with self.unwritable(self.path("cache", "tts")):
            code = run_check(self.config, out=out, euid=PI_UID, account="pi:pi")
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("結果: 警告 1 件。", out.getvalue())

    # -- ファイル：置き場所のフォルダに書ければ、所有者や権限は妨げにならない -------
    def test_the_app_replaces_a_read_only_state_file_so_it_is_not_a_problem(self):
        """前提の確認：アプリは一時ファイルから置き換えて保存するので、書けないファイルでも記録できる。"""
        from chime.state import State
        path = self.path("cache", "state.json")
        self.write_json(path, {"last_fired": {"hourly:10": "2026-10-08"}})
        os.chmod(path, 0o444)
        state = State(path)
        state.mark_fired("hourly:11", "2026-10-09")
        self.assertTrue(State(path).is_fired("hourly:11", "2026-10-09"))
        self.assertTrue(State(path).is_fired("hourly:10", "2026-10-08"))

    def test_a_state_file_that_is_not_writable_in_a_writable_directory_is_not_reported(self):
        self.write_json(self.path("cache", "state.json"), {})
        with self.unwritable(self.path("cache", "state.json")):
            results = self.results()
        self.assertEqual(levels(results), [OK, OK])
        self.assertNotIn("cache/state.json", [result.title for result in results])

    def test_a_root_owned_state_file_in_a_writable_directory_is_not_ng(self):
        self.write_json(self.path("cache", "state.json"), {})
        self.owners[self.path("cache", "state.json")] = 0
        self.assertEqual(levels(self.results()), [OK, OK])
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertNotIn("root", output)

    # -- sticky ビットのフォルダ：他人のファイルは、置き換えられない -------------------
    def sticky_state(self, directory_owner=0, file_owner=0, sticky=True, create_file=True):
        """``cache/`` を（sticky ビット付きで）用意し、``state.json`` を置く。持ち主は差し替える。"""
        cache = self.path("cache")
        os.chmod(cache, 0o1777 if sticky else 0o777)
        self.owners[os.path.realpath(cache)] = directory_owner
        if create_file:
            self.write_json(self.path("cache", "state.json"), {})
            self.owners[self.path("cache", "state.json")] = file_owner

    def test_a_state_file_nobody_here_owns_in_a_sticky_directory_nobody_here_owns_is_ng(self):
        """OS は、sticky ビットのフォルダで他人のファイルの置き換えを断る（``/tmp`` に置いた場合など）。"""
        self.sticky_state()
        result = only(self.results(), "cache/state.json")
        self.assertEqual(result.level, NG)
        self.assertIn("sticky", result.detail)
        self.assertIn("再生済みの記録を保存できません", result.detail)
        self.assertEqual(result.fix.split("（")[0], "sudo chown pi:pi {0}".format(self.path("cache", "state.json")))
        self.assertIn("state.file", result.fix)
        self.assertNotIn("-R", result.fix)
        code, output = self.run_check()
        self.assertEqual(code, 1, output)
        self.assertIn("sticky", output)

    def test_a_sticky_directory_is_fine_when_the_user_owns_the_directory(self):
        self.sticky_state(directory_owner=PI_UID, file_owner=0)
        self.assertEqual(levels(self.results()), [OK, OK])

    def test_a_sticky_directory_is_fine_when_the_user_owns_the_state_file(self):
        self.sticky_state(directory_owner=0, file_owner=PI_UID)
        self.assertEqual(levels(self.results()), [OK, OK])

    def test_a_sticky_directory_is_fine_when_there_is_no_state_file_yet(self):
        """まだ無ければ、新しく作れる（置き換えるのではない）。"""
        self.sticky_state(create_file=False)
        self.assertEqual(levels(self.results()), [OK, OK])

    def test_a_directory_without_the_sticky_bit_is_fine_whoever_owns_the_files(self):
        self.sticky_state(sticky=False)
        self.assertEqual(levels(self.results()), [OK, OK])

    def test_root_and_systems_without_uids_replace_files_in_sticky_directories(self):
        self.sticky_state()
        self.assertEqual(levels(self.results(euid=0, account="root:root")), [OK, OK, INFO])
        with mock.patch("chime.check._effective_uid", return_value=None):
            self.assertEqual(levels(check_writable(self.config)), [OK, OK])

    def test_a_sticky_directory_the_user_cannot_write_is_reported_once_by_the_directory_row(self):
        self.sticky_state()
        with self.unwritable(self.path("cache")):
            results = self.results()
        self.assertEqual(levels(results), [NG, OK])
        self.assertEqual([result.title for result in results], ["cache/", "cache/tts/"])

    def test_the_directory_owner_is_the_real_one_behind_a_symbolic_link(self):
        """``cache`` が root 所有のリンクで、行き先が自分のフォルダなら、置き換えられる。"""
        real = self.path("real_cache")
        os.rename(self.path("cache"), real)
        os.symlink(real, self.path("cache"))
        os.chmod(real, 0o1777)
        self.write_json(self.path("cache", "state.json"), {})
        self.owners[self.path("cache")] = 0  # リンクそのものは root 所有
        self.owners[self.path("cache", "state.json")] = 0
        self.owners[os.path.realpath(real)] = PI_UID
        self.assertEqual(levels(self.results()), [OK, OK])
        self.owners[os.path.realpath(real)] = 0
        self.assertEqual(only(self.results(), "cache/state.json").level, NG)

    def test_a_sticky_state_file_without_an_account_gets_the_generic_fix(self):
        self.sticky_state()
        result = only(self.results(account=""), "cache/state.json")
        self.assertEqual(result.level, NG)
        self.assertTrue(result.fix.startswith(check._GENERIC_FIX))

    def test_a_sticky_directory_that_is_not_there_is_left_alone(self):
        shutil.rmtree(self.path("cache"))
        self.assertEqual(levels(self.results()), [OK, OK])

    def test_the_sticky_check_writes_nothing(self):
        self.sticky_state()
        before = sorted(os.listdir(self.path("cache")))
        stat_before = os.stat(self.path("cache", "state.json")).st_mtime_ns
        self.results()
        self.assertEqual(sorted(os.listdir(self.path("cache"))), before)
        self.assertEqual(os.stat(self.path("cache", "state.json")).st_mtime_ns, stat_before)

    def test_a_host_that_happens_to_have_the_missing_directories_does_not_change_the_answers(self):
        """``/nonexistent`` や ``/etc/chime`` が、この機械に実在しても（以前の実行の残りなど）同じ結果になる。"""
        real_exists, real_isdir = os.path.exists, os.path.isdir
        present = ("/nonexistent", "/var/lib/chime", "/etc/chime")

        def exists(path):
            return str(path).startswith(present) or real_exists(path)

        def isdir(path):
            return str(path) in present or real_isdir(path)

        with mock.patch("os.path.exists", exists), mock.patch("os.path.isdir", isdir):
            self.test_a_state_file_under_a_missing_top_level_directory_makes_only_that_directory()
            self.test_a_missing_target_never_gets_a_recursive_chown_whichever_setting_points_at_it()
            self.test_a_cache_dir_under_var_lib_does_not_take_over_var_lib()
            self.test_a_history_file_under_etc_does_not_take_over_etc()

    def test_root_owned_files_under_the_tts_cache_are_not_examined(self):
        wav = self.path("cache", "tts", "abc.wav")
        make_wav(wav)
        self.owners[wav] = 0
        looked_at = []
        real = check._uid_of

        def spying(path):
            looked_at.append(path)
            return real(path)

        with mock.patch("chime.check._uid_of", spying):
            results = self.results()
        self.assertEqual(levels(results), [OK, OK])
        self.assertFalse([p for p in looked_at if p.startswith(self.path("cache", "tts") + os.sep)], looked_at)

    def test_a_root_owned_cache_directory_the_service_cannot_write_is_a_warning_with_chown(self):
        self.owners[self.path("cache", "tts")] = 0
        with self.unwritable(self.path("cache", "tts")):
            result = only(self.results(), "cache/tts/")
        self.assertEqual(result.level, WARNING)
        self.assertEqual(result.fix, "sudo chown -R pi:pi {0}".format(self.path("cache", "tts")))

    def test_a_state_file_that_is_a_directory_is_ng(self):
        os.mkdir(self.path("cache", "state.json"))
        result = only(self.results(), "cache/state.json")
        self.assertEqual(result.level, NG)
        self.assertIn("ファイルではなくフォルダです", result.detail)
        self.assertIn("再生済みの記録を保存できません", result.detail)
        self.assertIn("移すか消して", result.fix)
        code, output = self.run_check()
        self.assertEqual(code, 1, output)

    def test_a_history_file_that_is_a_directory_is_only_a_warning(self):
        os.mkdir(self.path("cache", "history.jsonl"))
        result = only(self.results(), "cache/history.jsonl")
        self.assertEqual(result.level, WARNING)
        self.assertIn("ファイルではなくフォルダです", result.detail)
        self.assertIn("放送は止まりません", result.detail)
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertIn("結果: 警告 1 件。", output)

    def test_a_history_file_that_cannot_be_appended_to_is_a_warning(self):
        """履歴は追記なので、ファイルそのものに書けないと残らない（置き換えでは済まない）。"""
        history = self.path("cache", "history.jsonl")
        open(history, "w").close()
        self.owners[history] = 0
        with self.unwritable(history):
            result = only(self.results(), "cache/history.jsonl")
        self.assertEqual(result.level, WARNING)
        self.assertIn("追記できません", result.detail)
        self.assertIn("放送は止まりません", result.detail)
        self.assertEqual(result.fix, "sudo chown pi:pi {0}".format(history))

    def test_a_history_file_the_user_owns_but_cannot_write_is_a_mode_problem(self):
        history = self.path("cache", "history.jsonl")
        open(history, "w").close()
        with self.unwritable(history):
            result = only(self.results(), "cache/history.jsonl")
        self.assertEqual(result.fix, "chmod u+w {0}".format(history))

    def test_a_writable_history_file_adds_no_row(self):
        open(self.path("cache", "history.jsonl"), "w").close()
        self.assertEqual(levels(self.results()), [OK, OK])

    def test_a_history_failure_the_app_really_shrugs_off(self):
        """前提の確認：履歴が書けなくても、``History.append`` は例外を出さず ``False`` を返す。

        そのとき ``chime.history`` が出す WARNING は、端末に漏らさず ``assertLogs`` で拾い、
        どの履歴ファイルを書けなかったのかが載っていることまで確かめる
        （``tests/__init__.py`` がログを止めているので ``logs_enabled()`` で一時的に戻す）。
        """
        from chime.history import History
        history = self.path("cache", "history.jsonl")
        os.mkdir(history)
        with logs_enabled(), self.assertLogs("chime.history", level="WARNING") as captured:
            appended = History(history).append({"a": 1})
        self.assertFalse(appended)
        self.assertEqual(len(captured.records), 1, captured.output)
        message = captured.records[0].getMessage()
        self.assertIn("放送の履歴を書けません", message)
        self.assertIn(history, message)

    def test_a_missing_state_file_is_not_reported(self):
        self.assertNotIn("cache/state.json", [r.title for r in self.results()])

    def test_nothing_is_said_about_owners_when_running_as_root(self):
        result = only(self.results(euid=0), "所有者")
        self.assertEqual(result.level, INFO)
        self.assertIn("root で実行している", result.detail)

    def test_running_as_root_skips_the_permission_advice(self):
        """root には何でも書けるので、書けない場所の判定は意味がない（情報だけ出す）。"""
        self.assertEqual(levels(self.results(euid=0)), [OK, OK, INFO])

    def test_a_system_without_uids_skips_the_owner_check(self):
        """Windows など uid を持たない OS では、所有者の確認を省く。"""
        with mock.patch("chime.check._effective_uid", return_value=None):
            titles = [result.title for result in check_writable(self.config)]
        self.assertEqual(titles, ["cache/", "cache/tts/"])

    def test_without_an_account_the_fix_is_generic(self):
        with mock.patch("chime.check._can_write", return_value=False), \
                mock.patch("chime.check._effective_uid", return_value=None):
            result = only(check_writable(self.config), "cache/")
        self.assertNotIn("chown", result.fix)
        self.assertIn("書き込める", result.fix)

    def test_without_an_account_a_missing_directory_still_only_names_that_directory(self):
        shutil.rmtree(self.path("cache"))
        with mock.patch("chime.check._can_write", return_value=False), \
                mock.patch("chime.check._effective_uid", return_value=None):
            result = only(check_writable(self.config), "cache/")
        self.assertIn("sudo mkdir -p {0}".format(self.path("cache")), result.fix)
        self.assertNotIn("chown", result.fix)

    def test_the_default_account_name_is_looked_up_from_the_uid(self):
        results = check_writable(self.config, euid=PI_UID)
        self.assertEqual(levels(results), [OK, OK])
        self.assertTrue(check._account_name(0).startswith("root:"))

    def test_an_unknown_uid_still_gives_a_usable_account(self):
        self.assertEqual(check._account_name(654321), "654321:654321")

    def test_a_shared_directory_is_checked_once(self):
        titles = [result.title for result in self.results() if result.title.endswith("/")]
        self.assertEqual(titles, ["cache/", "cache/tts/"])


class PermissionProbeTest(unittest.TestCase):
    """``os.access`` を使う小さな判定（書き込みはしない）。実物の関数を試すので、所有者の差し替えはしない。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def test_a_plain_writable_file_is_writable(self):
        path = self.path("w.json")
        open(path, "w").close()
        os.chmod(path, 0o644)
        self.assertTrue(check._can_write(path))

    def test_a_directory_is_writable_only_with_write_and_search_permission(self):
        """ディレクトリは、中にファイルを作れる（書き込みと検索の権限）ときだけ書ける。"""
        if os.geteuid() == 0:
            self.skipTest("root には権限が効かない")
        directory = self.path("noexec")
        os.mkdir(directory)
        os.chmod(directory, 0o666)   # 書き込みはできても、検索できない
        self.addCleanup(os.chmod, directory, 0o755)
        self.assertFalse(check._can_write(directory))
        os.chmod(directory, 0o755)
        self.assertTrue(check._can_write(directory))

    def test_the_directory_probe_asks_for_search_permission_too(self):
        directory = self.root
        with mock.patch("chime.check.os.access", return_value=True) as access:
            check._can_write(directory)
        access.assert_called_once_with(directory, os.W_OK | os.X_OK)
        plain = self.path("plain")
        open(plain, "w").close()
        with mock.patch("chime.check.os.access", return_value=True) as access:
            check._can_write(plain)
        access.assert_called_once_with(plain, os.W_OK)

    def test_the_effective_uid_is_the_real_one(self):
        self.assertEqual(check._effective_uid(), os.geteuid())

    def test_the_owner_is_read_without_following_links(self):
        link = self.path("link")
        os.symlink(self.root, link)
        with mock.patch("chime.check.os.lstat") as lstat:
            lstat.return_value.st_uid = 7
            self.assertEqual(check._uid_of(link), 7)
        lstat.assert_called_once_with(link)
        self.assertIsNone(check._uid_of(self.path("no", "such")))


class ExitCodeTest(InstallCase):
    def test_any_ng_makes_the_exit_code_1(self):
        os.remove(self.path("assets", "announce.wav"))
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("結果: NG 1 件。", output)
        self.assertIn("直し方: git checkout -- assets/announce.wav", output)

    def test_ng_and_warnings_are_counted_separately(self):
        os.remove(self.path("assets", "announce.wav"))
        os.remove(self.path("assets", "generated", "time_signal.wav"))
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("結果: NG 1 件・警告 1 件。", output)

    def test_a_config_error_is_an_ng(self):
        config = make_config(self.root, {"timezone": "Asia/Tokio"})
        code, output = self.run_check(config)
        self.assertEqual(code, 1)
        self.assertIn("timezone", output)

    def test_every_section_but_settings_gets_the_config_the_service_would_run_with(self):
        """設定の誤りは、サービスでは既定値に戻して動く。ほかの節も、その設定で調べる。"""
        raw = make_config(self.root, {"timezone": "Asia/Tokio"})
        effective = make_config(self.root)
        with mock.patch("chime.check.configcheck.sanitized", return_value=(effective, [])), \
                mock.patch("chime.check.check_settings", return_value=[]) as settings, \
                mock.patch("chime.check.check_voices", return_value=[]) as voices, \
                mock.patch("chime.check.check_sounds", return_value=[]) as sounds, \
                mock.patch("chime.check.check_writable", return_value=[]) as writable:
            check.collect(raw, euid=PI_UID, account="pi:pi")
        settings.assert_called_once_with(raw)
        voices.assert_called_once_with(effective)
        sounds.assert_called_once_with(effective, PI_UID, "pi:pi")
        writable.assert_called_once_with(effective, PI_UID, "pi:pi")

    def test_exactly_five_infos_need_no_overflow_row(self):
        infos = [configcheck.Finding(configcheck.INFO, "k{0}".format(number), "m")
                 for number in range(check.INFO_LIMIT)]
        with mock.patch("chime.check.configcheck.check_config", return_value=infos):
            results = check_settings(self.config)
        self.assertNotIn("（ほか）", [result.title for result in results])
        self.assertEqual(len([r for r in results if r.level == INFO]), check.INFO_LIMIT)


class NoWriteTest(InstallCase):
    """点検は何も書かない（ファイルの内容・時刻・フォルダの一覧が変わらない）。"""

    def snapshot(self):
        state = {}
        for directory, names, files in os.walk(self.root):
            for name in names + files:
                path = os.path.join(directory, name)
                stat = os.lstat(path)
                state[os.path.relpath(path, self.root)] = (stat.st_mtime_ns, stat.st_size, stat.st_mode)
        return state

    def assert_unchanged_by_check(self, config=None):
        before = self.snapshot()
        listing_before = sorted(before)
        self.run_check(config)
        after = self.snapshot()
        self.assertEqual(sorted(after), listing_before)
        self.assertEqual(after, before)

    def test_a_healthy_install_is_left_untouched(self):
        self.write_json(self.path("cache", "state.json"), {"last_fired": {}})
        self.assert_unchanged_by_check()

    def test_nothing_is_created_for_things_that_do_not_exist_yet(self):
        shutil.rmtree(self.path("cache"))
        os.remove(self.path("assets", "generated", "time_signal.wav"))
        os.rmdir(self.path("assets", "generated"))
        self.assert_unchanged_by_check()
        self.assertFalse(os.path.exists(self.path("cache")))
        self.assertFalse(os.path.exists(self.path("assets", "generated")))

    def test_a_broken_install_is_left_untouched(self):
        os.remove(self.path("assets", "announce.wav"))
        shutil.rmtree(self.path("assets", "voice"))
        self.assert_unchanged_by_check()

    def test_files_that_are_present_but_broken_are_left_untouched(self):
        """空・途中で切れた WAV・短い MP3・フォルダになった状態ファイル・無いひとことでも、何も直さない。"""
        open(self.path("assets", "voice", prerecorded_filename(
            collect_phrases(self.config, include_quotes=True)[0])), "wb").close()
        make_wav(self.path("assets", "announce.wav"), seconds=1.0)
        with open(self.path("assets", "announce.wav"), "r+b") as handle:
            handle.truncate(100)
        with open(self.path("assets", "hotaru.mp3"), "wb") as handle:
            handle.write(b"ID3" + b"\x00" * 997)
        os.remove(self.path("assets", "quotes.json"))
        os.mkdir(self.path("cache", "state.json"))
        os.mkdir(self.path("cache", "history.jsonl"))
        self.assert_unchanged_by_check()

    def test_a_time_signal_that_cannot_be_made_is_not_made_by_the_check(self):
        os.remove(self.path("assets", "generated", "time_signal.wav"))
        self.assert_unchanged_by_check()
        self.assertEqual(os.listdir(self.path("assets", "generated")), [])

    def test_the_shipped_voice_folder_is_not_modified(self):
        voice = os.path.join(ASSETS_DIR, "voice")
        before = sorted((name, os.stat(os.path.join(voice, name)).st_mtime_ns)
                        for name in os.listdir(voice))
        config = make_config(os.path.dirname(ASSETS_DIR), {"tts": {"cache_dir": self.path("cache", "tts")},
                                                           "state": {"file": self.path("cache", "state.json"),
                                                                     "history_file": self.path("cache", "h.jsonl")}})
        self.run_check(config)
        after = sorted((name, os.stat(os.path.join(voice, name)).st_mtime_ns)
                       for name in os.listdir(voice))
        self.assertEqual(after, before)

    def test_it_never_opens_a_file_for_writing(self):
        real_open = open
        modes = []

        def watching_open(file, mode="r", *args, **kwargs):
            modes.append((str(file), mode))
            return real_open(file, mode, *args, **kwargs)

        with mock.patch("builtins.open", watching_open):
            self.run_check()
        writes = [entry for entry in modes if any(flag in entry[1] for flag in "wax+")]
        self.assertEqual(writes, [])
        self.assertTrue(modes)  # 読みは行っている（差し替えが効いている）


class RenderingTest(unittest.TestCase):
    def test_display_width_counts_fullwidth_characters_as_two(self):
        self.assertEqual(display_width("abc"), 3)
        self.assertEqual(display_width("設定"), 4)
        self.assertEqual(display_width("OK 設定"), 7)

    def test_display_width_counts_fullwidth_latin_as_two(self):
        """全角の英数字（unicodedata では ``F``）も 2 桁。半角カナ（``H``）は 1 桁。"""
        self.assertEqual(display_width("Ａ"), 2)
        self.assertEqual(display_width("ＡＢ１"), 6)
        self.assertEqual(display_width("ｱ"), 1)

    def test_pad_aligns_by_display_width(self):
        self.assertEqual(display_width(pad("設定", 8)), 8)
        self.assertEqual(display_width(pad("cache/", 8)), 8)
        self.assertEqual(pad("long title", 3), "long title")

    def test_marks_have_the_same_width(self):
        widths = {display_width(mark) for mark in check._MARKS.values()}
        self.assertEqual(widths, {4})

    def test_a_section_aligns_the_detail_column_and_indents_the_extras(self):
        lines = render_section("見出し", [
            Result(OK, "短い", "詳細 1"),
            Result(NG, "長めの項目", "詳細 2", "直す", ("一覧 1", "一覧 2")),
        ])
        self.assertEqual(lines[0], "== 見出し ==")
        first, second = lines[1], lines[2]
        self.assertEqual(display_width(first[:first.index("詳細 1")]),
                         display_width(second[:second.index("詳細 2")]))
        indent = " " * display_width(second[:second.index("詳細 2")])
        self.assertEqual(lines[3:], [indent + "一覧 1", indent + "一覧 2", indent + "直し方: 直す"])

    def test_a_result_without_detail_has_no_trailing_spaces(self):
        [_, line] = render_section("x", [Result(OK, "項目")])
        self.assertEqual(line, line.rstrip())

    def test_summarize(self):
        self.assertEqual(summarize([Result(OK, "a"), Result(INFO, "b")]), "結果: すべて OK です。")
        self.assertEqual(summarize([Result(NG, "a"), Result(NG, "b")]), "結果: NG 2 件。")
        self.assertEqual(summarize([Result(WARNING, "a")]), "結果: 警告 1 件。")
        self.assertEqual(summarize([Result(NG, "a"), Result(WARNING, "b")]), "結果: NG 1 件・警告 1 件。")

    def test_shown_path_is_relative_inside_the_repository_and_absolute_outside(self):
        config = make_config("/opt/chime")
        self.assertEqual(shown_path(config, "/opt/chime/assets/x.wav"), "assets/x.wav")
        self.assertEqual(shown_path(config, "/elsewhere/x.wav"), "/elsewhere/x.wav")
        self.assertEqual(shown_path(config, ""), "")

    def test_shown_path_names_the_repository_itself_in_full_not_as_a_dot(self):
        config = make_config("/opt/chime")
        self.assertEqual(shown_path(config, "/opt/chime"), "/opt/chime")

    def test_the_output_is_japanese_and_has_no_color_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            run_check(make_config(tmp), out=out, euid=0)
        self.assertNotIn("\033", out.getvalue())
        self.assertIn("== 書き込み ==", out.getvalue())


if __name__ == "__main__":
    unittest.main()
