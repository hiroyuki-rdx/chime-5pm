"""文書の記載と実装の一致を確かめるテスト（標準ライブラリのみ）。

文書に書いた ``--say`` の例が作り置きに無い文言だと、読者が試しても無音になる
（Pi には実行時の合成手段が無い）。版の表記が文書ごとにずれると、どれが最新か
分からなくなる。文書に書いた件数（作り置きの総数、天気コードの語数など）が実装と
ずれると、確認手順の「138 件あるか」で正常な状態を異常と取り違える。廃止した設定
キーが現行の設定として書かれたままだと、読者が書いてしまい、警告が出る。
どれも目で見つけにくいので、機械的に確かめる。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import unittest
from typing import List, NamedTuple, Tuple

from tests.support import REPO_ROOT

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import generate_voicevox  # noqa: E402

from chime import __version__, cli, timesignal, weather  # noqa: E402
from chime import config as chime_config  # noqa: E402
from chime.config import BASE_DIR, DEFAULT_CONFIG, Config  # noqa: E402
from chime.quotes import load_quotes  # noqa: E402
from chime.state import MAX_RECENT_QUOTES  # noqa: E402

MANIFEST_PATH = os.path.join(REPO_ROOT, "assets", "voice", "manifest.json")
CHANGELOG_PATH = os.path.join(REPO_ROOT, "CHANGELOG.md")

#: ``--say "…"`` / ``--say '…'`` / ``--say …``（引数 1 つ分）を拾う。
#: ``--say`` の直後がバッククォートのもの（「`--say` に空文字列を…」のような
#: 文中の言及）は、空白が続かないので拾わない。
_SAY_EXAMPLE = re.compile(r"""--say[ \t]+(?:"([^"\n]*)"|'([^'\n]*)'|([^\s`"']+))""")

#: 文言ではなく、書き方を示す記号（``--say TEXT`` や ``--say "$phrase"`` など）。
_PLACEHOLDER = re.compile(r"^(?:[A-Z_]+|\$.*|<.*>|\{.*\}|\.{3}|…)$")


def say_examples(text: str) -> list:
    """``text`` に書かれた ``--say`` の例の文言を、書かれた順に返す。

    ``TEXT`` や ``$phrase`` のような書き方を示す記号は含めない。
    """
    found = []
    for match in _SAY_EXAMPLE.finditer(text):
        phrase = next(group for group in match.groups() if group is not None)
        if phrase and not _PLACEHOLDER.match(phrase):
            found.append(phrase)
    return found


def doc_paths() -> list:
    return [os.path.join(REPO_ROOT, "README.md")] + sorted(
        glob.glob(os.path.join(REPO_ROOT, "docs", "*.md")))


def current_doc_paths() -> list:
    """現行の仕様として読まれる文書（開発履歴 ``DEVELOPMENT_LOG.md`` を除く）。

    開発履歴は当時の出来事の記録なので、廃止したキーを現在形で書いても構わない。
    """
    return [path for path in doc_paths()
            if os.path.basename(path) != "DEVELOPMENT_LOG.md"]


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def load_manifest() -> dict:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


class SayExampleExtractionTest(unittest.TestCase):
    """検査そのものが、例を取りこぼさず、記号を拾いすぎないこと。"""

    def test_quoted_and_unquoted_examples(self):
        text = ('python3 campus_chime.py --say "正午をお知らせしたのだ。"   # 読み上げ\n'
                "campus_chime.py --say '午後3時をお知らせしたのだ。'\n"
                "campus_chime.py --say こんにちは      任意の文言を読み上げる\n")
        self.assertEqual(say_examples(text), [
            "正午をお知らせしたのだ。", "午後3時をお知らせしたのだ。", "こんにちは"])

    def test_placeholders_and_mentions_are_not_examples(self):
        text = ("| `--say TEXT` | 任意文言の読み上げ |\n"
                "`--say ... --dry-run` で 1 件ずつ流し\n"
                '--say "$phrase" --dry-run --backend mock\n'
                "`--say`・`--test-hourly` で実行した再生\n"
                "`--say` に空文字列を渡した場合\n"
                '--say ""\n')
        self.assertEqual(say_examples(text), [])

    def test_a_phrase_without_a_shipped_voice_is_detected(self):
        manifest = load_manifest()
        examples = say_examples('python3 campus_chime.py --say "テストです"')
        self.assertEqual(examples, ["テストです"])
        self.assertNotIn(examples[0], manifest)


