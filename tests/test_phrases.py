"""読み上げうる全文言の列挙（``chime/phrases.py``）のテスト。

Pi には実行時の音声合成が無く、声は文言の**完全一致**で作り置きから引く。
列挙が 1 文字でもずれると、その文だけ Pi で無音になる。ここでは既定設定・
現地設定それぞれの列挙結果を件数とハッシュで固定し（特性テスト）、整理で
結果が変わらないことを確かめる。あわせて、``--config`` の文言と既定の文言の
和集合、``--prune`` で残す文言、列挙を分けた各関数、再生系を import しない
こと（層の分け方）を調べる。
"""

from __future__ import annotations

import hashlib
import inspect
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import KYOTO_ONLY, REPO_ROOT, load_manifest, write_quotes

from chime import phrases, timesignal, weather
from chime.config import DEFAULT_CONFIG, Config, deep_merge
from chime.quotes import load_quotes

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
