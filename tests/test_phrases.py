"""読み上げうる全文言の列挙（``chime/phrases.py``）のテスト。

Pi には実行時の音声合成が無く、声は文言の**完全一致**で作り置きから引く。
列挙が 1 文字でもずれると、その文だけ Pi で無音になる。ここでは既定設定・
現地設定それぞれの列挙結果を件数とハッシュで固定し（特性テスト）、整理で
結果が変わらないことを確かめる。あわせて、``--config`` の文言と既定の文言の
和集合、``--prune`` で残す文言、列挙を分けた各関数、再生系を import しない
こと（層の分け方）を調べる。
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import KYOTO_ONLY, REPO_ROOT, VOICE_DIR, load_manifest, write_quotes

from chime import phrases, timesignal, weather
from chime.config import DEFAULT_CONFIG, Config, deep_merge
from chime.quotes import load_quotes
from chime.tts import TTSService, normalize_phrase

#: 現地の ``config.json`` で文言を足した、という想定の設定（上書き分）。
#: 地点を 2 つにし、閉館のひとこと・時報の範囲と言い回しを変える
#: （ひとことのファイルは、実行時に一時ファイルへ向ける）。
LOCAL_OVERRIDE = {
    "weather": {"open_meteo": {"locations": [
        {"label": "大津", "latitude": 35.0045, "longitude": 135.8686},
        {"label": "京都", "latitude": 35.0116, "longitude": 135.7681},
    ]}},
    "closing": {"extra_text": "本日もご利用ありがとうございました。"},
    "schedule": {"hourly": {"start_hour": 8, "end_hour": 18}},
    "time_signal": {"announce_template": "{period}{hour_reading}です。"},
}

#: 重複・空文字列・``by_hour`` のキーの並び（16 が 10 より先）を含むひとこと。
LOCAL_QUOTES = {
    "general": ["ひとつめなのだ。", "ふたつめなのだ。", "", "ひとつめなのだ。"],
    "by_hour": {"16": ["夕方なのだ。", "ふたつめなのだ。"], "10": ["朝なのだ。"], "12": []},
}

UNUSED = "どこにも使われていない文言なのだ。"


def sha1_of(items):
    """文言の並び（順序込み）の ``(件数, sha1)``。"""
    return len(items), hashlib.sha1("\n".join(items).encode("utf-8")).hexdigest()


def pairs_sha1_of(pairs):
    """``(文言, ファイル名)`` の並びの ``(件数, sha1)``。"""
    joined = "\n".join("{0}\t{1}".format(*pair) for pair in pairs)
    return len(pairs), hashlib.sha1(joined.encode("utf-8")).hexdigest()


class PinnedPhraseSetsTest(unittest.TestCase):
    """列挙結果の固定（特性テスト）。値は整理の前のコードで記録したもの。

    声は文言の完全一致で引くため、ここが動いたら「Pi で無音になる文言が出た」
    ことを疑う。意図して文言を変えたときだけ、値を更新して作り置きを作り直す。
    """

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        quotes_path = write_quotes(tmp.name, LOCAL_QUOTES)

        # ローカルの config.json を読まないよう、既定値から直接作る
        cls.default = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)
        override = deep_merge(LOCAL_OVERRIDE, {"quotes": {"file": quotes_path}})
        cls.local = Config(deep_merge(DEFAULT_CONFIG, override), base_dir=REPO_ROOT)

        # 現地設定の作り置きの目録（連番のファイル名）に、使われていない 1 件を足したもの
        manifest = {phrase: "{0:03d}.wav".format(number)
                    for number, phrase in enumerate(phrases.phrases_in_use(cls.local))}
        manifest[UNUSED] = "unused.wav"
        cls.manifest = manifest

    # -- 既定設定 ------------------------------------------------------------
    def test_default_collect_phrases(self):
        self.assertEqual(
            sha1_of(phrases.collect_phrases(self.default, include_quotes=False)),
            (81, "74d90530b4ab08e22fcde9a6ac313212ba493f82"))
        self.assertEqual(
            sha1_of(phrases.collect_phrases(self.default, include_quotes=True)),
            (138, "6dc7481f153451cff805a8cd3514c27c6ba0b4bb"))

    def test_default_phrases_to_generate(self):
        self.assertEqual(
            sha1_of(phrases.phrases_to_generate(self.default, include_quotes=False)),
            (81, "74d90530b4ab08e22fcde9a6ac313212ba493f82"))
        self.assertEqual(
            sha1_of(phrases.phrases_to_generate(self.default, include_quotes=True)),
            (138, "6dc7481f153451cff805a8cd3514c27c6ba0b4bb"))

    def test_default_phrases_in_use_and_to_keep(self):
        expected = (138, "6dc7481f153451cff805a8cd3514c27c6ba0b4bb")
        self.assertEqual(sha1_of(phrases.phrases_in_use(self.default)), expected)
        self.assertEqual(sha1_of(phrases.phrases_to_keep(self.default)), expected)

    def test_default_find_stale_entries(self):
        # 現地設定にだけある文言（43 件）と、使われていない 1 件の計 44 件が、
        # 既定設定の側から見て余りになる（目録の並びのまま）
        expected = (44, "ad5c0d3b4f20fb4c8aa31eaca771614accb97c27")
        self.assertEqual(
            pairs_sha1_of(phrases.find_stale_entries(
                self.manifest, phrases.phrases_in_use(self.default))),
            expected)
        self.assertEqual(
            pairs_sha1_of(phrases.find_stale_entries(
                self.manifest, phrases.phrases_to_keep(self.default))),
            expected)

    # -- 現地設定（地点・閉館のひとこと・時報の範囲と言い回し・ひとこと） ----
    def test_local_collect_phrases(self):
        self.assertEqual(
            sha1_of(phrases.collect_phrases(self.local, include_quotes=False)),
            (114, "eb9980ddfb32894683c298c8f5ad4ea22de0b325"))
        self.assertEqual(
            sha1_of(phrases.collect_phrases(self.local, include_quotes=True)),
            (118, "f97a9a2fbe5c3f66184fb4c023470f233be311be"))

    def test_local_phrases_to_generate(self):
        # 現地設定の文言が先、既定設定にだけある文言があとに続く
        self.assertEqual(
            sha1_of(phrases.phrases_to_generate(self.local, include_quotes=False)),
            (120, "764c38ce2ff104bee288a5d224925bb036af7350"))
        self.assertEqual(
            sha1_of(phrases.phrases_to_generate(self.local, include_quotes=True)),
            (181, "5926a7efa66ff4b34538b04de0397eb48cc09c0a"))

    def test_local_phrases_in_use_and_to_keep(self):
        self.assertEqual(sha1_of(phrases.phrases_in_use(self.local)),
                         (118, "f97a9a2fbe5c3f66184fb4c023470f233be311be"))
        self.assertEqual(sha1_of(phrases.phrases_to_keep(self.local)),
                         (181, "5926a7efa66ff4b34538b04de0397eb48cc09c0a"))

    def test_local_find_stale_entries(self):
        stale = phrases.find_stale_entries(self.manifest, phrases.phrases_to_keep(self.local))
        self.assertEqual(stale, [(UNUSED, "unused.wav")])
        self.assertEqual(pairs_sha1_of(stale),
                         (1, "b3ef0638046ca2b53c3192c063002fede4d6ebe1"))
        # 何も残さないなら、目録の全件が余りになる
        self.assertEqual(pairs_sha1_of(phrases.find_stale_entries(self.manifest, [])),
                         (119, "6423f15851cc0f1e740377949987770abc230901"))


class PruneTest(unittest.TestCase):
    def setUp(self):
        # ローカルの config.json を読まないよう、既定値から直接作る
        self.config = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)
        self.manifest = load_manifest()

    def test_every_default_phrase_is_in_the_shipped_manifest(self):
        """既定設定の全文言（ひとことを含む）が、同梱の manifest にあること。

        現地で足した文言（地点など）の作り置きを同梱してもよいので、
        manifest の側に余りが無いことまでは求めない。
        """
        self.assertTrue(self.manifest)
        missing = [phrase for phrase in phrases.phrases_in_use(self.config)
                   if phrase not in self.manifest]
        self.assertEqual(missing, [])

    def test_quotes_are_in_use_even_when_not_regenerated(self):
        quotes = load_quotes(self.config.path("quotes.file"))["general"]
        self.assertTrue(quotes)
        without_quotes = phrases.collect_phrases(self.config, include_quotes=False)
        in_use = phrases.phrases_in_use(self.config)
        for quote in quotes:
            self.assertNotIn(quote, without_quotes)
            self.assertIn(quote, in_use)

    def test_only_an_unused_phrase_is_stale(self):
        manifest = dict(self.manifest)
        manifest[UNUSED] = "unused.wav"
        stale = phrases.find_stale_entries(manifest, phrases.phrases_in_use(self.config))
        self.assertEqual(stale, [(UNUSED, "unused.wav")])


class PhraseUnionTest(unittest.TestCase):
    """``--config`` の設定の文言は、既定設定の文言に足される（和集合）。"""

    def setUp(self):
        self.default = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)
        self.local = Config(deep_merge(DEFAULT_CONFIG, KYOTO_ONLY), base_dir=REPO_ROOT)
        self.kyoto = [phrase for phrase in weather.prerecord_phrases(self.local.section("weather"))
                      if "京都" in phrase]
        self.otsu = [phrase for phrase in weather.prerecord_phrases(self.default.section("weather"))
                     if "大津" in phrase]

    def test_the_fixture_replaces_the_default_location(self):
        # 前提の確認: 配列が丸ごと置き換わり、指定設定だけでは大津が外れる
        self.assertTrue(self.kyoto)
        self.assertTrue(self.otsu)
        local_only = phrases.collect_phrases(self.local, include_quotes=False)
        for phrase in self.otsu:
            self.assertNotIn(phrase, local_only)

    def test_phrases_to_generate_adds_the_defaults_to_the_config(self):
        generated = phrases.phrases_to_generate(self.local, include_quotes=False)
        local_only = phrases.collect_phrases(self.local, include_quotes=False)
        default_only = phrases.collect_phrases(self.default, include_quotes=False)
        self.assertEqual(set(generated), set(local_only) | set(default_only))
        for phrase in self.kyoto + self.otsu:
            self.assertIn(phrase, generated)

    def test_phrases_to_generate_has_no_duplicates_and_a_stable_order(self):
        generated = phrases.phrases_to_generate(self.local, include_quotes=True)
        self.assertEqual(len(generated), len(set(generated)))
        # 指定設定の文言が先頭から順に並び、既定設定にだけある文言があとに続く
        local_only = phrases.collect_phrases(self.local, include_quotes=True)
        self.assertEqual(generated[:len(local_only)], local_only)
        self.assertEqual(
            generated, phrases.phrases_to_generate(self.local, include_quotes=True))

    def test_the_defaults_alone_are_unchanged(self):
        self.assertEqual(
            phrases.phrases_to_generate(self.default, include_quotes=True),
            phrases.collect_phrases(self.default, include_quotes=True))

    def test_phrases_to_keep_covers_the_config_and_the_defaults_including_quotes(self):
        keep = phrases.phrases_to_keep(self.local)
        for phrase in self.kyoto + self.otsu:
            self.assertIn(phrase, keep)
        for quote in load_quotes(self.default.path("quotes.file"))["general"]:
            self.assertIn(quote, keep)
        self.assertEqual(len(keep), len(set(keep)))

    def test_phrases_to_keep_is_phrases_to_generate_with_quotes(self):
        # ``--prune`` で残す文言は、ひとことを含めて生成する文言と同じ定義
        for config in (self.default, self.local):
            self.assertEqual(phrases.phrases_to_keep(config),
                             phrases.phrases_to_generate(config, include_quotes=True))


class AnnouncementPhrasesTest(unittest.TestCase):
    def config(self, hourly=None, time_signal=None):
        override = {"schedule": {"hourly": hourly or {}}, "time_signal": time_signal or {}}
        return Config(deep_merge(DEFAULT_CONFIG, override), base_dir=REPO_ROOT)

    def test_one_phrase_per_hour_from_start_to_end_inclusive(self):
        self.assertEqual(list(phrases.announcement_phrases(self.config())), [
            "午前10時をお知らせしたのだ。",
            "午前11時をお知らせしたのだ。",
            "正午をお知らせしたのだ。",
            "午後1時をお知らせしたのだ。",
            "午後2時をお知らせしたのだ。",
            "午後3時をお知らせしたのだ。",
            "午後よじをお知らせしたのだ。",
        ])

    def test_follows_the_configured_hours_and_template(self):
        config = self.config(hourly={"start_hour": 8, "end_hour": 10},
                             time_signal={"announce_template": "{period}{hour}時なのだ。"})
        self.assertEqual(list(phrases.announcement_phrases(config)),
                         ["午前8時なのだ。", "午前9時なのだ。", "午前10時なのだ。"])

    def test_is_a_generator_so_a_broken_template_fails_only_when_reached(self):
        # 12 時は正午用のテンプレートなので、通常のテンプレートが壊れていても
        # 13 時に進むまでは例外にならない（1 件ずつ処理する呼び出し側のため）
        config = self.config(hourly={"start_hour": 12, "end_hour": 13},
                             time_signal={"announce_template": "{period}{no_such_part}"})
        iterator = phrases.announcement_phrases(config)
        self.assertTrue(inspect.isgenerator(iterator))
        self.assertEqual(next(iterator), "正午をお知らせしたのだ。")
        with self.assertRaises(KeyError):
            next(iterator)

    def test_is_neither_deduplicated_nor_filtered_by_skip_hours(self):
        # 休みにしている時刻の文言も作り置きしておく。重複を除くのは collect_phrases
        config = self.config(hourly={"skip_hours": [12, 13]},
                             time_signal={"announce_template": "ただいまなのだ。",
                                          "use_noon_template": False})
        self.assertEqual(list(phrases.announcement_phrases(config)), ["ただいまなのだ。"] * 7)
        self.assertEqual(
            phrases.collect_phrases(config, include_quotes=False).count("ただいまなのだ。"), 1)

    def test_agrees_with_timesignal_announce_text(self):
        config = self.config(hourly={"start_hour": 0, "end_hour": 23})
        settings = config.section("time_signal")
        self.assertEqual(list(phrases.announcement_phrases(config)),
                         [timesignal.announce_text(hour, settings) for hour in range(24)])


class ClosingPhrasesTest(unittest.TestCase):
    def test_closing_extra_text_reads_the_configured_text(self):
        config = Config({"closing": {"extra_text": "本日もありがとうございました。"}})
        self.assertEqual(phrases.closing_extra_text(config), "本日もありがとうございました。")

    def test_closing_extra_text_is_empty_when_unset(self):
        # None・空文字列・キーなしは、どれも空文字列（None を "None" と読まない）
        self.assertEqual(phrases.closing_extra_text(Config({"closing": {"extra_text": None}})), "")
        self.assertEqual(phrases.closing_extra_text(Config({"closing": {"extra_text": ""}})), "")
        self.assertEqual(phrases.closing_extra_text(Config({"closing": {}})), "")
        self.assertEqual(phrases.closing_extra_text(Config({})), "")

    def test_closing_phrases_has_the_text_only_when_it_is_set(self):
        self.assertEqual(
            phrases.closing_phrases(Config({"closing": {"extra_text": "ありがとうなのだ。"}})),
            ["ありがとうなのだ。"])
        self.assertEqual(phrases.closing_phrases(Config({"closing": {"extra_text": ""}})), [])
        self.assertEqual(phrases.closing_phrases(Config({})), [])

    def test_the_default_config_has_no_closing_phrase(self):
        self.assertEqual(phrases.closing_phrases(Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)), [])


class QuotePhrasesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config({"quotes": {"file": write_quotes(tmp.name, LOCAL_QUOTES)}},
                             base_dir=tmp.name)

    def test_general_comes_first_then_by_hour_in_file_order(self):
        # そのままの並び（重複・空文字列も除かない）。除くのは collect_phrases
        self.assertEqual(phrases.quote_phrases(self.config), [
            "ひとつめなのだ。", "ふたつめなのだ。", "", "ひとつめなのだ。",
            "夕方なのだ。", "ふたつめなのだ。",
            "朝なのだ。",
        ])

    def test_collect_phrases_deduplicates_the_quotes(self):
        # 空文字列と重複を除き、最初に現れた順に残る
        announcements = set(phrases.announcement_phrases(self.config))
        collected = phrases.collect_phrases(self.config, include_quotes=True)
        self.assertEqual([phrase for phrase in collected if phrase not in announcements],
                         ["ひとつめなのだ。", "ふたつめなのだ。", "夕方なのだ。", "朝なのだ。"])

    def test_the_quotes_file_is_read_only_when_quotes_are_included(self):
        with mock.patch.object(phrases, "load_quotes",
                               return_value={"general": ["x"], "by_hour": {}}) as load:
            self.assertNotIn("x", phrases.collect_phrases(self.config, include_quotes=False))
            load.assert_not_called()
            self.assertIn("x", phrases.collect_phrases(self.config, include_quotes=True))
            load.assert_called_once_with(self.config.path("quotes.file"))

    def test_the_shipped_quotes_are_in_the_default_listing(self):
        config = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)
        shipped = phrases.quote_phrases(config)
        self.assertTrue(shipped)
        in_use = phrases.collect_phrases(config, include_quotes=True)
        self.assertTrue(set(shipped) <= set(in_use))


class WhitespaceNormalisationTest(unittest.TestCase):
    """列挙は、実行時の照合（``TTSService``）と同じく前後の空白を落とした文言を返す。

    実行時は読み上げる文言の前後の空白を落としてから作り置きを引く。列挙（＝作り置きの
    対象）が落とさないと、前後に空白のある ``extra_text`` やひとことは、manifest に
    空白つきのキーで載ってしまい、実行時には永久に引けない（その文だけ無音）。
    """

    RAW_QUOTES = {
        "general": ["  ひとつめなのだ。\n", "ふたつめなのだ。", "ひとつめなのだ。", "   ", ""],
        "by_hour": {"16": ["\t夕方なのだ。 ", "ふたつめなのだ。"], "10": ["\u3000朝なのだ。\u3000"]},
    }
    RAW_CLOSING = "  本日もご利用ありがとうございました。\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.quotes_path = write_quotes(tmp.name, self.RAW_QUOTES)
        override = {"quotes": {"file": self.quotes_path},
                    "closing": {"extra_text": self.RAW_CLOSING}}
        self.config = Config(deep_merge(DEFAULT_CONFIG, override), base_dir=REPO_ROOT)

    def tail(self, listing):
        """天気と時報の文言を除いた、閉館の文言とひとこと（列挙の順）。"""
        known = (set(phrases.announcement_phrases(self.config))
                 | set(weather.prerecord_phrases(self.config.section("weather"))))
        return [phrase for phrase in listing if phrase not in known]

    def test_collect_phrases_strips_drops_empty_and_keeps_order(self):
        self.assertEqual(
            self.tail(phrases.collect_phrases(self.config, include_quotes=True)),
            ["本日もご利用ありがとうございました。",
             "ひとつめなのだ。", "ふたつめなのだ。", "夕方なのだ。", "朝なのだ。"])

    def test_no_phrase_has_surrounding_whitespace_or_is_empty(self):
        for include_quotes in (False, True):
            listing = phrases.collect_phrases(self.config, include_quotes=include_quotes)
            self.assertEqual(len(listing), len(set(listing)))
            for phrase in listing:
                self.assertTrue(phrase)
                self.assertEqual(phrase, phrase.strip())

    def test_the_closing_text_is_listed_without_quotes_too(self):
        listing = phrases.collect_phrases(self.config, include_quotes=False)
        self.assertEqual(self.tail(listing), ["本日もご利用ありがとうございました。"])

    def test_the_other_listings_use_the_same_normalisation(self):
        for listing in (phrases.phrases_in_use(self.config),
                        phrases.phrases_to_keep(self.config),
                        phrases.phrases_to_generate(self.config, include_quotes=True)):
            self.assertIn("ひとつめなのだ。", listing)
            self.assertIn("本日もご利用ありがとうございました。", listing)
            self.assertEqual([phrase for phrase in listing if phrase != phrase.strip()], [])
            self.assertEqual(len(listing), len(set(listing)))

    def test_every_listed_phrase_is_what_the_runtime_looks_up(self):
        # 実行時は、読み上げる文言（空白つきのまま）を normalize_phrase で整えてから引く。
        listing = set(phrases.collect_phrases(self.config, include_quotes=True))
        spoken = [phrases.closing_extra_text(self.config)] + [
            text for text in phrases.quote_phrases(self.config) if text.strip()]
        for text in spoken:
            self.assertIn(normalize_phrase(text), listing, repr(text))

    def test_a_manifest_made_from_the_listing_is_found_by_the_service(self):
        with tempfile.TemporaryDirectory() as directory:
            voice = os.path.join(directory, "voice")
            os.makedirs(voice)
            manifest = {}
            for number, phrase in enumerate(
                    phrases.collect_phrases(self.config, include_quotes=True)):
                manifest[phrase] = "{0:03d}.wav".format(number)
                with open(os.path.join(voice, manifest[phrase]), "wb") as handle:
                    handle.write(b"RIFF")
            with open(os.path.join(voice, "manifest.json"), "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False)
            service = TTSService({"engines": ["prerecorded"]}, os.path.join(directory, "c"), voice)
            for raw in [self.RAW_CLOSING, "  ひとつめなのだ。\n", "\t夕方なのだ。 ",
                        "\u3000朝なのだ。\u3000"]:
                self.assertIsNotNone(service.prerecorded_lookup(raw), repr(raw))

    def test_an_announcement_template_with_whitespace_is_normalised_too(self):
        config = Config(deep_merge(DEFAULT_CONFIG, {
            "schedule": {"hourly": {"start_hour": 10, "end_hour": 11}},
            "time_signal": {"announce_template": " {period}{hour}時なのだ。 "}}),
            base_dir=REPO_ROOT)
        self.assertEqual(
            phrases.collect_phrases(config, include_quotes=False)[:2],
            ["午前10時なのだ。", "午前11時なのだ。"])

    def test_the_default_listing_is_unchanged(self):
        default = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)
        self.assertEqual(sha1_of(phrases.collect_phrases(default, include_quotes=False)),
                         (81, "74d90530b4ab08e22fcde9a6ac313212ba493f82"))
        self.assertEqual(sha1_of(phrases.collect_phrases(default, include_quotes=True)),
                         (138, "6dc7481f153451cff805a8cd3514c27c6ba0b4bb"))


class MissingTimeSignalSectionTest(unittest.TestCase):
    """``"time_signal": null`` でも、時報の文言は作り置きにある（Pi で無音にならない）。"""

    def config(self, time_signal):
        return Config(deep_merge(DEFAULT_CONFIG, {"time_signal": time_signal}),
                      base_dir=REPO_ROOT)

    def test_the_announcements_equal_the_default_ones_and_are_in_the_shipped_manifest(self):
        default = list(phrases.announcement_phrases(Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)))
        manifest = load_manifest()
        for time_signal in (None, {}, "壊れている"):
            with self.subTest(time_signal=time_signal):
                listing = list(phrases.announcement_phrases(self.config(time_signal)))
                self.assertEqual(listing, default)
                self.assertEqual([phrase for phrase in listing if phrase not in manifest], [])

    def test_the_whole_listing_is_the_default_one(self):
        default = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)
        self.assertEqual(
            phrases.collect_phrases(self.config(None), include_quotes=True),
            phrases.collect_phrases(default, include_quotes=True))


class CoverageTest(unittest.TestCase):
    """``coverage``: 列挙した文言のうち、作り置きの声がある・ない文言を数える。"""

    KINDS = ["announce", "closing", "quote", "weather"]

    def setUp(self):
        self.default = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)

    @staticmethod
    def everything(text):
        return "/voice/" + text + ".wav"

    @staticmethod
    def nothing(text):
        return None

    def test_the_totals_equal_the_number_of_listed_phrases(self):
        for include_quotes, expected in ((True, 138), (False, 81)):
            with self.subTest(include_quotes=include_quotes):
                result = phrases.coverage(self.default, self.everything, include_quotes)
                self.assertEqual(result.total, expected)
                self.assertEqual(
                    result.total,
                    len(phrases.collect_phrases(self.default, include_quotes)))
                self.assertEqual(sum(total for _, total in result.by_kind.values()), expected)

    def test_quotes_are_included_by_default(self):
        self.assertEqual(phrases.coverage(self.default, self.everything).total, 138)

    def test_everything_recorded_is_ok(self):
        result = phrases.coverage(self.default, self.everything)
        self.assertEqual(result.missing, [])
        self.assertTrue(result.ok)
        for kind, (available, total) in result.by_kind.items():
            self.assertEqual(available, total, kind)

    def test_nothing_recorded_lists_every_phrase_in_order(self):
        result = phrases.coverage(self.default, self.nothing)
        self.assertEqual(result.missing, phrases.collect_phrases(self.default, True))
        self.assertFalse(result.ok)
        for kind, (available, total) in result.by_kind.items():
            self.assertEqual(available, 0, kind)

    def test_the_kinds_are_the_four_in_listing_order(self):
        result = phrases.coverage(self.default, self.everything)
        self.assertEqual(list(result.by_kind), self.KINDS)

    def test_the_default_counts_per_kind(self):
        result = phrases.coverage(self.default, self.everything)
        quotes = len(load_quotes(self.default.path("quotes.file"))["general"])
        by_hour = sum(len(values) for values in
                      load_quotes(self.default.path("quotes.file"))["by_hour"].values())
        self.assertEqual(result.by_kind["announce"], (7, 7))
        self.assertEqual(result.by_kind["closing"], (0, 0))
        self.assertEqual(result.by_kind["quote"], (quotes + by_hour, quotes + by_hour))
        self.assertEqual(result.by_kind["weather"], (74, 74))

    def test_without_quotes_the_quote_kind_is_zero_but_still_present(self):
        result = phrases.coverage(self.default, self.everything, include_quotes=False)
        self.assertEqual(result.by_kind["quote"], (0, 0))
        self.assertEqual(list(result.by_kind), self.KINDS)

    def test_missing_phrases_are_counted_in_their_own_kind(self):
        announcements = list(phrases.announcement_phrases(self.default))
        weather_phrases = weather.prerecord_phrases(self.default.section("weather"))
        quote = phrases.quote_phrases(self.default)[0]
        lost = {announcements[0], weather_phrases[0], weather_phrases[1], quote}

        result = phrases.coverage(
            self.default, lambda text: None if text in lost else self.everything(text))
        self.assertFalse(result.ok)
        self.assertEqual(set(result.missing), lost)
        # 列挙の順（時報 → ひとこと → 天気）のまま
        self.assertEqual(result.missing, [text for text in phrases.collect_phrases(self.default, True)
                                          if text in lost])
        self.assertEqual(result.by_kind["announce"], (6, 7))
        self.assertEqual(result.by_kind["quote"][1] - result.by_kind["quote"][0], 1)
        self.assertEqual(result.by_kind["weather"], (72, 74))
        self.assertEqual(result.total, 138)

    def test_the_closing_text_is_its_own_kind(self):
        config = Config(deep_merge(DEFAULT_CONFIG, {
            "closing": {"extra_text": "本日もありがとうございました。"}}), base_dir=REPO_ROOT)
        closing = "本日もありがとうございました。"
        result = phrases.coverage(
            config, lambda text: None if text == closing else self.everything(text))
        self.assertEqual(result.by_kind["closing"], (0, 1))
        self.assertEqual(result.missing, ["本日もありがとうございました。"])

    def test_a_phrase_in_two_kinds_is_counted_once_in_the_first(self):
        # 時報の文言とひとことが同じ文言なら、列挙（重複なし）と同じく 1 件。時報の側に数える。
        with tempfile.TemporaryDirectory() as directory:
            shared = next(phrases.announcement_phrases(self.default))
            quotes = write_quotes(directory, {"general": [shared, "ひとことなのだ。"]})
            config = Config(deep_merge(DEFAULT_CONFIG, {"quotes": {"file": quotes}}),
                            base_dir=REPO_ROOT)
            result = phrases.coverage(config, self.nothing)
            listed = phrases.collect_phrases(config, True)
        self.assertEqual(result.by_kind["announce"], (0, 7))
        self.assertEqual(result.by_kind["quote"], (0, 1))
        self.assertEqual(result.total, len(listed))
        self.assertEqual(result.total, sum(total for _, total in result.by_kind.values()))

    def test_the_lookup_gets_each_listed_phrase_once_and_only_those(self):
        asked = []

        def lookup(text):
            asked.append(text)
            return self.everything(text)

        phrases.coverage(self.default, lookup)
        self.assertEqual(asked, phrases.collect_phrases(self.default, True))

    def test_the_quotes_file_is_read_only_when_quotes_are_included(self):
        with mock.patch.object(phrases, "load_quotes",
                               return_value={"general": ["x"], "by_hour": {}}) as load:
            phrases.coverage(self.default, self.everything, include_quotes=False)
            load.assert_not_called()
            phrases.coverage(self.default, self.everything, include_quotes=True)
            load.assert_called_once_with(self.default.path("quotes.file"))

    def test_the_result_is_immutable_and_comparable(self):
        result = phrases.Coverage(total=1, missing=["あ"], by_kind={"announce": (0, 1)})
        self.assertFalse(result.ok)
        self.assertEqual(result, phrases.Coverage(1, ["あ"], {"announce": (0, 1)}))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.total = 2
        self.assertTrue(phrases.Coverage(0, [], {}).ok)

    def test_with_the_shipped_voice_directory_every_default_phrase_is_covered(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TTSService({"engines": ["prerecorded"]}, directory, VOICE_DIR)
            result = phrases.coverage(self.default, service.prerecorded_lookup)
        self.assertEqual(result.missing, [])
        self.assertTrue(result.ok)
        self.assertEqual(result.total, 138)

    def test_with_an_empty_voice_directory_everything_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TTSService({"engines": ["prerecorded"]}, directory,
                                 os.path.join(directory, "voice"))
            result = phrases.coverage(self.default, service.prerecorded_lookup)
        self.assertEqual(result.missing, phrases.collect_phrases(self.default, True))

    def test_the_lookup_gets_phrases_normalised_like_the_runtime(self):
        # 前後に空白のある文言は、実行時と同じ整え方をした文言で引く
        # （空白つきのまま引くと、「ある」のに「無い」と出てしまう）。
        asked = []

        def lookup(text):
            asked.append(text)
            return "/x.wav" if text == "ひとことなのだ。" else None

        with tempfile.TemporaryDirectory() as directory:
            quotes = write_quotes(directory, {"general": ["  ひとことなのだ。 "]})
            config = Config(deep_merge(DEFAULT_CONFIG, {"quotes": {"file": quotes}}),
                            base_dir=REPO_ROOT)
            result = phrases.coverage(config, lookup)
        self.assertEqual(result.by_kind["quote"], (1, 1))
        self.assertIn("ひとことなのだ。", asked)
        self.assertEqual([text for text in asked if text != text.strip()], [])


class CoverageSizeGuardTest(unittest.TestCase):
    """``coverage`` は、設定の値でいくらでも増える文言を、列挙する前に断る。

    起動のたびに呼ばれるので、``weather.prerecord.temp_max`` に巨大な値があっても、
    長く固まったり、メモリを使い切ったりしない（Pi 3B は 1 GB）。
    """

    def setUp(self):
        self.default = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)

    @staticmethod
    def everything(text):
        return "/voice/" + text + ".wav"

    def config(self, **weather_override):
        """既定の設定に、``weather`` 節だけ上書きしたもの。"""
        return Config(deep_merge(DEFAULT_CONFIG, {"weather": weather_override}),
                      base_dir=REPO_ROOT)

    def refuses_before_listing(self, config, **kwargs):
        """``CoverageTooLarge`` を出し、文言の列挙も照合もしない。メッセージを返す。"""
        asked = []

        def lookup(text):
            asked.append(text)
            return self.everything(text)

        with mock.patch.object(weather, "prerecord_phrases",
                               side_effect=AssertionError("列挙してはいけない")), \
                mock.patch.object(phrases, "announcement_phrases",
                                  side_effect=AssertionError("列挙してはいけない")):
            with self.assertRaises(phrases.CoverageTooLarge) as caught:
                phrases.coverage(config, lookup, **kwargs)
        self.assertEqual(asked, [])
        return str(caught.exception)

    def test_the_limits_are_pinned(self):
        self.assertEqual(phrases.MAX_COVERAGE_PHRASES, 2000)
        self.assertEqual(phrases.MAX_TEMP_SPAN, 200)
        self.assertIs(inspect.signature(phrases.coverage).parameters["max_phrases"].default,
                      phrases.MAX_COVERAGE_PHRASES)

    def test_the_exception_is_a_value_error_with_a_japanese_message(self):
        self.assertTrue(issubclass(phrases.CoverageTooLarge, ValueError))
        message = self.refuses_before_listing(self.config(prerecord={"temp_max": 10 ** 9}))
        self.assertIn("気温", message)

    def test_a_normal_config_is_counted_as_before(self):
        result = phrases.coverage(self.default, self.everything)
        self.assertEqual((result.total, result.missing), (138, []))
        self.assertEqual(result, phrases.coverage(self.default, self.everything, max_phrases=None))

    def test_the_default_estimate_is_the_exact_listing_size(self):
        # 時報 7 + 天気 74（重複なし）。ひとことは見積もりに入れない（ファイルの中身で決まる）。
        self.assertEqual(phrases._estimate_work(self.default), 81)

    def test_a_huge_temperature_range_is_refused_before_anything_is_listed(self):
        for temp_max in (10 ** 9, 10 ** 400, float("inf")):
            with self.subTest(temp_max=temp_max):
                self.refuses_before_listing(self.config(prerecord={"temp_max": temp_max}))

    def test_a_huge_negative_minimum_is_refused_too(self):
        self.refuses_before_listing(self.config(prerecord={"temp_min": -10 ** 9}))
        self.refuses_before_listing(self.config(prerecord={"temp_min": float("-inf")}))

    def test_the_message_says_how_wide_the_range_is(self):
        message = self.refuses_before_listing(
            self.config(prerecord={"temp_min": 0, "temp_max": 999}))
        self.assertIn("1000 度分", message)
        self.assertIn("上限は 200 度分", message)

    def test_a_range_of_exactly_the_limit_is_still_counted(self):
        config = self.config(prerecord={"temp_min": 0, "temp_max": phrases.MAX_TEMP_SPAN - 1})
        result = phrases.coverage(config, self.everything)
        self.assertEqual(result.by_kind["weather"][1], 28 + 200)
        self.refuses_before_listing(
            self.config(prerecord={"temp_min": 0, "temp_max": phrases.MAX_TEMP_SPAN}))

    def test_the_range_does_not_matter_while_no_temperature_sentence_is_enabled(self):
        # 気温の文を読まない設定なら、気温の幅は列挙されない。幅が広くても断らない。
        config = self.config(prerecord={"temp_min": -10 ** 9, "temp_max": 10 ** 9},
                             sentence_temp="", sentence_temp_max="")
        result = phrases.coverage(config, self.everything)
        self.assertEqual(result.by_kind["weather"], (28, 28))

    def test_either_temperature_sentence_makes_the_range_count(self):
        for key, text in (("sentence_temp", "気温は{temp}度なのだ。"),
                          ("sentence_temp_max", "最高気温は{temp_max}度なのだ。")):
            with self.subTest(key=key):
                others = {"sentence_temp": "", "sentence_temp_max": ""}
                others[key] = text
                self.refuses_before_listing(self.config(
                    prerecord={"temp_max": 10 ** 9}, **others))

    def test_the_two_temperature_sentences_each_count_the_range(self):
        base = self.config()
        both = self.config(sentence_temp_max="最高気温は{temp_max}度なのだ。")
        self.assertEqual(phrases._estimate_work(both) - phrases._estimate_work(base), 46)

    def test_the_precipitation_sentence_adds_up_to_101(self):
        base = self.config()
        pop = self.config(sentence_pop="降水確率は{pop}パーセントなのだ。")
        self.assertEqual(phrases._estimate_work(pop) - phrases._estimate_work(base), 101)

    def test_many_places_and_times_are_refused_by_count(self):
        # 1 地点 × 100 の「いつ」 × 28 の天気 = 2800 > 2000。幅は小さいので気温では断らない。
        config = self.config(prerecord={"whens": ["日{0}".format(n) for n in range(100)]})
        message = self.refuses_before_listing(config)
        self.assertIn("2000", message)

    def test_a_wide_span_of_announcement_hours_is_refused_before_listing(self):
        config = Config(deep_merge(DEFAULT_CONFIG, {
            "schedule": {"hourly": {"start_hour": 0, "end_hour": 10 ** 9}}}), base_dir=REPO_ROOT)
        self.refuses_before_listing(config)

    def test_the_limit_can_be_given(self):
        # ひとこと抜きなら 81 件（時報 7 + 天気 74）。作る前の見積もりも同じ 81。
        config = self.config()
        self.assertEqual(len(phrases.collect_phrases(config, False)), 81)
        result = phrases.coverage(config, self.everything, include_quotes=False, max_phrases=81)
        self.assertEqual(result.total, 81)
        message = self.refuses_before_listing(config, include_quotes=False, max_phrases=80)
        self.assertIn("約 81 件", message)
        self.assertIn("上限は 80 件", message)

    def test_the_count_after_listing_is_checked_too(self):
        # ひとこと（ファイルの中身）は作る前には分からない。作ったあとの件数でも断る。
        listed = len(phrases.collect_phrases(self.default, True))
        self.assertEqual(listed, 138)
        self.assertEqual(phrases.coverage(self.default, self.everything, max_phrases=138).total, 138)
        with self.assertRaises(phrases.CoverageTooLarge) as caught:
            phrases.coverage(self.default, self.everything, max_phrases=137)
        self.assertIn("138 件", str(caught.exception))

    def test_none_turns_the_guard_off(self):
        config = self.config(prerecord={"temp_min": 0, "temp_max": 5000})
        result = phrases.coverage(config, self.everything, max_phrases=None)
        self.assertEqual(result.by_kind["weather"][1], 28 + 5001)

    def test_the_guard_applies_without_quotes_too(self):
        self.refuses_before_listing(self.config(prerecord={"temp_max": 10 ** 9}),
                                    include_quotes=False)

    def test_the_listing_functions_have_no_limit(self):
        # 生成スクリプトが使う列挙は、上限なしのまま（作り置きの対象を勝手に減らさない）。
        config = self.config(prerecord={"temp_min": 0, "temp_max": 3000})
        self.assertEqual(len(phrases.collect_phrases(config, False)), 7 + 28 + 3001)
        self.assertGreater(len(phrases.collect_phrases(config, True)), 7 + 28 + 3001)
        self.assertEqual(phrases.phrases_in_use(config), phrases.collect_phrases(config, True))

    def test_a_setting_that_cannot_be_read_fails_as_the_listing_does(self):
        # 設定が読めないのは、数え上げが省かれる理由ではない。列挙と同じ例外のまま。
        for override in ({"prerecord": {"whens": 5}}, {"prerecord": {"temp_min": [1]}}):
            with self.subTest(override=override):
                config = self.config(**override)
                try:
                    phrases.collect_phrases(config, True)
                except Exception as expected:
                    with self.assertRaises(type(expected)):
                        phrases.coverage(config, self.everything)
                else:  # 列挙が通る設定なら、数え上げも通る
                    phrases.coverage(config, self.everything)

    def test_the_estimate_is_never_below_the_listing(self):
        # 見積もりが列挙の読み出しとずれていないことの確認（ずれると上限が当てにならない）。
        pop = "降水確率は{pop}パーセントなのだ。"
        high = "最高気温は{temp_max}度なのだ。"
        two_places = [{"label": "大津", "latitude": 35.0, "longitude": 135.8},
                      {"label": "京都", "latitude": 35.0, "longitude": 135.7}]
        cases = [
            {},
            {"prerecord": {"temp_min": 0, "temp_max": 10, "pop_step": 1, "whens": ["今日", "明日"]},
             "sentence_temp_max": high, "sentence_pop": pop},
            {"open_meteo": {"locations": two_places}, "sentence_pop": pop},
            {"open_meteo": {"locations": []}},
            {"prerecord": {"temp_min": 30, "temp_max": 10}},
            {"prerecord": {"temp_min": "abc"}},
            {"sentence_weather": "", "sentence_temp": ""},
            {"sentence_temp": "あつい。", "sentence_temp_max": "あつい。"},
        ]
        for override in cases:
            with self.subTest(override=override):
                config = self.config(**override)
                actual = (len(list(phrases.announcement_phrases(config)))
                          + len(weather.prerecord_phrases(config.section("weather"))))
                self.assertGreaterEqual(phrases._estimate_work(config), actual)


class CoverageCharsGuardTest(unittest.TestCase):
    """``coverage`` は、件数が少なくても 1 件が巨大な設定を、列挙する前に断る。

    件数の上限（2000 件）だけでは、短い書式の幅（``{label:>200000000}``）や長い地点名で、
    起動のたびに数秒と数百 MB〜数 GB を使ってしまう（天気を止めていても列挙される。Pi 3B は
    1 GB なので、常駐が起動を繰り返して落ちる）。
    """

    def setUp(self):
        self.default = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)

    @staticmethod
    def everything(text):
        return "/voice/" + text + ".wav"

    def config(self, **override):
        return Config(deep_merge(DEFAULT_CONFIG, override), base_dir=REPO_ROOT)

    def refuses_before_listing(self, config, **kwargs):
        """``CoverageTooLarge`` を出し、天気の文言も時刻アナウンスも作らない。メッセージを返す。"""
        with mock.patch.object(weather, "prerecord_phrases",
                               side_effect=AssertionError("列挙してはいけない")), \
                mock.patch.object(phrases, "announcement_phrases",
                                  side_effect=AssertionError("列挙してはいけない")):
            with self.assertRaises(phrases.CoverageTooLarge) as caught:
                phrases.coverage(config, self.everything, **kwargs)
        return str(caught.exception)

    def labelled(self, length):
        """天気を止めたまま、地点名だけが ``length`` 文字ある設定。"""
        return self.config(weather={"enabled": False, "open_meteo": {"locations": [
            {"label": "あ" * length, "latitude": 35.0, "longitude": 135.0}]}})

    def test_the_limits_are_pinned(self):
        self.assertEqual(phrases.MAX_COVERAGE_CHARS, 2_000_000)
        self.assertEqual(phrases.MAX_FORMAT_SPEC, 1000)
        self.assertIs(inspect.signature(phrases.coverage).parameters["max_chars"].default,
                      phrases.MAX_COVERAGE_CHARS)

    def test_the_default_config_is_far_below_the_limit(self):
        listed = phrases.collect_phrases(self.default, True)
        self.assertEqual(len(listed), 138)
        self.assertLess(sum(len(text) for text in listed), phrases.MAX_COVERAGE_CHARS // 100)

    def test_a_huge_width_in_a_weather_template_is_refused_before_anything_is_built(self):
        # 天気を止めていても、数え上げは天気の文言も列挙する。幅 2 億の文は 200 MB になる。
        for template in ("{label:>200000000}", "{when}の{label:*^200000000}", "{weather:.1001}"):
            with self.subTest(template=template):
                config = self.config(weather={"enabled": False, "sentence_weather": template})
                message = self.refuses_before_listing(config)
                self.assertIn("weather.sentence_weather", message)
                self.assertIn("1000", message)

    def test_every_template_that_is_listed_is_checked(self):
        for key, name in (("sentence_temp", "temp"), ("sentence_temp_max", "temp_max"),
                          ("sentence_pop", "pop")):
            with self.subTest(key=key):
                config = self.config(weather={key: "{" + name + ":>5000}"})
                self.assertIn("weather." + key, self.refuses_before_listing(config))

    def test_a_huge_width_in_an_announcement_template_is_refused_before_anything_is_built(self):
        for name in ("announce_template", "noon_template"):
            with self.subTest(name=name):
                config = self.config(time_signal={name: "{period}{hour_reading:>200000000}"})
                message = self.refuses_before_listing(config)
                self.assertIn("time_signal." + name, message)

    def test_an_announcement_template_nobody_formats_is_not_judged(self):
        # 時報の時刻が 1 つも無ければ、時刻アナウンスは作られない。使われない書式は断らない。
        config = self.config(schedule={"hourly": {"start_hour": 17, "end_hour": 16}},
                             time_signal={"announce_template": "{hour_reading:>200000000}"})
        result = phrases.coverage(config, self.everything)
        self.assertEqual(result.by_kind["announce"][1], 0)

    def test_widths_up_to_the_limit_are_fine(self):
        config = self.config(weather={"sentence_weather": "{label:>1000}の{weather}"})
        result = phrases.coverage(config, self.everything)
        self.assertEqual(result.by_kind["weather"][1], 28 + 46)

    def test_a_long_location_name_is_refused_before_anything_is_built(self):
        # 地点 1 × 「いつ」 1 × 天気 28 = 28 文。1 文が 10 万文字なら 280 万文字。
        message = self.refuses_before_listing(self.labelled(100_000))
        self.assertIn("文字", message)
        self.assertIn("weather.open_meteo.locations", message)
        self.assertIn("weather.prerecord", message)

    def test_a_location_name_that_fits_is_still_counted(self):
        result = phrases.coverage(self.labelled(60_000), self.everything)  # 28 文 × 6 万 = 168 万
        self.assertEqual(result.by_kind["weather"][1], 28 + 46)

    def test_many_long_places_stop_the_estimate_early(self):
        place = {"label": "あ" * 10_000, "latitude": 35.0, "longitude": 135.0}
        config = self.config(weather={"open_meteo": {"locations": [place] * 50}})
        with mock.patch.object(phrases, "_Shape", wraps=phrases._Shape) as shape:
            message = self.refuses_before_listing(config, max_phrases=None)
        self.assertIn("weather.open_meteo.locations", message)
        self.assertLess(shape.call_count, 10)  # 見積もりは、テンプレートごとに 1 度だけ読む（50 地点分は読まない）

    def test_the_other_weather_sentences_count_by_their_widths_too(self):
        # 気温 46 通り × 幅 1000 で 46000 文字。件数も文字数も上限の内側。
        config = self.config(weather={"sentence_temp": "{temp:>1000}"})
        self.assertEqual(phrases.coverage(config, self.everything).by_kind["weather"][1], 28 + 46)
        # 幅 1000 の文が 3000 文。件数の上限（2000）に先に当たらないよう件数を外して、文字数で断る。
        wide = self.config(weather={"sentence_temp": "{temp:>1000}",
                                    "prerecord": {"temp_min": 0, "temp_max": 199}})
        phrases.coverage(wide, self.everything, max_chars=None)
        message = self.refuses_before_listing(wide, max_chars=100_000)
        self.assertIn("weather.sentence_temp", message)

    def test_an_estimate_is_never_below_the_real_length(self):
        # 見積もりが実際の長さを下回ると、上限が当てにならない。
        two_places = [{"label": "大津びわ湖", "latitude": 35.0, "longitude": 135.8},
                      {"label": "京都", "latitude": 35.0, "longitude": 135.7}]
        cases = [
            {},
            {"sentence_weather": "{when}の{label:>12}は{weather}。{label}"},
            {"sentence_temp": "{temp:>6}度", "sentence_temp_max": "最高{temp_max}度",
             "sentence_pop": "{pop:03d}パーセント",
             "prerecord": {"temp_min": -30, "temp_max": 99, "pop_step": 1,
                           "whens": ["今", "あした", "あさって"]},
             "open_meteo": {"locations": two_places}},
            {"sentence_weather": "{{{label}}}", "sentence_temp": "あ{temp:.1f}い"},
        ]
        for override in cases:
            with self.subTest(override=override):
                config = self.config(weather=override)
                actual = sum(len(text) for text in weather.prerecord_phrases(config.section("weather")))
                estimate = sum(chars for _, chars in phrases._weather_chars(config, 10 ** 12))
                self.assertGreaterEqual(estimate, actual)

    def test_the_length_after_listing_is_checked_too(self):
        # ひとこと（ファイルの中身）や時報の文言は、作る前には長さが分からない。作りながら数える。
        with tempfile.TemporaryDirectory() as directory:
            quotes = write_quotes(directory, {"general": ["あ" * 800_000] * 3})
            config = self.config(quotes={"file": quotes})
            with self.assertRaises(phrases.CoverageTooLarge) as caught:
                phrases.coverage(config, self.everything)
            self.assertIn("quotes.file", str(caught.exception))
            # 3 つとも同じ文なので、重複を除くと 1 件
            unlimited = phrases.coverage(config, self.everything, max_chars=None)
            self.assertEqual(unlimited.by_kind["quote"], (1, 1))

    def test_announcements_are_counted_while_they_are_built(self):
        # 午前の言い回しが 100 万文字なら、午前の 2 つ目で上限を超える。残りの時刻は作らない。
        config = self.config(time_signal={"period_am": "あ" * 1_000_000})
        built = []
        original = timesignal.announce_text

        def counted(hour, settings):
            built.append(hour)
            return original(hour, settings)

        with mock.patch.object(timesignal, "announce_text", counted):
            with self.assertRaises(phrases.CoverageTooLarge) as caught:
                phrases.coverage(config, self.everything)
        self.assertEqual(built, [10, 11, 12][:len(built)])
        self.assertLessEqual(len(built), 3)
        self.assertIn("time_signal", str(caught.exception))

    def test_none_turns_the_guard_off(self):
        # 10 万文字の地点名（280 万文字）も、上限を外せば数える。
        result = phrases.coverage(self.labelled(100_000), self.everything, max_chars=None)
        self.assertEqual(result.by_kind["weather"][1], 28 + 46)

    def test_the_count_guard_and_the_length_guard_are_independent(self):
        config = self.labelled(100_000)
        phrases.coverage(config, self.everything, max_phrases=None, max_chars=None)
        self.refuses_before_listing(config, max_phrases=None)
        self.refuses_before_listing(config, max_phrases=10 ** 6)

    def test_the_listing_functions_have_no_length_limit(self):
        # 生成スクリプトが使う列挙は、上限なしのまま（作り置きの対象を勝手に減らさない）。
        listed = phrases.collect_phrases(self.labelled(100_000), False)
        self.assertGreater(sum(len(text) for text in listed), phrases.MAX_COVERAGE_CHARS)

    def test_a_template_that_cannot_be_read_fails_as_the_listing_does(self):
        # 波括弧が壊れているのは、数え上げが省かれる理由ではない。列挙と同じ例外のまま。
        for template in ("{label", "{label:>}}", "{"):
            with self.subTest(template=template):
                config = self.config(weather={"sentence_weather": template})
                with self.assertRaises(ValueError) as caught:
                    phrases.collect_phrases(config, True)
                with self.assertRaises(type(caught.exception)) as again:
                    phrases.coverage(config, self.everything)
                self.assertNotIsInstance(again.exception, phrases.CoverageTooLarge)

    def test_a_spec_number_is_read_without_converting_a_huge_integer(self):
        self.assertEqual(phrases._spec_number(""), 0)
        self.assertEqual(phrases._spec_number(">200"), 200)
        self.assertEqual(phrases._spec_number("0>5.12f"), 12)
        self.assertGreater(phrases._spec_number(">" + "9" * 5000), phrases.MAX_FORMAT_SPEC)

    def test_the_messages_do_not_show_a_python_class_name(self):
        configs = [self.labelled(100_000),
                   self.config(weather={"sentence_weather": "{label:>200000000}"}),
                   self.config(weather={"prerecord": {"temp_max": 10 ** 9}}),
                   self.config(weather={"prerecord": {"whens": ["日{0}".format(n) for n in range(100)]}})]
        for config in configs:
            with self.subTest():
                message = self.refuses_before_listing(config)
                self.assertNotIn("CoverageTooLarge", message)
                self.assertNotRegex(message, r"[A-Za-z]+Error")

    def test_every_message_names_the_setting_to_fix(self):
        wide_range = self.refuses_before_listing(self.config(weather={"prerecord": {"temp_max": 10 ** 9}}))
        self.assertIn("weather.prerecord", wide_range)
        many = self.refuses_before_listing(self.config(weather={"prerecord": {
            "whens": ["日{0}".format(n) for n in range(100)]}}))
        self.assertIn("weather.prerecord", many)
        self.assertIn("weather.open_meteo.locations", many)


class LayeringTest(unittest.TestCase):
    def test_importing_phrases_does_not_pull_in_the_playback_stack(self):
        """生成スクリプトに pygame を引き込まないよう、再生系を import しない。"""
        code = "\n".join([
            "import sys",
            "import chime.phrases",
            "banned = ('chime.audio', 'chime.sequence', 'chime.app', 'pygame')",
            "loaded = [name for name in banned if name in sys.modules]",
            "print(','.join(loaded))",
            "sys.exit(1 if loaded else 0)",
        ])
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True)
        self.assertEqual(result.returncode, 0,
                         "chime.phrases が再生系を import しています: {0}\n{1}".format(
                             result.stdout.strip(), result.stderr.strip()))


if __name__ == "__main__":
    unittest.main()