class SayExamplesInDocsTest(unittest.TestCase):
    """文書に書いた ``--say`` の例は、すべて作り置きにある文言であること。"""

    def setUp(self):
        self.manifest = load_manifest()

    def test_the_examples_in_readme_and_docs_have_shipped_voices(self):
        missing = []
        for path in doc_paths():
            for number, line in enumerate(read_text(path).splitlines(), start=1):
                for phrase in say_examples(line):
                    if phrase not in self.manifest:
                        missing.append("{0}:{1}: {2}".format(
                            os.path.relpath(path, REPO_ROOT), number, phrase))
        self.assertEqual(
            missing, [],
            "作り置き（assets/voice/manifest.json）に無い文言が --say の例に書かれています")

    def test_readme_has_a_say_example(self):
        # 検査が何も拾えないまま通ってしまわないための確認
        self.assertTrue(say_examples(read_text(os.path.join(REPO_ROOT, "README.md"))))

    def test_the_examples_in_the_cli_help_have_shipped_voices(self):
        missing = [phrase for phrase in say_examples(cli.EPILOG) if phrase not in self.manifest]
        self.assertEqual(
            missing, [],
            "chime/cli.py の EPILOG（--help の使用例）に、作り置きに無い文言が書かれています")


class VersionInDocsTest(unittest.TestCase):
    """文書に書いた版が ``chime/__init__.py`` の ``__version__`` と一致すること。"""

    VERSION = r"(\d+\.\d+\.\d+)"

    def test_readme_marks_the_current_version_in_bold_in_the_history_table(self):
        text = read_text(os.path.join(REPO_ROOT, "README.md"))
        bold = re.findall(r"^\|\s*\*\*v{0}\*\*\s*\|".format(self.VERSION), text, re.MULTILINE)
        self.assertEqual(bold, [__version__],
                         "README の版表で太字にする行は、現行版（{0}）の 1 行だけです".format(__version__))

    def test_requirements_header_version(self):
        text = read_text(os.path.join(REPO_ROOT, "docs", "REQUIREMENTS.md"))
        match = re.search(r"\*\*バージョン:\*\*\s*{0}".format(self.VERSION), text)
        self.assertIsNotNone(match, "REQUIREMENTS.md のヘッダに版がありません")
        self.assertEqual(match.group(1), __version__)

    def test_requirements_history_marks_the_current_version_as_this_document(self):
        text = read_text(os.path.join(REPO_ROOT, "docs", "REQUIREMENTS.md"))
        rows = re.findall(r"^\|\s*v{0}\s*\|[^|]*\|\s*本書。".format(self.VERSION), text,
                          re.MULTILINE)
        self.assertEqual(rows, [__version__],
                         "REQUIREMENTS の経緯表で「本書。」と書く行は、現行版（{0}）の 1 行だけです"
                         .format(__version__))

    def test_specification_header_version(self):
        text = read_text(os.path.join(REPO_ROOT, "docs", "SPECIFICATION.md"))
        match = re.search(r"\*\*バージョン:\*\*\s*{0}".format(self.VERSION), text)
        self.assertIsNotNone(match, "SPECIFICATION.md のヘッダに版がありません")
        self.assertEqual(match.group(1), __version__)

    def test_specification_points_at_the_matching_requirements(self):
        text = read_text(os.path.join(REPO_ROOT, "docs", "SPECIFICATION.md"))
        match = re.search(r"\*\*対応要件定義:\*\*\s*`docs/REQUIREMENTS\.md`\s*v{0}"
                          .format(self.VERSION), text)
        self.assertIsNotNone(match, "SPECIFICATION.md のヘッダに対応要件定義の版がありません")
        self.assertEqual(match.group(1), __version__)


# -- CHANGELOG ---------------------------------------------------------------

#: ``## [5.3.0] - 2026-09-29`` のような版の見出し。``## [Unreleased]`` は拾わない。
_CHANGELOG_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)

#: 末尾の ``[5.3.0]: https://…`` のようなリンク定義。
_CHANGELOG_LINK = re.compile(r"^\[(\d+\.\d+\.\d+)\]:[ \t]*(\S+)", re.MULTILINE)


def changelog_versions(text: str) -> List[str]:
    """``CHANGELOG`` の版の見出しを、書かれた順（新しい順）に返す。"""
    return _CHANGELOG_HEADING.findall(text)


def changelog_links(text: str) -> dict:
    """``CHANGELOG`` 末尾のリンク定義を ``{版: URL}`` で返す。"""
    return {version: url for version, url in _CHANGELOG_LINK.findall(text)}


class ChangelogParsingTest(unittest.TestCase):
    """検査そのものが、見出しとリンク定義を取り違えないこと。"""

    TEXT = ("# 変更履歴\n\n## [Unreleased]\n\n- 作業中\n\n"
            "## [1.2.0] - 2026-01-02\n\n本文 [1.0.0] を参照\n\n"
            "## [1.0.0] - 2026-01-01\n\n"
            "[1.2.0]: https://example.com/releases/tag/v1.2.0\n"
            "[1.0.0]: https://example.com/releases/tag/v1.0.0\n")

    def test_versions_are_listed_newest_first_and_skip_unreleased(self):
        self.assertEqual(changelog_versions(self.TEXT), ["1.2.0", "1.0.0"])

    def test_links_are_read_from_the_reference_definitions_only(self):
        self.assertEqual(changelog_links(self.TEXT), {
            "1.2.0": "https://example.com/releases/tag/v1.2.0",
            "1.0.0": "https://example.com/releases/tag/v1.0.0",
        })

    def test_a_changelog_without_links_has_none(self):
        self.assertEqual(changelog_links("## [1.0.0] - 2026-01-01\n"), {})


class ChangelogMatchesTheVersionTest(unittest.TestCase):
    """``CHANGELOG.md`` が、現行版（``chime.__version__``）を指していること。"""

    def setUp(self):
        self.text = read_text(CHANGELOG_PATH)

    def test_the_first_version_heading_is_the_current_version(self):
        versions = changelog_versions(self.text)
        self.assertTrue(versions, "CHANGELOG.md に「## [x.y.z]」の見出しがありません")
        self.assertEqual(
            versions[0], __version__,
            "CHANGELOG.md の先頭の版（{0}）が chime/__init__.py の __version__（{1}）と違います。"
            "版を上げるときは両方を更新します".format(versions[0], __version__))

    def test_the_current_version_has_a_link_reference_when_links_are_used(self):
        links = changelog_links(self.text)
        if not links:
            return  # リンク定義を使わない書き方なら、検査することが無い
        self.assertIn(
            __version__, links,
            "CHANGELOG.md の末尾に、現行版（{0}）のリンク定義「[{0}]: https://…」がありません"
            .format(__version__))
        self.assertTrue(
            links[__version__].endswith("v" + __version__),
            "現行版のリンク先が v{0} を指していません（前の版の行を写した書き間違いの疑い）: {1}"
            .format(__version__, links[__version__]))


# -- 文書に書いた件数 ----------------------------------------------------------


class Claim(NamedTuple):
    """文書に書かれた、実装から数えられる数値の記述 1 種類。"""

    path: str        #: ``REPO_ROOT`` からの相対パス
    name: str        #: 何についての記述か（失敗の表示用）
    pattern: str     #: 記述を拾う正規表現。数値は、この順に並ぶグループ
    expected: tuple  #: 各グループの数値（文書の書き方に合わせて文字列と比べる）


def check_claim(text: str, pattern: str, expected: tuple) -> Tuple[int, list]:
    """``pattern`` に一致した箇所の数を、食い違った記述の一覧と共に返す。"""
    wanted = tuple(str(value) for value in expected)
    found = 0
    wrong = []
    for match in re.finditer(pattern, text):
        found += 1
        if match.groups() != wanted:
            wrong.append((match.group(0), match.groups()))
    return found, wrong


def _count_phrases(settings: dict, **overrides) -> int:
    changed = dict(settings)
    changed.update(overrides)
    return len(weather.prerecord_phrases(changed))


class DocumentedCounts:
    """既定の設定から数えた、文書に書かれる件数。"""

    def __init__(self) -> None:
        config = Config(DEFAULT_CONFIG, base_dir=BASE_DIR)
        self.config = config

        # 作り置きの総数（文言の重複は 1 件に数える）と、その内訳
        self.total = len(generate_voicevox.collect_phrases(config, include_quotes=True))
        self.total_without_quotes = len(
            generate_voicevox.collect_phrases(config, include_quotes=False))

        hourly = config.section("schedule.hourly")
        self.announcements = len({
            timesignal.announce_text(hour, config.section("time_signal"))
            for hour in range(int(hourly["start_hour"]), int(hourly["end_hour"]) + 1)})

        quotes = load_quotes(config.path("quotes.file"))
        texts = list(quotes.get("general", []))
        for values in quotes.get("by_hour", {}).values():
            texts.extend(values)
        self.quotes = len(set(texts))

        weather_settings = config.section("weather")
        self.weather = len(weather.prerecord_phrases(weather_settings))
        # 天気の文だけ（地点 × 天気コード）と、気温の文だけ
        silent = {"sentence_temp_max": "", "sentence_pop": ""}
        self.weather_sentences = _count_phrases(weather_settings, sentence_temp="", **silent)
        self.temp_sentences = _count_phrases(weather_settings, sentence_weather="", **silent)

        prerecord = weather_settings["prerecord"]
        self.temp_min = prerecord["temp_min"]
        self.temp_max = prerecord["temp_max"]
        self.wmo_words = len(weather.WMO_CODES)

        # 廃止した設定キーの数（文書は、種類ごとの数も書く）
        removed = list(chime_config.REMOVED_KEYS)
        self.removed = len(removed)
        self.removed_extra_segment = len([k for k in removed if k.startswith("extra_segment.")])
        self.removed_weather = len([k for k in removed if k.startswith("weather.")])


def documented_claims(counts: DocumentedCounts) -> List[Claim]:
    """文書に書かれた件数の記述と、あるべき値の一覧。

    正規表現は書き方に合わせて狭く取る。文書の文言を直したときに一致しなくなったら、
    検査が空振りしないよう、一致が 0 件の記述として失敗する（正規表現を直すこと）。
    """
    total = counts.total
    config = counts.config
    hourly = config.section("schedule.hourly")
    closing = config.section("schedule.closing")
    spec = "docs/SPECIFICATION.md"
    setup = "docs/SETUP.md"
    return [
        # 作り置きの総数
        Claim("README.md", "作り置きの総数", r"事前生成して同梱（(\d+) 件）", (total,)),
        Claim(setup, "作り置きの総数と内訳",
              r"既定の設定では \*\*(\d+) 件\*\*（時刻アナウンス (\d+) ＋ ひとこと (\d+) ＋ 天気 (\d+)。",
              (total, counts.announcements, counts.quotes, counts.weather)),
        Claim(setup, "天気の内訳", r"天気の内訳は、天気の文 (\d+) ＋ 気温の文 (\d+)）",
              (counts.weather_sentences, counts.temp_sentences)),
        Claim(setup, "確認手順の件数", r"\*\*(\d+) 件\*\*あるか確認", (total,)),
        Claim("docs/KNOWLEDGE_BASE.md", "確認手順の件数", r"wc -l\s+# (\d+) 件あるか", (total,)),
        Claim("docs/REQUIREMENTS.md", "作り置きの総数", r"現在は (\d+) 文言すべてが作り置き済み",
              (total,)),
        # 天気コードの語数（WMO_CODES）
        Claim(spec, "天気コードの語数", r"`WMO_CODES`[（・](\d+) 語", (counts.wmo_words,)),
        Claim("docs/REQUIREMENTS.md", "天気コードの語数", r"天気コード（(\d+) 語）",
              (counts.wmo_words,)),
        Claim("docs/KNOWLEDGE_BASE.md", "天気コードの語数", r"天気コード（(\d+) 語）",
              (counts.wmo_words,)),
        # 事前生成する気温の範囲
        Claim(spec, "事前生成する気温の範囲",
              r"\| `prerecord\.temp_min` / `temp_max` \| `(-?\d+)` / `(-?\d+)` \|",
              (counts.temp_min, counts.temp_max)),
        # 設定表の既定値のうち、数えられるもの
        Claim(spec, "時報の時間帯（開始）", r"\| `hourly\.start_hour` \| `(\d+)` \|",
              (hourly["start_hour"],)),
        Claim(spec, "時報の時間帯（終了）", r"\| `hourly\.end_hour` \| `(\d+)` \|",
              (hourly["end_hour"],)),
        Claim(spec, "閉館放送の時刻",
              r"\| `closing\.hour` / `closing\.minute` \| `(\d+)` / `(\d+)` \|",
              (closing["hour"], closing["minute"])),
        Claim(spec, "天気を流す時刻", r"\| `weather_hours` \| `(\[[\d, ]*\])` \|",
              (json.dumps(config.get("extra_segment.weather_hours")),)),
        Claim(spec, "ひとことの直近除外", r"\| `avoid_recent` \| `(\d+)` \|",
              (config.get("quotes.avoid_recent"),)),
        Claim(spec, "天気のキャッシュ時間", r"\| `cache_minutes` \| `(\d+)` \|",
              (config.get("weather.cache_minutes"),)),
        # 状態ファイルが覚えるひとことの件数
        Claim(spec, "状態ファイルが覚えるひとこと", r"`remember_quote\(\)` は直近 (\d+) 件まで保持",
              (MAX_RECENT_QUOTES,)),
        # 廃止した設定キーの数
        Claim(spec, "廃止したキーの数", r"次の (\d+) キーは v6\.0\.0 で廃止した", (counts.removed,)),
        Claim(spec, "廃止したキーの数（extra_segment）", r"`extra_segment` の (\d+) キーが",
              (counts.removed_extra_segment,)),
        Claim(spec, "廃止したキーの数（weather）", r"`weather` の (\d+) キーが",
              (counts.removed_weather,)),
        Claim(setup, "廃止したキーの数（extra_segment）",
              r"廃止した (\d+) 項目 `extra_segment\.mode`", (counts.removed_extra_segment,)),
        Claim(setup, "廃止したキーの数（weather）",
              r"廃止した (\d+) 項目 `weather\.provider`", (counts.removed_weather,)),
        Claim("docs/KNOWLEDGE_BASE.md", "廃止したキーの数", r"廃止した (\d+) キーのうち",
              (counts.removed,)),
    ]


class ClaimCheckTest(unittest.TestCase):
    """検査そのものが、食い違いを見つけ、記述が消えたことにも気づくこと。"""

    PATTERN = r"（(\d+) 件）"

    def test_a_matching_number_passes(self):
        self.assertEqual(check_claim("同梱（138 件）", self.PATTERN, (138,)), (1, []))

    def test_a_wrong_number_is_reported(self):
        found, wrong = check_claim("同梱（137 件）と（138 件）", self.PATTERN, (138,))
        self.assertEqual(found, 2)
        self.assertEqual(wrong, [("（137 件）", ("137",))])

    def test_every_group_of_a_multi_number_claim_is_compared(self):
        pattern = r"(\d+) ＋ (\d+)"
        self.assertEqual(check_claim("7 ＋ 57", pattern, (7, 57)), (1, []))
        self.assertEqual(check_claim("7 ＋ 56", pattern, (7, 57))[1], [("7 ＋ 56", ("7", "56"))])

    def test_a_missing_statement_is_reported_as_zero_matches(self):
        # 記述が消えた・言い換えられた場合に、通ったことにしない
        self.assertEqual(check_claim("件数の記述なし", self.PATTERN, (138,)), (0, []))


class DocumentedCountsTest(unittest.TestCase):
    """README・文書に書かれた件数が、既定の設定から数えた値と一致すること。"""

    @classmethod
    def setUpClass(cls):
        cls.counts = DocumentedCounts()

    def test_the_breakdown_adds_up_to_the_total(self):
        # 文書は「138 ＝ 7 ＋ 57 ＋ 74」と内訳も書く。数え方がずれていないことの確認
        counts = self.counts
        self.assertEqual(counts.announcements + counts.quotes + counts.weather, counts.total)
        self.assertEqual(counts.weather_sentences + counts.temp_sentences, counts.weather)
        self.assertEqual(counts.temp_sentences, counts.temp_max - counts.temp_min + 1)
        self.assertEqual(counts.total - counts.total_without_quotes, counts.quotes)

    def test_the_counts_stated_in_the_docs_match_the_code(self):
        problems = []
        for claim in documented_claims(self.counts):
            text = read_text(os.path.join(REPO_ROOT, claim.path))
            found, wrong = check_claim(text, claim.pattern, claim.expected)
            if not found:
                problems.append("{0}: {1}: 記述が見つかりません（文書の書き方が変わったなら"
                                "正規表現を直す）: {2}".format(claim.path, claim.name, claim.pattern))
            for quoted, stated in wrong:
                problems.append("{0}: {1}: 文書は {2} と書いていますが、実装から数えると {3} です: {4}"
                                .format(claim.path, claim.name, stated, claim.expected, quoted))
        self.assertEqual(problems, [])


# -- 廃止した設定キー -----------------------------------------------------------

#: 設定キーを指す書き方（``chime.config.REMOVED_KEYS`` の全キーを覆う）。
_REMOVED_KEY_TOKENS = (
    "weather_probability", "always_weather_hours", "always_quote_hours",
    "fallback_to_quote", "max_weather_chars", "extra_segment.mode",
    "weather.provider", "weather.jma",
)

#: 値や、キー名の一部だけを指す書き方（これらも廃止したものを指す）。
_REMOVED_KEY_VARIANTS = ('mode="choice"', '"choice"', "`provider`", "`jma`")

REMOVED_KEY_TOKENS = _REMOVED_KEY_TOKENS + _REMOVED_KEY_VARIANTS

#: 廃止したキーに触れる行が、必ず持つべき語。
REMOVAL_WORD = "廃止"


def lines_naming_removed_keys_without_the_removal_word(text: str) -> List[Tuple[int, str]]:
    """廃止したキーに触れているのに「廃止」と書かれていない行を、行番号付きで返す。

    そうした行は、廃止したキーを現行の設定として説明しているように読める
    （読者が ``config.json`` に書き写し、起動のたびに警告が出る）。
    """
    return [(number, line) for number, line in enumerate(text.splitlines(), start=1)
            if any(token in line for token in REMOVED_KEY_TOKENS)
            and REMOVAL_WORD not in line]


class RemovedKeyCheckerTest(unittest.TestCase):
    """検査そのものが、違反を見つけ、「廃止」と書いた行は通すこと。"""

    def test_a_line_presenting_a_removed_key_as_current_is_flagged(self):
        for line in ("| `weather_probability` | `0.0` | 天気予報が選ばれる確率 |",
                     "| `mode` | `\"both\"` | `choice` = どちらか一方を選ぶ。`extra_segment.mode` |",
                     "`mode=\"choice\"` なら確率で選ぶ",
                     "`weather.provider` を `jma` にする",
                     "| `provider` | `\"open_meteo\"` | 取得先 |",
                     "天気の取得先は `jma` でも選べる",
                     "`max_weather_chars` で文字数を絞る",
                     "`always_weather_hours` / `always_quote_hours` / `fallback_to_quote`",
                     "`weather.jma.area_code` に地域コードを書く",
                     "`mode` は \"choice\" か \"both\"",
                     ):
            with self.subTest(line=line):
                self.assertEqual(
                    lines_naming_removed_keys_without_the_removal_word(line), [(1, line)])

    def test_a_line_saying_the_key_was_removed_is_accepted(self):
        for line in ("v6.0.0 で廃止した `weather_probability` は、書いても無視される",
                     "廃止: `extra_segment.mode=\"choice\"`（`weather_hours` に一本化）",
                     "| `weather.provider` を廃止 | 起動時に警告 |"):
            with self.subTest(line=line):
                self.assertEqual(lines_naming_removed_keys_without_the_removal_word(line), [])

    def test_lines_that_do_not_name_a_removed_key_are_ignored(self):
        text = ("| `weather_hours` | `[12]` | 天気を流す時刻 |\n"
                "| `weather.open_meteo.locations` | 大津 |\n"
                "気象庁の予報文は自由文で作り置きできない\n"
                "provider という語だけでは拾わない\n")
        self.assertEqual(lines_naming_removed_keys_without_the_removal_word(text), [])

    def test_the_line_numbers_point_at_the_violating_lines(self):
        text = "正常な行\n`jma` の行\n廃止した `jma` の行\n`weather_probability` の行\n"
        self.assertEqual(
            [number for number, _ in lines_naming_removed_keys_without_the_removal_word(text)],
            [2, 4])

    def test_every_removed_config_key_is_covered_by_a_token(self):
        # 実装側で廃止するキーが増えたとき、この検査の対象にも足すことを忘れさせない
        uncovered = [key for key in chime_config.REMOVED_KEYS
                     if not any(token in key for token in _REMOVED_KEY_TOKENS)]
        self.assertEqual(uncovered, [])


class RemovedKeysInDocsTest(unittest.TestCase):
    """廃止したキーは、文書で現行の設定として書かれていないこと。"""

    def test_lines_naming_a_removed_key_say_that_it_was_removed(self):
        violations = []
        for path in current_doc_paths():
            text = read_text(path)
            for number, line in lines_naming_removed_keys_without_the_removal_word(text):
                violations.append("{0}:{1}: {2}".format(
                    os.path.relpath(path, REPO_ROOT), number, line.strip()[:100]))
        self.assertEqual(
            violations, [],
            "廃止したキーに触れる行には「{0}」と書いてください（現行の設定に読めてしまいます）"
            .format(REMOVAL_WORD))

    def test_the_docs_do_mention_the_removed_keys_as_removed(self):
        # 検査が何も拾えないまま通ってしまわないための確認。廃止の案内そのものは、
        # 利用者が journalctl の警告を見て調べに来る先として残すべき情報である
        for name in ("docs/SPECIFICATION.md", "docs/SETUP.md", "docs/KNOWLEDGE_BASE.md"):
            with self.subTest(doc=name):
                text = read_text(os.path.join(REPO_ROOT, name))
                mentions = [line for line in text.splitlines()
                            if any(token in line for token in REMOVED_KEY_TOKENS)]
                self.assertTrue(mentions, "{0} に廃止したキーの案内がありません".format(name))

    def test_the_docs_checked_exclude_only_the_development_log(self):
        names = sorted(os.path.relpath(path, REPO_ROOT) for path in current_doc_paths())
        self.assertIn("README.md", names)
        self.assertIn(os.path.join("docs", "SPECIFICATION.md"), names)
        self.assertNotIn(os.path.join("docs", "DEVELOPMENT_LOG.md"), names)


if __name__ == "__main__":
    unittest.main()
