"""文書の記載と実装の一致を確かめるテスト（標準ライブラリのみ）。

文書に書いた ``--say`` の例が作り置きに無い文言だと、読者が試しても無音になる
（Pi には実行時の合成手段が無い）。版の表記が文書ごとにずれると、どれが最新か
分からなくなる。文書に書いた件数（作り置きの総数、天気コードの語数など）が実装と
ずれると、確認手順の「138 件あるか」で正常な状態を異常と取り違える。廃止した設定
キーが現行の設定として書かれたままだと、読者が書いてしまい、警告が出る。
どれも目で見つけにくいので、機械的に確かめる。

運用の道具（``--check``・``--status``・``--wait-idle``・``scripts/update.sh``）については、
すべてのオプション・モジュール・スクリプト・テストファイルが文書の一覧に載っていること、
文書が指す節が実在すること、出力の例が実装の出力と同じ形であること、待機の秒数や履歴の
件数が実装の定数と一致すること、手作業の確認（``ls … | wc -l`` など）が戻っていないことを
確かめる。

文書に例として載せた文面（履歴の行・``--check`` の出力の文言）は、手で書いた辞書や文字列
ではなく、実装そのものに出させた文面と一致させる（実装が記録できない文面を例にしていた
ことがある）。設定の検査で何を置き換え、何を警告にとどめるかの表も、実装に値を入れて
確かめた結果と一致させる。

数え間違い（表の行数と「上の N つ」の食い違い）、言い過ぎ（途中で切れた MP3 を見つけると
書いたのに、先頭しか見ていなかった）、言い落とし（原因ごとの案内、上限の値、簡易の内容で
鳴った放送の表示）も、実装の動きと定数に合わせて確かめる。ソースの書き方（無効な
エスケープの警告）も、ここで確かめる。
"""

from __future__ import annotations

import copy
import glob
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from typing import List, NamedTuple, Tuple
from unittest import mock
from zoneinfo import ZoneInfo

from tests.support import REPO_ROOT, load_manifest

from chime import __version__, buildinfo, check, cli, configcheck, phrases, status, timesignal, weather
from chime import config as chime_config
from chime.app import MISSING_PREVIEW, RETRY_SECONDS, ChimeApp
from chime.audio import Segment
from chime.check import INFO_LIMIT, LIST_LIMIT, Result, prerecorded_coverage, render_section, summarize
from chime.config import BASE_DIR, DEFAULT_CONFIG, Config
from chime.history import KEEP_LINES, MAX_LINES, History, make_entry
from chime.phrases import announcement_phrases, collect_phrases
from chime.quotes import load_quotes
from chime.scheduler import Scheduler
from chime.sequence import PlaybackPlan
from chime.state import MAX_RECENT_QUOTES

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
        self.total = len(collect_phrases(config, include_quotes=True))
        self.total_without_quotes = len(collect_phrases(config, include_quotes=False))

        self.announcements = len(set(announcement_phrases(config)))

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
        # --check / --status の出力に書いた件数（手作業で数える手順は、これに置き換えた）
        Claim(setup, "確認手順の件数（--check）", r"「(\d+) 件すべてそろっています」", (total,)),
        Claim("docs/KNOWLEDGE_BASE.md", "確認手順の件数（--check）",
              r"「(\d+) 件すべてそろっています」", (total,)),
        Claim(setup, "--check の出力例の内訳",
              r"(\d+) 件すべてそろっています（時刻アナウンス (\d+)/(\d+)・ひとこと (\d+)/(\d+)・天気 (\d+)/(\d+)）",
              (total, counts.announcements, counts.announcements, counts.quotes, counts.quotes,
               counts.weather, counts.weather)),
        Claim(setup, "古い config.json の例の内訳",
              r"(\d+) 件中 (\d+) 件の声がありません（時刻アナウンス 0/(\d+)・ひとこと (\d+)/(\d+)・天気 (\d+)/(\d+)）",
              (total, counts.announcements, counts.announcements, counts.quotes, counts.quotes,
               counts.weather, counts.weather)),
        Claim(spec, "起動時のログの作り置きの件数", r"作り置きの音声: (\d+)/(\d+) 件", (total, total)),
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
    ] + tool_claims()


def tool_claims() -> List[Claim]:
    """道具（``--check``・``--status``・``--wait-idle``・履歴）について文書に書かれた数値と、実装の定数。"""
    readme = "README.md"
    setup = "docs/SETUP.md"
    kb = "docs/KNOWLEDGE_BASE.md"
    spec = "docs/SPECIFICATION.md"
    req = "docs/REQUIREMENTS.md"
    wait = status.WAIT_IDLE_MAX
    window = (int(status.WINDOW_BEFORE_SECONDS), int(status.WINDOW_AFTER_SECONDS["hourly"]),
              int(status.WINDOW_AFTER_SECONDS["closing"]))
    return [
        # --wait-idle の上限と、放送の時間帯
        Claim(readme, "待つ上限", r"(?:待つ（|--wait-idle`。)最大 (\d+) 秒", (wait,)),
        Claim(readme, "待っても終わらないとき", r"(\d+) 秒待っても終わらな", (wait,)),
        Claim(setup, "待つ上限", r"待つのは最大 (\d+) 秒", (wait,)),
        Claim(setup, "待っても終わらないとき", r"(\d+) 秒待っても終わらない", (wait,)),
        Claim(kb, "待つ上限", r"(?:待つ（|--wait-idle`。)最大 (\d+) 秒", (wait,)),
        Claim(req, "待つ上限", r"(?:待つ（|--wait-idle`。)最大 (\d+) 秒", (wait,)),
        Claim(spec, "待つ上限", r"既定・最大(?:とも)? (\d+) 秒", (wait,)),
        Claim(spec, "待つ上限（実機での確認）", r"（(\d+) 秒で終わらなければ 1）", (wait,)),
        Claim(setup, "放送の時間帯",
              r"準備を始める少し前（(\d+) 秒前）から、時報は再生開始の (\d+) 秒後、閉館放送は (\d+) 秒後まで",
              window),
        Claim(kb, "放送の時間帯",
              r"準備を始める (\d+) 秒前から、時報は再生開始の (\d+) 秒後、閉館放送は (\d+) 秒後まで",
              window),
        Claim(spec, "放送の時間帯",
              r"の (\d+) 秒前から、再生開始（`play_at`）の (\d+) 秒後（時報）・(\d+) 秒後（閉館放送",
              window),
        # --status の件数と、外のコマンドの待ち時間
        Claim(readme, "直近の放送の件数", r"`--status` で直近 (\d+) 件", (status.HISTORY_LIMIT,)),
        Claim(kb, "直近の放送と次の予定の件数", r"（新しい順に (\d+) 件まで）、「次の予定」（(\d+) 件）",
              (status.HISTORY_LIMIT, status.UPCOMING_LIMIT)),
        Claim(spec, "直近の放送と次の予定の件数", r"履歴の新しい順に (\d+) 件）・次の予定（(\d+) 件）",
              (status.HISTORY_LIMIT, status.UPCOMING_LIMIT)),
        Claim(kb, "無音の文言を挙げる数", r"（(\d+) 件まで。ほかは件数）", (status.SILENT_LIMIT,)),
        Claim(spec, "無音の文言を挙げる数", r"音にならなかった文言（(\d+) 件まで）", (status.SILENT_LIMIT,)),
        Claim(spec, "欠けた音源を挙げる数", r"欠けた音源（(\d+) 件まで）", (status.MISSING_LIMIT,)),
        Claim(spec, "外のコマンドの待ち時間", r"(\d+) 秒で打ち切", (status.COMMAND_TIMEOUT,)),
        # --check の一覧の件数
        Claim(setup, "足りない文言を挙げる数", r"先頭の (\d+) 件まで）", (LIST_LIMIT,)),
        Claim(setup, "情報を挙げる数", r"先頭の (\d+) 件のあとは", (INFO_LIMIT,)),
        Claim(kb, "足りない文言を挙げる数", r"先頭の (\d+) 件まで一覧が出る", (LIST_LIMIT,)),
        Claim(spec, "足りない文言を挙げる数", r"先頭の (\d+) 件を一覧", (LIST_LIMIT,)),
        Claim(spec, "情報を挙げる数", r"先頭の (\d+) 件まで。残りは件数だけ", (INFO_LIMIT,)),
        # 放送の履歴
        Claim(spec, "履歴の保存先", r"\| `state\.history_file` \| `([^`]+)` \|",
              (DEFAULT_CONFIG["state"]["history_file"],)),
        Claim(kb, "履歴の行数", r"(\d+) 行を超えると新しい (\d+) 行だけに書き直されます",
              (MAX_LINES, KEEP_LINES)),
        Claim(spec, "履歴の行数", r"(\d+) 行（`MAX_LINES`）を超えたら、新しい (\d+) 行（`KEEP_LINES`）",
              (MAX_LINES, KEEP_LINES)),
        # 作り置きの数え上げの上限（起動を遅くしないため）
        Claim(spec, "数え上げる文言の数の上限", r"既定は (\d+) 件（`MAX_COVERAGE_PHRASES`）",
              (phrases.MAX_COVERAGE_PHRASES,)),
        Claim(spec, "数え上げる気温の幅の上限", r"気温の幅は (\d+) 度分（`MAX_TEMP_SPAN`）まで",
              (phrases.MAX_TEMP_SPAN,)),
        # 常駐ループと起動時のログ
        Claim(spec, "予定を求められないときのやり直し", r"(\d+) 秒後にやり直す", (RETRY_SECONDS,)),
        Claim(spec, "起動時の警告に挙げる文言の数", r"件数と先頭 (\d+) 件の文言", (MISSING_PREVIEW,)),
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


# -- 道具（--check / --status / --wait-idle / update.sh）の文書 ----------------------

README_MD = "README.md"
SETUP_MD = "docs/SETUP.md"
KNOWLEDGE_BASE_MD = "docs/KNOWLEDGE_BASE.md"
SPECIFICATION_MD = "docs/SPECIFICATION.md"
REQUIREMENTS_MD = "docs/REQUIREMENTS.md"


def doc(name: str) -> str:
    """``REPO_ROOT`` からの相対パス ``name`` の文書を読む。"""
    return read_text(os.path.join(REPO_ROOT, name))


def section(text: str, start: str, end: str) -> str:
    """``start`` から、その後に最初に現れる ``end`` の手前まで（``start`` が無ければ空文字列）。"""
    first = text.find(start)
    if first < 0:
        return ""
    last = text.find(end, first + len(start))
    return text[first:] if last < 0 else text[first:last]


def fenced_blocks(text: str) -> List[str]:
    """``` で囲まれたコードブロックの中身を、書かれた順に返す。"""
    return re.findall(r"^```[^\n]*\n(.*?)^```", text, re.MULTILINE | re.DOTALL)


def without_fenced_blocks(text: str) -> str:
    return re.sub(r"^```.*?^```", "", text, flags=re.MULTILINE | re.DOTALL)


# -- コマンドのオプション ---------------------------------------------------------

_OPTION = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")

#: コマンドの終わり（バッククォート・パイプ・コメント・``&&``・``;``・閉じ括弧・行末）。
_COMMAND_END = re.compile(r"[`|#;)）」\n]|&&")


def options_after(text: str, program: str) -> List[Tuple[int, str]]:
    """``program`` のあとに書かれた ``--option`` を ``(行番号, オプション)`` で返す。

    オプションは、``program`` から同じ行のコマンドの終わりまでの間にあるものだけを拾う
    （``python3 campus_chime.py --check`` の `` ` `` のあとの散文は拾わない）。
    """
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        start = 0
        while True:
            index = line.find(program, start)
            if index < 0:
                break
            rest = line[index + len(program):]
            end = _COMMAND_END.search(rest)
            if end:
                rest = rest[:end.start()]
            found.extend((number, option) for option in _OPTION.findall(rest))
            start = index + len(program)
    return found


def cli_options() -> set:
    """``campus_chime.py`` の長いオプション（``--help`` を含む）。"""
    parser = cli.build_parser()
    return {option for action in parser._actions
            for option in action.option_strings if option.startswith("--")}


def shell_options(script: str) -> set:
    """シェルスクリプトの ``case`` が受ける長いオプション（行頭の ``--x)`` と ``-h|--x)``）。"""
    text = read_text(os.path.join(REPO_ROOT, "scripts", script))
    return set(re.findall(r"^\s+(?:-[a-z]\|)?(--[a-z][a-z-]*)\)", text, re.MULTILINE))


class OptionExtractionTest(unittest.TestCase):
    """検査そのものが、コマンドのオプションだけを拾うこと。"""

    def options(self, text, program="campus_chime.py"):
        return [option for _, option in options_after(text, program)]

    def test_options_after_the_program_are_found_in_order(self):
        self.assertEqual(
            self.options("python3 campus_chime.py --test-hourly 12 --backend pygame"),
            ["--test-hourly", "--backend"])

    def test_the_end_of_the_command_stops_the_search(self):
        for text in ("`python3 campus_chime.py --check` の「設定」は --foo を見る",
                     "python3 campus_chime.py --check   # --foo は別の話",
                     "python3 campus_chime.py --check && sudo systemctl restart --foo",
                     "python3 campus_chime.py --check | grep --foo",
                     "（python3 campus_chime.py --check）--foo"):
            with self.subTest(text=text):
                self.assertEqual(self.options(text), ["--check"])

    def test_a_line_without_the_program_has_no_options(self):
        self.assertEqual(self.options("journalctl -u campus_chime.service --since today"), [])

    def test_the_service_name_is_not_the_program(self):
        self.assertEqual(self.options("systemctl restart campus_chime.service --now"), [])

    def test_line_numbers_point_at_the_line(self):
        text = "説明\npython3 campus_chime.py --status\n"
        self.assertEqual(options_after(text, "campus_chime.py"), [(2, "--status")])

    def test_a_quoted_phrase_is_not_an_option(self):
        self.assertEqual(self.options('python3 campus_chime.py --say "正午をお知らせしたのだ。"'),
                         ["--say"])

    def test_the_options_of_the_cli_and_the_scripts_are_read(self):
        self.assertIn("--check", cli_options())
        self.assertIn("--help", cli_options())
        self.assertEqual(shell_options("setup.sh"), {"--help", "--no-apt", "--no-service"})
        self.assertEqual(shell_options("update.sh"), {"--help"})


class CliOptionsInDocsTest(unittest.TestCase):
    """コマンドラインオプションが文書に載っていて、文書に書いたオプションが実在すること。"""

    #: README の使い方の一覧（コードブロック）ではなく、そのあとの説明にまとめて書く共通のオプション。
    COMMON_OPTIONS = {"--config", "--backend", "--log-level"}

    def test_every_option_is_in_the_readme_usage(self):
        usage = section(doc(README_MD), "## 7. 使い方", "## 8.")
        self.assertTrue(usage, "README に 7 章がありません")
        listing = next(block for block in fenced_blocks(usage) if "--schedule" in block)
        missing = sorted(option for option in cli_options() - {"--help"} - self.COMMON_OPTIONS
                         if option not in listing)
        self.assertEqual(missing, [], "README の使い方の一覧（7 章）に載っていません")
        for option in self.COMMON_OPTIONS:
            self.assertIn(option, usage, "README の使い方（7 章）に共通のオプションの説明がありません")

    def test_every_option_is_a_row_of_the_specification_table(self):
        table = section(doc(SPECIFICATION_MD), "### 4.11", "### 4.12")
        self.assertTrue(table, "SPECIFICATION.md に 4.11 章がありません")
        missing = sorted(option for option in cli_options() - {"--help"}
                         if "| `{0}".format(option) not in table)
        self.assertEqual(missing, [], "SPECIFICATION.md 4.11 章の表に載っていません")

    def test_the_options_in_the_requirements_table_exist_and_include_the_new_ones(self):
        table = section(doc(REQUIREMENTS_MD), "### FR-09", "### FR-10")
        listed = set(re.findall(r"^\| `(--[a-z-]+)", table, re.MULTILINE))
        self.assertEqual(sorted(listed - cli_options()), [])
        for option in ("--check", "--status", "--wait-idle", "--version"):
            self.assertIn(option, listed, "REQUIREMENTS.md の FR-09 の表に載っていません")

    def test_the_options_written_after_the_commands_exist(self):
        programs = {"campus_chime.py": cli_options(),
                    "scripts/setup.sh": shell_options("setup.sh"),
                    "scripts/update.sh": shell_options("update.sh")}
        unknown = []
        for path in current_doc_paths():
            text = read_text(path)
            for program, valid in programs.items():
                for number, option in options_after(text, program):
                    if option not in valid:
                        unknown.append("{0}:{1}: {2} {3}".format(
                            os.path.relpath(path, REPO_ROOT), number, program, option))
        self.assertEqual(unknown, [], "存在しないオプションが書かれています（綴りの違い、または廃止）")


# -- ファイル・モジュールの一覧 ---------------------------------------------------------


def listed_files(directory: str, suffixes: Tuple[str, ...], prefix: str = "") -> List[str]:
    """``REPO_ROOT`` 内のフォルダにあるファイル名（``__init__.py`` と、フォルダは除く）。"""
    folder = os.path.join(REPO_ROOT, directory)
    return sorted(name for name in os.listdir(folder)
                  if name.startswith(prefix) and name.endswith(suffixes) and name != "__init__.py"
                  and os.path.isfile(os.path.join(folder, name)))


class FileListsInDocsTest(unittest.TestCase):
    """モジュール・スクリプト・テストファイルが、README と仕様書の一覧に載っていること。"""

    def setUp(self):
        self.readme = doc(README_MD)
        self.spec = doc(SPECIFICATION_MD)

    def test_the_readme_tree_lists_exactly_the_modules(self):
        tree = section(self.readme, "├── chime/", "├── assets/")
        self.assertEqual(sorted(re.findall(r"[├└]── (\w+\.py)", tree)),
                         listed_files("chime", (".py",)))

    def test_the_readme_tree_lists_exactly_the_scripts(self):
        tree = section(self.readme, "├── scripts/", "├── tests/")
        self.assertEqual(sorted(re.findall(r"[├└]── ([\w.]+\.(?:sh|py))", tree)),
                         listed_files("scripts", (".sh", ".py")))

    def test_the_specification_names_every_module(self):
        missing = [name for name in listed_files("chime", (".py",))
                   if "`chime/{0}`".format(name) not in self.spec]
        self.assertEqual(missing, [], "SPECIFICATION.md に `chime/<名前>` の記載がありません")

    def test_the_specification_names_every_script_in_its_script_section(self):
        scripts = section(self.spec, "### 4.19", "\n---\n")
        self.assertTrue(scripts, "SPECIFICATION.md に 4.19 章がありません")
        missing = [name for name in listed_files("scripts", (".sh", ".py"))
                   if "| `{0}` |".format(name) not in scripts]
        self.assertEqual(missing, [])

    def test_the_test_table_has_every_test_file_and_only_existing_ones(self):
        table = section(self.spec, "### 10.1", "### 10.2")
        listed = set(re.findall(r"`tests/(test_\w+\.py)`", table))
        self.assertEqual(sorted(listed), listed_files("tests", (".py",), prefix="test_"))


# -- 文書が指す節 -------------------------------------------------------------------

#: 見出しの番号（``## 9.``・``### 10-7.``・``### 4.11``）。
_HEADING_NUMBER = re.compile(r"^#{2,4}[ \t]+(\d+(?:[-.]\d+)*)\.?[ \t]", re.MULTILINE)

#: ``KNOWLEDGE_BASE.md 3-6``・``[SETUP.md](docs/SETUP.md) の「10-6.…``・``SETUP.md 9 章`` のように、
#: ほかの文書の名前に続けて書いた節の番号。
_QUALIFIED_REFERENCE = re.compile(
    r"(SETUP|SPECIFICATION|KNOWLEDGE_BASE|REQUIREMENTS)\.md"
    r"(?:\]\([^)]*\)|[`\])])*[ \t]*(?:の「)?(\d+(?:[-.]\d+)*)(?!\d)")

_CHAPTER_REFERENCE = re.compile(r"(\d+(?:\.\d+)?)[ \t]*章")
_SECTION_REFERENCE = re.compile(r"(?<![\w.#-])(\d+-\d+)(?![\w-])")

_DOC_FILES = {"SETUP": SETUP_MD, "SPECIFICATION": SPECIFICATION_MD,
              "KNOWLEDGE_BASE": KNOWLEDGE_BASE_MD, "REQUIREMENTS": REQUIREMENTS_MD}


def heading_numbers(text: str) -> set:
    return set(_HEADING_NUMBER.findall(text))


def dangling_references(name: str, text: str, headings: dict) -> List[str]:
    """文書 ``name`` が指している、実在しない節の番号（``headings`` は文書名 → 見出しの番号の集合）。"""
    problems = []
    for target, number in _QUALIFIED_REFERENCE.findall(text):
        if number not in headings[_DOC_FILES[target]]:
            problems.append("{0}: {1}.md の {2}".format(name, target, number))
    # 文書名のつかない番号は、その文書自身の節を指す。
    own = _QUALIFIED_REFERENCE.sub(" ", without_fenced_blocks(text))
    found = _CHAPTER_REFERENCE.findall(own)
    if name in (SETUP_MD, KNOWLEDGE_BASE_MD):
        found += _SECTION_REFERENCE.findall(own)
    problems.extend("{0}: {1}".format(name, number) for number in found
                    if number not in headings[name])
    return problems


class SectionReferenceCheckTest(unittest.TestCase):
    """検査そのものが、見出しの番号と、指している番号を取り違えないこと。"""

    HEADINGS = {name: set() for name in _DOC_FILES.values()}
    HEADINGS.update({KNOWLEDGE_BASE_MD: {"3", "3-6"}, SETUP_MD: {"9", "10", "10-7"},
                     SPECIFICATION_MD: {"3.4", "4.11"}, README_MD: {"9"}})

    def test_heading_numbers_of_every_style(self):
        text = ("# 題\n## 9. 更新\n### 10-7. 読み上げ\n### 4.11 `chime/cli.py`（責務）\n"
                "### 版のタグ\n#### FR-09 テスト\n本文 3-6 は見出しではない\n")
        self.assertEqual(heading_numbers(text), {"9", "10-7", "4.11"})

    def test_references_in_every_written_style_are_found(self):
        for text, target, number in (
                ("[docs/KNOWLEDGE_BASE.md](docs/KNOWLEDGE_BASE.md) 3-6", "KNOWLEDGE_BASE", "3-6"),
                ("[SETUP.md](docs/SETUP.md) の「10-6. WSL2 で」", "SETUP", "10-6"),
                ("`docs/SETUP.md` 9 章 B", "SETUP", "9"),
                ("[SETUP.md 10-7](SETUP.md#10-7-読み上げが無音になる)", "SETUP", "10-7"),
                ("SPECIFICATION.md 3.4 章", "SPECIFICATION", "3.4")):
            with self.subTest(text=text):
                self.assertEqual(_QUALIFIED_REFERENCE.findall(text), [(target, number)])

    def test_a_link_and_a_version_after_a_document_name_are_not_references(self):
        for text in ("| [docs/SETUP.md](docs/SETUP.md) | 再構築手順書 |",
                     "`docs/REQUIREMENTS.md` v6.0.0", "SETUP.md#10-7-読み上げ"):
            with self.subTest(text=text):
                self.assertEqual(_QUALIFIED_REFERENCE.findall(text), [])

    def test_a_missing_section_is_reported(self):
        text = "[KB](docs/KNOWLEDGE_BASE.md) 3-9 と SETUP.md 9 章 と 下の 8 章"
        self.assertEqual(
            dangling_references(README_MD, text, self.HEADINGS),
            ["README.md: KNOWLEDGE_BASE.md の 3-9", "README.md: 8"])

    def test_an_existing_section_is_not_reported(self):
        text = "KNOWLEDGE_BASE.md 3-6 と SETUP.md 10-7 と 下の 9 章"
        self.assertEqual(dangling_references(README_MD, text, self.HEADINGS), [])

    def test_the_numbers_without_a_document_name_point_at_the_document_itself(self):
        text = "（3-6）と（3-9）と 10 章と 11 章"
        self.assertCountEqual(
            dangling_references(KNOWLEDGE_BASE_MD, text, self.HEADINGS),
            ["{0}: 3-9".format(KNOWLEDGE_BASE_MD), "{0}: 10".format(KNOWLEDGE_BASE_MD),
             "{0}: 11".format(KNOWLEDGE_BASE_MD)])

    def test_dates_and_versions_in_code_blocks_are_not_section_numbers(self):
        text = "```\n時報 2026-08-27 10:00:00\n```\n本文\n"
        self.assertEqual(dangling_references(SETUP_MD, text, self.HEADINGS), [])


class SectionReferencesInDocsTest(unittest.TestCase):
    """文書が指す節（``KNOWLEDGE_BASE.md 3-6``・``4.14 章`` など）が実在すること。"""

    def test_every_referenced_section_exists(self):
        headings = {name: heading_numbers(doc(name)) for name in
                    (README_MD, SETUP_MD, KNOWLEDGE_BASE_MD, SPECIFICATION_MD, REQUIREMENTS_MD)}
        problems = []
        for path in current_doc_paths():
            name = os.path.relpath(path, REPO_ROOT)
            problems.extend(dangling_references(name, read_text(path), headings))
        self.assertEqual(problems, [], "実在しない節を指しています（見出しを変えたら参照も直す）")

    def test_the_sections_the_new_tools_refer_to_exist(self):
        # 検査が何も見ないまま通ってしまわないための確認
        self.assertIn("3-6", heading_numbers(doc(KNOWLEDGE_BASE_MD)))
        self.assertIn("4.19", heading_numbers(doc(SPECIFICATION_MD)))
        self.assertIn("9", heading_numbers(doc(SETUP_MD)))
        references = _QUALIFIED_REFERENCE.findall(doc(README_MD)) + \
            _QUALIFIED_REFERENCE.findall(doc(SETUP_MD))
        self.assertIn(("KNOWLEDGE_BASE", "3-6"), references)


# -- 出力の例 -----------------------------------------------------------------------

ZONE = ZoneInfo("Asia/Tokyo")

#: 例に載せる Pi の設置場所（テストでは一時フォルダの中身を、この表記に置き換えて比べる）。
EXAMPLE_HOME = "/home/pi/campus-chime"


def _merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _app_in(directory: str, **overrides) -> ChimeApp:
    """``directory`` の中だけに書く、音を出さない（mock）本物の ``ChimeApp``。

    読み上げのエンジンは作り置きだけにする（VOICEVOX ENGINE への通信をしない）。
    """
    data = copy.deepcopy(DEFAULT_CONFIG)
    _merge(data, overrides)
    data["state"] = {"file": os.path.join(directory, "state.json"),
                     "history_file": os.path.join(directory, "history.jsonl")}
    data["audio"]["mock_max_seconds"] = 0
    data["tts"]["engines"] = ["prerecorded"]
    return ChimeApp(Config(data, base_dir=BASE_DIR), backend="mock")


def _event(app: ChimeApp, day: date, key: str):
    return next(event for event in app.scheduler.events_for_date(day) if event.key == key)


def _broadcast(app: ChimeApp, event, plan: PlaybackPlan) -> None:
    """``ChimeApp.run_event`` の、再生して履歴に残す部分（組み立ては呼び出し側が済ませた）。"""
    outcome = app.play_with_result(plan)
    app._record_history(event, plan, outcome)


def real_history_lines(directory: str) -> List[str]:
    """本物の組み立て・再生・履歴の追記・読み出しを通した、「直近の放送」の行（新しい順）。

    KNOWLEDGE_BASE.md 3-6 の例は、手で書いた辞書ではなく、この実装そのものが記録する
    文面でなければならない（例: 必須の音源が再生の直前に消えたときの ``PlaybackError`` の
    文面、音源ファイルが無くて積めなかった部品の名前）。ファイルは ``directory`` の
    中だけに作り、その場所は ``EXAMPLE_HOME`` の表記に置き換えて返す。
    """
    home = os.path.join(directory, "home")
    assets = os.path.join(home, "assets")
    os.makedirs(assets)

    # 10/07 10:00 時報: 鳴らせるものが 1 つも無かった（任意の部品が、再生の直前に無い）
    app = _app_in(directory)
    event = _event(app, date(2026, 10, 7), "hourly:10")
    _broadcast(app, event, PlaybackPlan(event=event, segments=[
        Segment(os.path.join(directory, "none.wav"), label="読み上げ", optional=True)]))

    # 10/07 16:57 閉館放送: 組み立てのあと、再生の直前に蛍の光のファイルが無くなった
    music = os.path.join(assets, "hotaru.mp3")
    with open(music, "wb") as handle:
        handle.write(b"x")
    app = _app_in(directory, closing={"music_file": music})
    event = _event(app, date(2026, 10, 7), "closing")
    plan = app.builder.build_closing()
    os.remove(music)
    _broadcast(app, event, plan)

    # 10/08 12:00 時報: 作り置きの無い文が、その 1 文だけ無音になった
    voices = os.path.join(directory, "voice")
    os.makedirs(voices)
    noon = "正午をお知らせしたのだ。"
    manifest = load_manifest()
    shutil.copy(os.path.join(REPO_ROOT, "assets", "voice", manifest[noon]), voices)
    with open(os.path.join(voices, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump({noon: manifest[noon]}, handle, ensure_ascii=False)
    app = _app_in(directory, tts={"prerecorded_dir": voices})
    event = _event(app, date(2026, 10, 8), "hourly:12")
    _broadcast(app, event, app.builder.build_texts([noon, "今の大津の天気はくもりなのだ。"]))

    # 10/08 16:57 閉館放送: 閉館アナウンスの音源ファイルが無く、組み立てのときに省かれた
    app = _app_in(directory, closing={"announce_file": os.path.join(assets, "announce.wav")})
    event = _event(app, date(2026, 10, 8), "closing")
    _broadcast(app, event, app.builder.build_closing())

    # 10/09 10:00 時報: 組み立てに失敗して、最小の内容（時報音だけ）で鳴った。結果は成功のまま
    app = _app_in(directory, time_signal={"output_file": os.path.join(directory, "time_signal.wav")})
    event = _event(app, date(2026, 10, 9), "hourly:10")
    with mock.patch.object(app.builder, "build", side_effect=RuntimeError("組み立ての失敗")), \
            mock.patch("chime.app.logger"):
        plan = app._build_plan(event)
    _broadcast(app, event, plan)

    # 10/09 16:57 閉館放送: すべて鳴った
    app = _app_in(directory)
    event = _event(app, date(2026, 10, 9), "closing")
    _broadcast(app, event, app.builder.build_closing())

    entries = History(os.path.join(directory, "history.jsonl")).recent(status.HISTORY_LIMIT)
    return [status.describe_entry(entry, ZONE).replace(home, EXAMPLE_HOME) for entry in entries]


def example_status(config: Config) -> status.Status:
    """SETUP.md 6-2 の例と同じ、正常な状態（2026-08-26 20:07:15。サービスは動作中）。"""
    now = datetime(2026, 8, 26, 20, 7, 15, tzinfo=ZONE)
    scheduler = Scheduler(config.section("schedule"), ZONE,
                          timesignal.lead_seconds(config.section("time_signal")),
                          clock=lambda: now)
    return status.Status(
        version="campus-chime 6.1.0 (a1b2c3d)", now=now, ntp=True, active="active",
        enabled="enabled", backend="pygame", production=True, history=[],
        upcoming=scheduler.upcoming(limit=status.UPCOMING_LIMIT),
        coverage=prerecorded_coverage(config), zone=ZONE)


def block_starting_with(text: str, first_line: str) -> str:
    """``first_line`` で始まるコードブロックの中身（無ければ空文字列）。"""
    for block in fenced_blocks(text):
        if block.startswith(first_line):
            return block
    return ""


def pi_like_install(directory: str, config_data=None) -> Config:
    """``directory`` に、Pi の設置場所（正常な状態）を再現し、その設定を返す。

    同梱の音源・作り置き・ひとこと定義はリポジトリのものへのリンク、時報音はここで生成、
    ``cache/`` は空（サービスが一度動いたあとのように、書ける状態）、``config.json`` は
    ``config_data``（省略時は空の上書き）。``--check`` の出力例と同じ表示になる。
    """
    assets = os.path.join(directory, "assets")
    os.makedirs(os.path.join(assets, "generated"))
    os.makedirs(os.path.join(directory, "cache"))
    for name in ("announce.wav", "hotaru.mp3", "quotes.json", "voice"):
        os.symlink(os.path.join(REPO_ROOT, "assets", name), os.path.join(assets, name))
    settings = DEFAULT_CONFIG["time_signal"]
    timesignal.generate_time_signal(os.path.join(assets, "generated", "time_signal.wav"),
                                    settings, DEFAULT_CONFIG["audio"]["mixer"])
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(config_data or {}, handle, ensure_ascii=False)
    return chime_config.load_config(base_dir=directory)


def check_output(config: Config) -> List[str]:
    """``--check`` の出力の行（Pi の利用者 ``pi:pi`` で実行したことにする）。"""
    out = io.StringIO()
    check.run_check(config, out, euid=1000, account="pi:pi")
    return out.getvalue().replace(config.base_dir, EXAMPLE_HOME).splitlines()


class OutputExamplesTest(unittest.TestCase):
    """文書に載せた ``--status`` / ``--check`` の出力例が、実装の出力と同じ形であること。"""

    @classmethod
    def setUpClass(cls):
        cls.config = Config(DEFAULT_CONFIG, base_dir=BASE_DIR)
        cls.sample = example_status(cls.config)
        cls.rendered = status.render_status(cls.sample, status.attention(cls.sample))
        cls.setup = doc(SETUP_MD)
        cls.knowledge_base = doc(KNOWLEDGE_BASE_MD)

    def test_the_status_example_in_setup_is_what_status_prints(self):
        block = block_starting_with(self.setup, "campus-chime ")
        self.assertTrue(block, "SETUP.md 6-2 に --status の出力例がありません")
        self.assertEqual(block.rstrip("\n"), "\n".join(self.rendered))

    def test_the_healthy_example_has_no_reason_to_check(self):
        self.assertEqual(status.attention(self.sample), [])
        self.assertEqual(self.rendered[-1], "気になる点は見つかりませんでした。")

    def test_the_status_labels_are_the_rows_of_the_real_output(self):
        labels = []
        for line in self.rendered[2:]:
            if not line:
                break
            labels.append(re.split(r"\s{2,}", line, maxsplit=1)[0])
        self.assertEqual(labels, ["現在時刻", "時刻の同期", "サービス", "自動起動", "再生方法",
                                  "作り置きの音声"])
        for label in labels:
            self.assertIn("「{0}」".format(label), self.knowledge_base)

    def test_the_closing_line_of_each_tool_is_quoted_as_printed(self):
        ok = [Result(check.OK, "x")]
        self.assertEqual(summarize(ok), "結果: すべて OK です。")
        for name in (SETUP_MD, SPECIFICATION_MD):
            with self.subTest(doc=name):
                self.assertIn("結果: すべて OK です。", doc(name))
        for name in (README_MD, SETUP_MD, KNOWLEDGE_BASE_MD, SPECIFICATION_MD, REQUIREMENTS_MD):
            with self.subTest(doc=name):
                self.assertIn("気になる点は見つかりませんでした。", doc(name))

    def test_the_check_example_in_setup_is_what_check_prints(self):
        with tempfile.TemporaryDirectory() as directory:
            printed = check_output(pi_like_install(directory))
        block = block_starting_with(self.setup, "== 設定 ==")
        self.assertEqual(block.rstrip("\n").splitlines(), printed)

    def test_the_stale_config_example_in_setup_uses_lines_check_really_prints(self):
        # 旧版の既定値を丸ごと写した config.json（時刻アナウンスの文言が古い）。
        # 例は「情報」の行を省いて載せているので、載せた行がすべて実際の出力にあることを確かめる
        stale = copy.deepcopy(DEFAULT_CONFIG)
        stale["time_signal"]["announce_template"] = "{period}{hour}時をお知らせしました。"
        stale["time_signal"]["noon_template"] = "正午をお知らせしました。"
        with tempfile.TemporaryDirectory() as directory:
            printed = check_output(pi_like_install(directory, stale))
        blocks = [block for block in fenced_blocks(self.setup) if block.startswith("== 設定 ==")]
        self.assertEqual(len(blocks), 2, "SETUP.md に --check の出力例が 2 つ（6-2 と 10-7）ありません")
        omitted = "  （同じ形の「情報」が続く）"
        shown = [line for line in blocks[1].rstrip("\n").splitlines() if line != omitted]
        self.assertEqual([line for line in shown if line not in printed], [])
        self.assertIn(omitted, blocks[1])
        # 省いたのは「情報」の行だけ。NG の行（声の無い文言の一覧）は 1 行も省かない
        voices = printed[printed.index("== 作り置きの音声 =="):]
        self.assertEqual(voices[:voices.index("")], shown[shown.index("== 作り置きの音声 =="):])

    def test_the_check_example_follows_the_real_layout(self):
        headings = [heading for heading, _ in check.collect(self.config)]
        self.assertEqual(headings, ["設定", "作り置きの音声", "音源", "書き込み"])
        block = block_starting_with(self.setup, "== 設定 ==")
        self.assertTrue(block, "SETUP.md 6-2 に --check の出力例がありません")
        self.assertEqual(re.findall(r"^== (.+) ==$", block, re.MULTILINE), headings)
        self.assertEqual(block.rstrip("\n").splitlines()[-1], "結果: すべて OK です。")
        for line in block.splitlines():
            if re.match(r"^  \S", line):   # 項目の行（続きの行は、もっと深く字下げされる）
                self.assertRegex(line, r"^  (?:OK  |NG  |情報|警告)  \S", line)
        for heading in headings:
            self.assertIn("「{0}」".format(heading), self.knowledge_base)

    def test_the_marks_of_a_check_line_are_the_ones_the_docs_explain(self):
        marks = []
        for level in (check.OK, check.INFO, check.WARNING, check.NG):
            lines = render_section("見出し", [Result(level, "項目")])
            marks.append(lines[1].split()[0])
        self.assertEqual(marks, ["OK", "情報", "警告", "NG"])
        self.assertIn("`OK`・`情報`・`警告`・`NG`", self.knowledge_base)

    def test_the_history_example_is_what_status_prints(self):
        # 実装に放送させて、履歴に記録された行を読み出し、--status が表示する行と比べる
        with tempfile.TemporaryDirectory() as directory:
            lines = real_history_lines(directory)
        self.assertEqual(len(lines), 6)
        self.assertIn("\n".join(lines), self.knowledge_base)

    def test_the_result_words_of_a_history_line_are_explained(self):
        words = []
        for result in ("ok", "partial", "failed", "error"):
            line = status.describe_entry(
                {"kind": "hourly", "result": result, "at": "2026-10-08T12:00:00+09:00"}, ZONE)
            words.append(line.split()[3])
        self.assertEqual(words, ["成功", "一部のみ", "失敗", "エラー"])
        for word in words:
            self.assertIn("| {0} |".format(word), self.knowledge_base)

    def test_the_reasons_to_check_are_explained(self):
        def reasons(**overrides):
            values = dict(vars(self.sample))
            values.update(overrides)
            return status.attention(status.Status(**values))

        missing = type(self.sample.coverage)(total=2, missing=["x"], by_kind={})
        # 音源ファイルが無くて積めなかった部品がある放送は、結果が一部のみでも要確認にする
        dropped = dict(history=[{"result": "partial", "missing": ["閉館アナウンス"]}])
        # 内容を組み立てられず、最小のプランで鳴った放送は、結果が成功でも要確認にする
        degraded = dict(history=[{"result": "ok", "degraded": True}])
        # tts.engines に prerecorded が無いと、声がそろっていても Pi は無音になる
        silent = dict(prerecorded_enabled=False)
        cases = (dict(ntp=False), dict(active="inactive"), dict(backend="mock"),
                 dict(history=[{"result": "failed"}]), dropped, degraded, silent,
                 dict(coverage=missing))
        for overrides in cases:
            reason = reasons(**overrides)
            self.assertEqual(len(reason), 1, overrides)
            # 「（」の前までが、文書の表に載っている言い方
            stated = re.split(r"（|\d+ 件", reason[0])[0]
            with self.subTest(reason=reason[0]):
                self.assertIn(stated, self.knowledge_base)

    def test_the_version_examples_have_the_shape_of_the_real_output(self):
        shape = re.compile(r"campus-chime \d+\.\d+\.\d+ \((?:[0-9a-f]{7}|unknown)\)")
        with tempfile.TemporaryDirectory() as empty:
            self.assertRegex(buildinfo.version_string(empty), shape)
        examples = []
        for name in (README_MD, SETUP_MD, KNOWLEDGE_BASE_MD, SPECIFICATION_MD, REQUIREMENTS_MD):
            examples.extend(re.findall(r"campus-chime \d+\.\d+\.\d+ \([^)]*\)", doc(name)))
        self.assertTrue(examples)
        for example in examples:
            self.assertRegex(example, shape)


# -- 置き換えた手作業の確認 ---------------------------------------------------------

#: ``--check`` / ``--status`` で置き換えた、手作業の確認。文書に戻っていないこと。
REPLACED_MANUAL_CHECKS = (
    (re.compile(r"ls\s+assets/voice/\*\.wav\s*\|\s*wc\s+-l"),
     "作り置きの数え上げは --check の「作り置きの音声」"),
    (re.compile(r"grep\s+-c\s+\S+\s+config\.json"),
     "古い config.json の判定は --check の「設定」と「作り置きの音声」"),
    (re.compile(r"python3?\s+-c\s+[\"']import\s+json"),
     "設定値を読み出す python -c ではなく --check"),
    (re.compile(r"journalctl[^\n]*\|\s*grep\s+(?:廃止|既定値|合成できませんでした)"),
     "journalctl の grep ではなく --check（設定）と --status（直近の放送）"),
)


def manual_checks(text: str) -> List[Tuple[int, str]]:
    """``text`` にある、置き換えたはずの手作業の確認を ``(行番号, その行)`` で返す。"""
    return [(number, line.strip()) for number, line in enumerate(text.splitlines(), start=1)
            if any(pattern.search(line) for pattern, _ in REPLACED_MANUAL_CHECKS)]


class ManualCheckDetectionTest(unittest.TestCase):
    """検査そのものが、置き換えた手作業の確認を見つけ、ほかのコマンドは通すこと。"""

    def test_each_replaced_check_is_found(self):
        for line in ("ls assets/voice/*.wav | wc -l",
                     'grep -c "お知らせしました" config.json',
                     "python3 campus_chime.py --print-config | python3 -c \"import json,sys; x\"",
                     'journalctl -u campus_chime.service -n 50 | grep 既定値',
                     'journalctl -u campus_chime.service --since "5 min ago" | grep 廃止',
                     "journalctl -u campus_chime.service -n 50 --no-pager | grep 合成できませんでした"):
            with self.subTest(line=line):
                self.assertEqual(len(manual_checks(line)), 1)

    def test_other_commands_are_not_found(self):
        text = ("journalctl -u campus_chime.service -f\n"
                "journalctl -u campus_chime.service -p err --since -7d\n"
                "grep dtparam=audio /boot/firmware/config.txt\n"
                "python3 campus_chime.py --check\n")
        self.assertEqual(manual_checks(text), [])

    def test_line_numbers_point_at_the_line(self):
        self.assertEqual(manual_checks("説明\nls assets/voice/*.wav | wc -l\n"),
                         [(2, "ls assets/voice/*.wav | wc -l")])


class ManualChecksInDocsTest(unittest.TestCase):
    """手作業の確認を ``--check`` / ``--status`` に置き換えたままであること。"""

    def test_the_replaced_checks_are_not_in_the_docs(self):
        found = []
        for path in current_doc_paths():
            for number, line in manual_checks(read_text(path)):
                found.append("{0}:{1}: {2}".format(os.path.relpath(path, REPO_ROOT), number, line))
        self.assertEqual(found, [], "手作業の確認が戻っています（--check / --status を使う）")

    def test_the_docs_point_at_the_tools_instead(self):
        for name in (README_MD, SETUP_MD, KNOWLEDGE_BASE_MD):
            text = doc(name)
            for needle in ("--check", "--status", "bash scripts/update.sh"):
                with self.subTest(doc=name, needle=needle):
                    self.assertIn(needle, text)


class DocTestCase(unittest.TestCase):
    """文書に記述があることを確かめるテストの土台（失敗の表示に文書全体を出さない）。"""

    def assertInDoc(self, needle: str, text: str, name: str) -> None:
        if needle not in text:
            self.fail("{0} に次の記述がありません:\n{1}".format(name, needle))

    def assertNotInDoc(self, needle: str, text: str, name: str) -> None:
        if needle in text:
            self.fail("{0} に、書いてはいけない次の記述があります:\n{1}".format(name, needle))


#: 手で更新する 2 つの手順の目印（README・SETUP・KNOWLEDGE_BASE が、この言い方で書く）。
FIRST_UPDATE_MARK = "初めて更新するとき"
STOPPED_UPDATE_MARK = "途中で止まったとき"


def manual_procedures(text: str) -> Tuple[str, str]:
    """手で更新する 2 つの手順の本文を ``(初めての更新, update.sh が止まったとき)`` で返す。

    1 つ目は、``update.sh`` も ``--wait-idle`` もまだ無い版（v6.1.0 より前）から初めて更新する手順。
    2 つ目は、``update.sh`` が途中で止まって手で進める手順（すでに新しい版のコードが手元にある）。
    1 つ目は 2 つ目の目印の手前まで、2 つ目は文書の終わりまで。目印が無ければ空文字列。
    """
    first = text.find(FIRST_UPDATE_MARK)
    stopped = text.find(STOPPED_UPDATE_MARK, first + 1) if first >= 0 else -1
    if first < 0 or stopped < 0:
        return "", ""
    return text[first:stopped], text[stopped:]


class ManualProceduresTest(DocTestCase):
    """検査そのものが、2 つの手順を取り分けること。"""

    TEXT = ("前置き\n**v6.1.0 より前から初めて更新するとき**\n```\ngit pull\n```\n"
            "**update.sh が途中で止まったとき**\n```\npython3 campus_chime.py --wait-idle\n```\n")

    def test_the_two_procedures_are_split_at_the_second_mark(self):
        first, second = manual_procedures(self.TEXT)
        self.assertIn("git pull", first)
        self.assertNotIn("--wait-idle", first)
        self.assertIn("--wait-idle", second)

    def test_a_document_without_the_marks_has_no_procedures(self):
        self.assertEqual(manual_procedures("手順なし"), ("", ""))
        self.assertEqual(manual_procedures("初めて更新するとき だけ"), ("", ""))


class UpdateProcedureInDocsTest(DocTestCase):
    """更新の手順が ``update.sh`` で、手で更新する予備の手順が実在する版の動きに合っていること。"""

    UPDATE_SH = os.path.join(REPO_ROOT, "scripts", "update.sh")
    SETUP_SH = os.path.join(REPO_ROOT, "scripts", "setup.sh")

    def test_the_script_the_docs_name_exists(self):
        self.assertTrue(os.path.isfile(self.UPDATE_SH))

    def test_the_first_update_from_before_6_1_0_does_not_use_wait_idle(self):
        # --wait-idle は v6.1.0 で入った。それより前の版で実行すると
        # 「unrecognized arguments」で終了コード 2 になり、手順の最初で止まる
        for name in (README_MD, SETUP_MD, KNOWLEDGE_BASE_MD):
            with self.subTest(doc=name):
                first, _ = manual_procedures(doc(name))
                self.assertTrue(first, "{0} に「{1}」の手順がありません".format(name, FIRST_UPDATE_MARK))
                blocks = "\n".join(fenced_blocks(first))
                self.assertNotInDoc("--wait-idle", blocks, name)
                self.assertInDoc("git pull", blocks, name)
                self.assertInDoc("bash scripts/setup.sh --no-apt", blocks, name)
                self.assertLess(blocks.index("git pull"), blocks.index("bash scripts/setup.sh --no-apt"),
                                "{0}: git pull は setup.sh より先".format(name))
                # --wait-idle で待てないので、放送の時間帯を避けて実行することを伝える
                self.assertInDoc("16:55〜17:02", first, name)
                # 以降は update.sh で更新できる
                self.assertInDoc("bash scripts/update.sh", first, name)

    def test_the_procedure_for_a_stopped_update_waits_before_setup(self):
        # update.sh が止まったとき、コードはもう新しい版（--wait-idle がある）。
        # 放送の時間帯を待ってから setup.sh を実行する
        for name in (README_MD, SETUP_MD, KNOWLEDGE_BASE_MD):
            with self.subTest(doc=name):
                _, stopped = manual_procedures(doc(name))
                self.assertTrue(stopped, "{0} に「{1}」の手順がありません".format(name, STOPPED_UPDATE_MARK))
                commands = []
                for block in fenced_blocks(stopped):
                    commands.append(block)
                    if "bash scripts/setup.sh --no-apt" in block:
                        break
                joined = "\n".join(commands)
                self.assertInDoc("python3 campus_chime.py --wait-idle", joined, name)
                self.assertLess(joined.index("--wait-idle"), joined.index("bash scripts/setup.sh --no-apt"))
                # もう一度 update.sh を実行すれば続きから反映する（記録で判断する）こと
                self.assertInDoc("bash scripts/update.sh", stopped, name)

    def test_a_manual_restart_still_waits_for_the_broadcast(self):
        for name in (README_MD, SETUP_MD, KNOWLEDGE_BASE_MD):
            with self.subTest(doc=name):
                self.assertInDoc("sudo systemctl restart campus_chime.service", doc(name), name)
        self.assertInDoc("python3 campus_chime.py --wait-idle && sudo systemctl restart campus_chime.service",
                         doc(KNOWLEDGE_BASE_MD), KNOWLEDGE_BASE_MD)

    def test_the_stopping_reasons_in_setup_match_the_script(self):
        script = read_text(self.UPDATE_SH)
        table = section(doc(SETUP_MD), "| 止まる理由 | 対処 |", "\n\n")
        for reason, needle in (("root", "id -u"), ("書き換わっている", "git status --porcelain"),
                               ("持ち主が違う", "status.err"),
                               ("detached HEAD", "git symbolic-ref"), ("`git pull` できない", "--ff-only"),
                               ("360 秒待っても", "--wait-idle"),
                               ("`config.json` を読めない", "wait_status}\" -eq 2"),
                               ("`setup.sh` が失敗", "setup.sh\" --no-apt")):
            with self.subTest(reason=reason):
                self.assertIn(reason, table)
                self.assertIn(needle, script)

    def test_the_restore_advice_is_the_command_the_script_prints(self):
        # git checkout -- <ファイル> は、git add 済みの変更を戻せない（案内どおりに実行しても、
        # 次の実行で同じ理由でまた断られる）。スクリプトが案内する、HEAD の内容に戻す形を載せる
        command = "git restore --source=HEAD --staged --worktree --"
        self.assertIn(command, read_text(self.UPDATE_SH))
        table = section(doc(SETUP_MD), "| 止まる理由 | 対処 |", "\n\n")
        self.assertInDoc(command, table, SETUP_MD)
        self.assertNotInDoc("表示された `git checkout -- <ファイル>` で", table, SETUP_MD)

    def test_the_record_of_the_deployed_version_is_the_one_the_scripts_use(self):
        # update.sh は cache/deployed_commit（setup.sh が再起動に成功したときに書く）を見て、
        # すでに最新版でも「サービスが古い版のまま」を見分ける
        recorded = re.search(r'DEPLOYED_FILE="\$\{REPO_DIR\}/([\w/.]+)"', read_text(self.UPDATE_SH))
        self.assertIsNotNone(recorded)
        path = recorded.group(1)
        self.assertEqual(path, "cache/deployed_commit")
        self.assertIn("cache/deployed_commit", read_text(self.SETUP_SH))
        for name in (SPECIFICATION_MD, SETUP_MD, KNOWLEDGE_BASE_MD):
            self.assertInDoc(path, doc(name), name)

    def test_an_up_to_date_checkout_is_not_called_finished_when_the_service_is_old(self):
        for name in (SPECIFICATION_MD, SETUP_MD):
            with self.subTest(doc=name):
                self.assertInDoc("すでに最新版でも", doc(name), name)

    def test_setup_exits_with_1_when_it_withholds_the_restart(self):
        self.assertRegex(read_text(self.SETUP_SH), r'restart_withheld[^\n]*-eq 1[\s\S]*exit 1')
        steps = section(doc(SETUP_MD), "## 5. 本体を導入する", "## 6.")
        self.assertInDoc("終了コード 1", steps, SETUP_MD)
        self.assertInDoc("終了コード 1", section(doc(SPECIFICATION_MD), "### 4.19", "\n---\n"), SPECIFICATION_MD)


class TagDocsTest(unittest.TestCase):
    """版のタグの説明（人が付ける 2 つの版と、そのコマンド）が実装と食い違わないこと。"""

    @classmethod
    def setUpClass(cls):
        scripts = os.path.join(REPO_ROOT, "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import tag_releases
        cls.tag_releases = tag_releases

    def test_the_two_old_tags_and_the_reason_are_documented(self):
        for name in (README_MD, SPECIFICATION_MD):
            text = doc(name)
            for needle in ("v5.2.0", "v5.3.0", "GITHUB_TOKEN", "workflows"):
                with self.subTest(doc=name, needle=needle):
                    self.assertIn(needle, text)

    def test_the_command_in_the_docs_is_the_one_the_warning_prints(self):
        with tempfile.TemporaryDirectory() as not_a_repository:
            message = self.tag_releases._workflows_message(
                not_a_repository, "origin", "v5.2.0", "reason")
        head = "git fetch origin && git tag -a v5.2.0 "
        tail = " -m v5.2.0 && git push origin v5.2.0"
        self.assertIn(head, message)
        self.assertIn(tail, message)
        for name in (README_MD, SPECIFICATION_MD):
            text = doc(name)
            with self.subTest(doc=name):
                self.assertIn(head, text)
                self.assertIn(tail, text)

    def test_the_old_tag_exception_is_not_described_as_automatic(self):
        # 「手でタグを作る必要はない」と言い切ると、v5.2.0 / v5.3.0 を付け忘れる
        for name in (README_MD, SPECIFICATION_MD):
            with self.subTest(doc=name):
                self.assertNotIn("手でタグを作る必要はありません", doc(name))
                self.assertNotIn("手でタグを作る必要はなく、", doc(name))


# -- 表の形 -----------------------------------------------------------------------

def split_cells(line: str) -> List[str]:
    r"""表の 1 行を、セルに分ける（``\|`` はセルの区切りではない）。"""
    parts = re.split(r"(?<!\\)\|", line.strip())
    return [cell.strip() for cell in parts[1:-1]]


def table_blocks(text: str) -> List[List[Tuple[int, str]]]:
    """コードブロックの外にある表を、``(行番号, 行)`` の並びで返す（`|` で始まる連続した行）。"""
    blocks: List[List[Tuple[int, str]]] = []
    current: List[Tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.lstrip().startswith("|"):
            current.append((number, line))
            continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def ragged_rows(text: str) -> List[Tuple[int, int, int]]:
    """見出し行とセルの数が違う表の行を ``(行番号, セルの数, 見出しのセルの数)`` で返す。

    GFM は、見出しより多いセルを表示しない。2 つの行が改行なしでつながると、後ろの行が
    前の行の続きのセルになり、表示から消える。
    """
    found = []
    for block in table_blocks(text):
        expected = len(split_cells(block[0][1]))
        found.extend((number, len(split_cells(line)), expected) for number, line in block
                     if len(split_cells(line)) != expected)
    return found


class RaggedRowDetectionTest(unittest.TestCase):
    """検査そのものが、つながってしまった行を見つけ、正しい表は通すこと。"""

    def test_two_rows_glued_into_one_line_are_found(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 || 3 | 4 |\n| 5 | 6 |\n"
        self.assertEqual(ragged_rows(text), [(3, 5, 2)])

    def test_a_well_formed_table_and_an_escaped_pipe_are_accepted(self):
        text = "| a | b |\n|---|---|\n| `x \\| y` | 2 |\n\n文\n\n| c |\n|---|\n| 1 |\n"
        self.assertEqual(ragged_rows(text), [])

    def test_tables_inside_code_blocks_are_ignored(self):
        text = "```\n| a | b |\n| 1 | 2 || 3 |\n```\n"
        self.assertEqual(ragged_rows(text), [])


class TablesInDocsTest(unittest.TestCase):
    """どの表の行も、見出しと同じ数のセルを持つこと（表示から消える行を作らない）。"""

    def test_no_row_has_a_different_number_of_cells_than_its_header(self):
        problems = []
        for path in current_doc_paths():
            for number, cells, expected in ragged_rows(read_text(path)):
                problems.append("{0}:{1}: セルが {2} 個（見出しは {3} 個）".format(
                    os.path.relpath(path, REPO_ROOT), number, cells, expected))
        self.assertEqual(problems, [], "改行が抜けて行がつながっています")


# -- CI の書き方 ------------------------------------------------------------------

class CiWordingInDocsTest(DocTestCase):
    """CI の ``bash -n`` の説明が、ci.yml の書き方（1 本ずつ）と合っていること。"""

    CI_YML = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")

    def test_ci_runs_bash_n_once_per_script(self):
        # bash -n a.sh b.sh は a.sh しか調べない（b.sh は引数として渡されるだけ）
        ci = read_text(self.CI_YML)
        self.assertIn('for f in scripts/*.sh; do', ci)
        self.assertIn('bash -n "$f"', ci)
        self.assertIn("shellcheck scripts/*.sh", ci)
        self.assertIsNone(re.search(r"bash -n [^\n]*\.sh[^\n]*\.sh", ci),
                          "1 回の bash -n に複数のファイルを渡さない")

    def test_the_docs_say_every_script_is_checked_one_by_one(self):
        for name in (SPECIFICATION_MD, REQUIREMENTS_MD):
            with self.subTest(doc=name):
                text = doc(name)
                self.assertInDoc("scripts/*.sh", text, name)
                self.assertInDoc("1 本ずつ", text, name)
                self.assertInDoc("bash -n", text, name)
        # 2 本を並べて 1 回の bash -n で調べるかのような書き方は残さない
        self.assertNotInDoc("`scripts/setup.sh` と `scripts/update.sh` の構文（`bash -n`）",
                            doc(SPECIFICATION_MD), SPECIFICATION_MD)
        self.assertNotInDoc("シェルスクリプト（`scripts/setup.sh`・`scripts/update.sh`）を CI で",
                            doc(REQUIREMENTS_MD), REQUIREMENTS_MD)


# -- 文面 ------------------------------------------------------------------------

class WordingInDocsTest(DocTestCase):
    """読み違えやすい書き方を直したままであること。"""

    def test_six_minutes_is_the_update_wait_not_the_length_of_the_broadcast(self):
        # 閉館放送そのものは約 2.5 分（announce.wav 約 4 秒 + hotaru.mp3 約 139 秒）。
        # 最大約 6 分は、update.sh が放送の時間帯を待つ上限（--wait-idle の上限）
        minutes = status.WAIT_IDLE_MAX // 60
        self.assertEqual(minutes * 60, status.WAIT_IDLE_MAX)
        for name in (README_MD, SETUP_MD):
            with self.subTest(doc=name):
                text = doc(name)
                found, wrong = check_claim(text, r"`update\.sh` は最大約 (\d+) 分", (minutes,))
                self.assertEqual((found, wrong), (1, []), "{0}: update.sh が待つ最大の時間の記述".format(name))
                self.assertIsNone(re.search(r"閉館放送は[^。\n]*約 \d+ 分", text),
                                  "閉館放送そのものの長さと読める書き方")

    def test_print_config_does_not_show_the_removed_keys(self):
        # 廃止したキーは読み込むときに捨てられるので、--print-config には出ない。
        # 知らないキー（綴りの間違い）は、そのまま出る
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"extra_segment": {"mode": "choice"}, "typo_key": 1}, handle)
            shown = chime_config.load_config(path).data
        self.assertNotIn("mode", shown["extra_segment"])
        self.assertEqual(shown["typo_key"], 1)
        # README は使い方の一覧の 1 行、仕様書は 4.11 章の表の 1 行
        for name, start in ((README_MD, "python3 campus_chime.py --print-config"),
                            (SPECIFICATION_MD, "| `--print-config` |")):
            with self.subTest(doc=name):
                lines = [line for line in doc(name).splitlines() if line.startswith(start)]
                self.assertEqual(len(lines), 1, "{0} に --print-config の説明がありません".format(name))
                self.assertIn("書かれたとおり", lines[0])
                self.assertIn("廃止", lines[0])


# -- 放送の履歴 --------------------------------------------------------------------

class HistoryEntryInDocsTest(DocTestCase):
    """履歴の 1 行の項目が、仕様書の表に全部載っていること。"""

    def test_every_field_of_an_entry_is_a_row_of_the_specification_table(self):
        entry = make_entry(at=datetime(2026, 10, 8, 12, 0, tzinfo=ZONE), key="hourly:12",
                           kind="hourly", played=1, total=1, error="x")
        table = section(doc(SPECIFICATION_MD), "### 4.16", "### 4.17")
        for field in entry:
            with self.subTest(field=field):
                self.assertInDoc("`{0}`".format(field), table, SPECIFICATION_MD)

    def test_a_part_dropped_at_build_time_makes_the_result_partial(self):
        # played == total でも、必須の部品（音源ファイル）が積めなかったなら「すべて鳴った」ではない
        entry = make_entry(at=datetime(2026, 10, 8, 16, 57, tzinfo=ZONE), key="closing",
                           kind="closing", played=1, total=1, missing=["閉館アナウンス"])
        self.assertEqual(entry["result"], "partial")
        self.assertEqual(entry["missing"], ["閉館アナウンス"])
        self.assertEqual(make_entry(at=datetime(2026, 10, 8, 16, 57, tzinfo=ZONE), key="closing",
                                    kind="closing", played=2, total=2)["result"], "ok")
        table = section(doc(SPECIFICATION_MD), "### 4.16", "### 4.17")
        self.assertInDoc("`missing`", table, SPECIFICATION_MD)
        kb = section(doc(KNOWLEDGE_BASE_MD), "### 3-6.", "\n---\n")
        self.assertInDoc("欠けた音源:", kb, KNOWLEDGE_BASE_MD)

    def test_status_shows_the_missing_parts_of_the_last_broadcast(self):
        line = status.describe_entry(
            {"kind": "closing", "result": "partial", "at": "2026-10-08T16:57:00+09:00",
             "missing": ["閉館アナウンス"]}, ZONE)
        self.assertTrue(line.endswith("欠けた音源: 「閉館アナウンス」"), line)
        self.assertInDoc("欠けた音源", section(doc(SPECIFICATION_MD), "### 4.15", "### 4.16"),
                         SPECIFICATION_MD)


# -- 設定の検査（3.5 章） ----------------------------------------------------------------

def config_paths(default=DEFAULT_CONFIG, prefix: str = "") -> List[str]:
    """既定設定の全キーを、ドット区切りのパスで列挙する（自由形式の節の中身は含めない）。"""
    free_form = {"time_signal.hour_readings", "audio.commands"}
    paths = []
    for key, value in default.items():
        path = prefix + key
        paths.append(path)
        if isinstance(value, dict) and path not in free_form:
            paths.extend(config_paths(value, path + "."))
    return paths


#: 実装に入れて、どの重大度になるかを調べる値。型の違う値・範囲外の値・巨大な値・書き方の違い。
PROBE_VALUES = (None, True, 0, -1, 1, 24, 10 ** 30, 1.5, float("nan"), "abc", "9", "LOUD",
                "%(foo)s", [], ["x"], [[1]], ["a"], [0, 7], {}, {"a": 1}, 5)


def set_at(data: dict, key: str, value) -> None:
    *parents, leaf = key.split(".")
    node = data
    for part in parents:
        node = node[part]
    node[leaf] = value


def probed_levels() -> Tuple[set, set]:
    """全キーに ``PROBE_VALUES`` を 1 つずつ入れて ``validate`` にかけ、error・warning が付いたキーの集合を返す。

    再生方式が ``mock`` のときの結果も合わせる（外部コマンドの表 ``audio.commands`` は、
    表を読む再生方式のときだけ error になり、読まない間は warning にとどまる）。
    """
    errors, warnings = set(), set()
    for backend in (DEFAULT_CONFIG["audio"]["backend"], "mock"):
        for key in config_paths():
            for value in PROBE_VALUES:
                data = copy.deepcopy(DEFAULT_CONFIG)
                data["audio"]["backend"] = backend
                set_at(data, key, copy.deepcopy(value))
                for finding in configcheck.validate(Config(data, base_dir=BASE_DIR)):
                    (errors if finding.level == configcheck.ERROR else warnings).add(finding.key)
    return errors, warnings


def rule_table(spec: str) -> List[List[str]]:
    """仕様書 3.5 章の、規則の表（``対象 | error | warning``）の行（見出しと区切りを除く）。"""
    body = section(spec, "### 3.5", "\n## 4.")
    for block in table_blocks(body):
        header = split_cells(block[0][1])
        if header[:1] == ["対象"] and len(header) == 3:
            return [split_cells(line) for _, line in block[2:]]
    return []


def keys_in(cell: str) -> set:
    """セルの中でバッククォートに囲んだ、既定設定にあるキー。"""
    known = set(config_paths())
    return {token for token in re.findall(r"`([a-z_]+(?:\.[a-z_]+)*)`", cell) if token in known}


class ConfigCheckPolicyInDocsTest(DocTestCase):
    """仕様書 3.5 章の「どの値を置き換え、どの値を警告にとどめるか」が、実装の動きと一致すること。"""

    @classmethod
    def setUpClass(cls):
        cls.spec = doc(SPECIFICATION_MD)
        cls.rows = rule_table(cls.spec)
        cls.errors, cls.warnings = probed_levels()

    def test_the_table_exists_with_a_row_for_each_kind_of_rule(self):
        self.assertGreaterEqual(len(self.rows), 6)
        for row in self.rows:
            self.assertEqual(len(row), 3)

    def test_the_keys_in_the_error_column_are_exactly_the_keys_that_can_be_errors(self):
        # 置き換わるキーの一覧は、実装と過不足なく一致させる（書き漏れると、置き換えられて
        # 驚く。書きすぎると、警告で済むはずの値を直さずに済ませる）
        documented = set()
        for row in self.rows:
            documented |= keys_in(row[1])
        self.assertEqual(sorted(documented - self.errors), [], "文書は error と書いているが、実装は error にしない")
        self.assertEqual(sorted(self.errors - documented), [], "実装は error にするが、文書に書かれていない")

    def test_the_keys_in_the_warning_column_can_really_be_warnings(self):
        documented = set()
        for row in self.rows:
            documented |= keys_in(row[2])
        self.assertTrue(documented)
        self.assertEqual(sorted(documented - self.warnings), [])

    def test_values_the_runtime_tolerates_are_kept_as_written(self):
        # 前の版が例外なく動かしていた設定は、警告にとどめて、値を置き換えない。
        # 仕様書 3.5 章は、次の例を挙げる
        tolerated = (
            ("schedule.hourly.weekdays", [0, 1, 2, 3, 4, 5, 7]),   # 土曜は鳴り続ける
            ("schedule.hourly.skip_hours", [12, 24]),              # 休みにした 12 時は、休みのまま
            ("schedule.hourly.start_hour", "9"),                   # 数値の書き方の違い
            ("schedule.closing.minute", 55.0),
            ("schedule.hourly.end_hour", 24),                      # 範囲外の時刻は鳴らさないだけ
            ("schedule.hourly", None),                             # 時報を null にしたら、鳴らない
            ("weather.open_meteo.locations", [{"label": "x", "latitude": 99, "longitude": 0}]),
            ("time_signal.announce_template", "{foo}時"),          # 既定の文言に切り替わる
        )
        for key, value in tolerated:
            with self.subTest(key=key):
                data = copy.deepcopy(DEFAULT_CONFIG)
                set_at(data, key, value)
                config = Config(data, base_dir=BASE_DIR)
                fixed, errors = configcheck.sanitized(config)
                self.assertEqual(errors, [])
                self.assertEqual(fixed.data, config.data)
                self.assertTrue(configcheck.validate(config), "警告は出る")
                self.assertInDoc("`{0}`".format(key), section(self.spec, "### 3.5", "\n## 4."), SPECIFICATION_MD)

    def test_only_the_broken_elements_of_a_list_are_removed(self):
        broken = (
            ("schedule.hourly.skip_hours", [12, "x"], [12]),
            ("schedule.hourly.weekdays", [0, [1], 2], [0, 2]),
            ("schedule.closing.weekdays", [4, {"a": 1}], [4]),
            ("audio.commands", {"wav": ["aplay", "{path}"], "mp3": 5}, {"wav": ["aplay", "{path}"]}),
        )
        for key, value, expected in broken:
            with self.subTest(key=key):
                data = copy.deepcopy(DEFAULT_CONFIG)
                set_at(data, key, value)
                fixed, errors = configcheck.sanitized(Config(data, base_dir=BASE_DIR))
                self.assertEqual([finding.key for finding in errors], [key])
                node = fixed.data
                for part in key.split("."):
                    node = node[part]
                self.assertEqual(node, expected)
        text = section(self.spec, "### 3.5", "\n## 4.")
        self.assertInDoc("その要素だけ", text, SPECIFICATION_MD)

    def test_a_check_that_fails_by_itself_is_a_warning_and_does_not_stop_startup(self):
        broken = lambda config: 1 / 0  # noqa: E731
        with mock.patch.object(configcheck, "_check_timezone", broken):
            findings = configcheck.validate(Config(DEFAULT_CONFIG, base_dir=BASE_DIR))
        self.assertEqual([finding.level for finding in findings], [configcheck.WARNING])
        self.assertInDoc("検査できませんでした", self.spec, SPECIFICATION_MD)

    def test_the_limits_in_the_table_are_the_ones_in_the_code(self):
        claims = [
            Claim(SPECIFICATION_MD, "時報の時刻を調べる範囲の上限",
                  r"調べる範囲（`end_hour` − `start_hour` \+ 1）が ([\d,]+) 件を超える",
                  ("{0:,}".format(configcheck._MAX_HOUR_SPAN),)),
            Claim(SPECIFICATION_MD, "セグメントの間の無音の上限",
                  r"`audio\.gap_ms` が負、または ([\d,]+) ミリ秒を超える",
                  ("{0:,}".format(configcheck._MAX_GAP_MS),)),
            Claim(SPECIFICATION_MD, "気温の読み上げ文の数の上限（警告）",
                  r"気温の読み上げ文が (\d+) 件を超える", (configcheck._MAX_TEMP_VALUES,)),
            Claim(SPECIFICATION_MD, "待機の分割単位の下限", r"`schedule\.max_sleep_seconds` が (\d+) 未満",
                  (next(rule.minimum for rule in configcheck._NUMBERS
                        if rule.key == "schedule.max_sleep_seconds"),)),
        ]
        problems = []
        for claim in claims:
            found, wrong = check_claim(doc(claim.path), claim.pattern, claim.expected)
            if not found:
                problems.append("{0}: 記述が見つかりません: {1}".format(claim.name, claim.pattern))
            problems.extend("{0}: {1}".format(claim.name, quoted) for quoted, _ in wrong)
        self.assertEqual(problems, [])


# -- 点検（--check）の直し方 ---------------------------------------------------------

class CheckFixInDocsTest(DocTestCase):
    """``--check`` の「直し方」の形が、文書に載せたものと同じであること。"""

    def test_a_missing_folder_gets_mkdir_and_a_non_recursive_chown(self):
        # まだ無い場所には、その 1 つだけを作って渡す。実在する祖先（/ や /var/lib）は巻き込まない
        target = EXAMPLE_HOME + "/cache"
        fix = check._directory_fix(target, EXAMPLE_HOME, ["state.file"], "pi:pi", 1000)
        self.assertEqual(fix, "sudo mkdir -p {0} && sudo chown pi:pi {0}".format(target))
        self.assertNotIn("-R", fix)
        for name in (SPECIFICATION_MD, KNOWLEDGE_BASE_MD):
            with self.subTest(doc=name):
                self.assertInDoc(fix, doc(name), name)

    def test_an_existing_folder_owned_by_someone_else_gets_a_recursive_chown(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = os.getuid() if hasattr(os, "getuid") else 0
            fix = check._directory_fix(directory, directory, ["state.file"], "pi:pi", owner + 1)
        self.assertEqual(fix, "sudo chown -R pi:pi {0}".format(directory))
        shown = EXAMPLE_HOME + "/cache"
        for name in (SPECIFICATION_MD, KNOWLEDGE_BASE_MD):
            with self.subTest(doc=name):
                self.assertInDoc("sudo chown -R pi:pi " + shown, doc(name), name)

    def test_an_os_directory_is_never_given_to_the_service_user(self):
        fix = check._directory_fix("/var/lib", "/var/lib", ["tts.cache_dir"], "pi:pi", 1000)
        self.assertNotIn("chown", fix)
        self.assertIn("OS の場所", fix)
        for name in (SPECIFICATION_MD, KNOWLEDGE_BASE_MD):
            with self.subTest(doc=name):
                self.assertInDoc("OS の場所", doc(name), name)

    def test_the_documented_sound_checks_are_the_ones_the_code_makes(self):
        claims = [Claim(SPECIFICATION_MD, "MP3 の最小の大きさ", r"(\d+) バイトに満たない MP3",
                        (check.MIN_MP3_BYTES,))]
        for claim in claims:
            found, wrong = check_claim(doc(claim.path), claim.pattern, claim.expected)
            self.assertEqual((found, wrong), (1, []), claim.name)

    def test_the_check_does_not_report_root_owned_files_that_the_service_can_replace(self):
        # state.json は一時ファイルに書いて置き換えるので、root 所有のファイルが残っていても、
        # 置き場所のフォルダに書ければ困らない。文書も「所有者の NG」を載せない
        for name in (SPECIFICATION_MD, SETUP_MD, KNOWLEDGE_BASE_MD, REQUIREMENTS_MD):
            with self.subTest(doc=name):
                self.assertNotInDoc("root が所有するもの", doc(name), name)
                self.assertNotInDoc("root 所有のものはありません", doc(name), name)
                self.assertNotInDoc("root 所有のものが残っていないか", doc(name), name)


# -- 再点検で見つかった記述の食い違い -----------------------------------------------------

UPDATE_SH = os.path.join(REPO_ROOT, "scripts", "update.sh")
SETUP_SH = os.path.join(REPO_ROOT, "scripts", "setup.sh")


def stopping_table_reasons(text: str) -> List[str]:
    """SETUP.md 9 章 A の「止まる理由」の表の、止まる理由のセル（上から順）。"""
    body = section(text, "| 止まる理由 | 対処 |", "\n\n")
    return [split_cells(line)[0] for line in body.splitlines()[2:]]


#: 元の版へ戻す手順を表示する条件（コードを更新した回だけ）を述べた言い方。
ROLLBACK_CONDITION = re.compile(r"コード(?:が|を)更新(?:された|した)")
#: 元の版へ戻す手順に触れた文。
ROLLBACK_MENTION = re.compile(r"元の版(?:へ|への)戻(?:す|し)")


def rollback_paragraphs(text: str) -> List[str]:
    """元の版へ戻す手順に触れた段落・箇条書き 1 項目・表の 1 行（コードブロックの中は除く）。"""
    pieces = re.split(r"\n\s*\n|\n(?=[-*] |\d+\. |\|)", without_fenced_blocks(text))
    return [piece for piece in pieces if ROLLBACK_MENTION.search(piece)]


#: 元の版へ戻す手順を説明した箇所（文書, 節の始まり, 節の終わり）。
ROLLBACK_SECTIONS = (
    (README_MD, "## 9. 更新のしかた", "### 手で更新するとき"),
    (SETUP_MD, "## 9. 更新のしかた", "### B."),
    (KNOWLEDGE_BASE_MD, "### 3-3.", "### 3-4."),
    (SPECIFICATION_MD, "### 4.19", "\n---\n"),
    (REQUIREMENTS_MD, "### FR-13", "\n---\n"),
)


class RollbackMentionDetectionTest(unittest.TestCase):
    """検査そのものが、段落・箇条書き・表の行を取り分け、コードブロックの中は見ないこと。"""

    def test_paragraphs_list_items_and_table_rows_are_split(self):
        text = ("前置き\n\n- 元の版へ戻す手順は表示するだけ\n- 別の項目\n\n"
                "| 元の版へ戻す手順 | 表の行 |\n| 別の行 | 別 |\n\n```\n元の版へ戻す手順\n```\n")
        self.assertEqual(rollback_paragraphs(text),
                         ["- 元の版へ戻す手順は表示するだけ", "| 元の版へ戻す手順 | 表の行 |"])

    def test_the_condition_is_recognised_in_the_phrasings_the_docs_use(self):
        for phrase in ("コードが更新されたときだけ", "コードを更新した回の終わりに"):
            self.assertTrue(ROLLBACK_CONDITION.search(phrase), phrase)
        self.assertIsNone(ROLLBACK_CONDITION.search("最後に表示します"))


class UpdateStopTableTest(DocTestCase):
    """SETUP.md 9 章 A の「止まる理由」の表と、その前後の数え方・原因の分け方が実装と合っていること。"""

    def test_the_row_counts_written_beside_the_table_are_the_real_counts(self):
        # 行を足したのに「上の 4 つ」のままだった（実際は 5 行あった）
        text = doc(SETUP_MD)
        reasons = stopping_table_reasons(text)
        boundary = next((index for index, reason in enumerate(reasons) if "360 秒" in reason), -1)
        self.assertGreater(boundary, 0, "表の「360 秒待っても終わらない」の行が見つかりません")
        self.assertEqual(re.findall(r"上の (\d+) つは", text), [str(boundary)])
        self.assertEqual(re.findall(r"下の (\d+) つは", text), [str(len(reasons) - boundary)])

    def test_every_cause_of_a_failed_pull_that_the_script_tells_apart_has_a_row(self):
        script = read_text(UPDATE_SH)
        table = section(doc(SETUP_MD), "| 止まる理由 | 対処 |", "\n\n")
        for cause in ("network", "disk", "permission", "untracked"):
            with self.subTest(cause=cause):
                self.assertIn("pull_problem=" + cause, script)
        # 表が git の表示として挙げる語は、スクリプトが見分けに使う語と同じ
        for word in ("Permission denied", "insufficient permission", "would be overwritten",
                     "No space left on device", "Read-only file system", "Input/output error",
                     "Disk quota exceeded"):
            with self.subTest(word=word):
                self.assertIn(word, script)
                self.assertIn(word, table)
        self.assertIn("sudo chown -R ${owner_user", script)
        self.assertInDoc("sudo chown -R pi:pi /home/pi/campus-chime", table, SETUP_MD)
        self.assertInDoc("ネットワーク", table, SETUP_MD)

    def test_the_disk_row_names_the_checks_the_script_shows(self):
        # 空き容量の不足・読み取り専用の SD カードを、ネットワークや権限の問題と取り違えさせない
        script = read_text(UPDATE_SH)
        shown = section(script, "    disk)", "    untracked)")
        rows = [line for line in section(doc(SETUP_MD), "| 止まる理由 | 対処 |", "\n\n").splitlines()
                if "No space left on device" in line]
        self.assertEqual(len(rows), 1)
        for command in ("df -h", "dmesg"):
            with self.subTest(command=command):
                self.assertIn(command, shown)
                self.assertIn(command, rows[0])
        for needle in ("SD カード", "ネットワークの問題ではない"):
            with self.subTest(needle=needle):
                self.assertIn(needle, rows[0])

    def test_a_disk_problem_is_recognised_before_a_permission_word(self):
        # 容量の不足や読み取り専用は、持ち主を直しても直らない。権限の語句と一緒に出ても権限と案内しない
        script = read_text(UPDATE_SH)
        self.assertLess(script.index("pull_problem=disk"), script.index("pull_problem=permission"))
        self.assertInDoc("権限より先に", section(doc(SPECIFICATION_MD), "### 4.19", "\n---\n"), SPECIFICATION_MD)

    def test_the_network_row_counts_the_pull_rows_below_it(self):
        reasons = stopping_table_reasons(doc(SETUP_MD))
        network = next((index for index, reason in enumerate(reasons)
                        if reason.startswith("`git pull` できない（ネットワーク")), -1)
        self.assertGreaterEqual(network, 0, "表の「ネットワークにつながらない」の行が見つかりません")
        below = [reason for reason in reasons[network + 1:] if reason.startswith("`git pull` できない")]
        self.assertEqual(re.findall(r"下の (\d+) つに当てはまらない", reasons[network]), [str(len(below))])

    def test_the_other_docs_name_the_same_causes(self):
        command = "sudo chown -R pi:pi /home/pi/campus-chime"
        for name in (README_MD, KNOWLEDGE_BASE_MD):
            with self.subTest(doc=name):
                self.assertInDoc(command, doc(name), name)
                self.assertInDoc("df -h", doc(name), name)
        spec = section(doc(SPECIFICATION_MD), "### 4.19", "\n---\n")
        for needle in ("権限", "Git 管理外のファイル", "ネットワーク", "ディスク", "No space left on device",
                       "Read-only file system"):
            with self.subTest(needle=needle):
                self.assertInDoc(needle, spec, SPECIFICATION_MD)
        for name in (REQUIREMENTS_MD,):
            fr13 = section(doc(name), "### FR-13", "\n---\n")
            self.assertInDoc("権限", fr13, name)
            self.assertInDoc("ディスク", fr13, name)

    def test_the_readme_stop_list_names_every_cause_of_a_failed_pull(self):
        readme = doc(README_MD)
        stop_list = next((line for line in readme.splitlines() if "何も変えずに**理由を表示して止まります" in line), "")
        self.assertTrue(stop_list, "README に、止まる条件の一覧がありません")
        for needle in ("ネットワーク", "権限", "Git 管理外", "ディスク"):
            with self.subTest(needle=needle):
                self.assertIn(needle, stop_list)
        causes = next((paragraph for paragraph in readme.split("\n\n") if paragraph.startswith("`git pull` が止まる原因")), "")
        for needle in ("No space left on device", "Read-only file system", "df -h", "dmesg", "SD カード"):
            with self.subTest(needle=needle):
                self.assertIn(needle, causes)

    def test_a_partly_applied_pull_is_not_called_unchanged(self):
        # 権限やディスクで失敗した pull は途中まで進むことがある。スクリプトも「何も変更していません」と言わない
        script = read_text(UPDATE_SH)
        for label, start, end, word in (("権限", "    permission)", "    disk)", "Permission denied"),
                                        ("ディスク", "    disk)", "    untracked)", "No space left on device")):
            with self.subTest(cause=label):
                self.assertNotIn("何も変更していません", section(script, start, end))
                table = section(doc(SETUP_MD), "| 止まる理由 | 対処 |", "\n\n")
                rows = [line for line in table.splitlines() if word in line]
                self.assertEqual(len(rows), 1)
                self.assertIn("途中", rows[0])
        lead = re.search(r"上の \d+ つは、\*\*何も変えずに\*\*止まります（[^）]*）", doc(SETUP_MD))
        self.assertIsNotNone(lead, "表の前の文に、途中まで進むことがある場合の断りがありません")
        for needle in ("権限", "ディスク"):
            self.assertIn(needle, lead.group(0))

    def test_the_hand_off_when_the_config_is_unreadable_is_worded_like_the_script(self):
        # 急いで setup.sh を手で実行しても、config.json を読めないままでは反映されない
        sentence = "設定を読めないままでは、setup.sh もサービスを再起動しません"
        self.assertIn(sentence, read_text(UPDATE_SH))
        table = section(doc(SETUP_MD), "| 止まる理由 | 対処 |", "\n\n")
        self.assertRegex(table, r"`?setup\.sh`? もサービスを再起動しない")

    def test_the_rollback_is_described_as_shown_only_when_the_code_changed(self):
        # 続きから反映する回（コードが最新）は、元の版へ戻す手順を表示しない。
        # 「最後に、元の版へ戻す手順を表示します」と無条件に書くと、読者は止まった回の表示を捨ててしまう
        script = read_text(UPDATE_SH)
        calls = len(re.findall(r"^\s+print_rollback\b", script, re.MULTILINE))
        guarded = re.findall(r'if \[ "\$\{code_changed\}" -eq 1 \]; then\n\s+print_rollback\b', script)
        self.assertEqual((calls, len(guarded)), (3, 3), "戻す手順の表示は、コードを更新した回だけ")
        problems = []
        for name, start, end in ROLLBACK_SECTIONS:
            mentions = rollback_paragraphs(section(doc(name), start, end))
            self.assertTrue(mentions, "{0} に元の版へ戻す手順の説明がありません".format(name))
            problems.extend("{0}: {1}".format(name, piece.strip()[:60]) for piece in mentions
                            if not ROLLBACK_CONDITION.search(piece))
        self.assertEqual(problems, [], "コードを更新した回にだけ表示することを書いていません")

    def test_the_readme_tells_the_reader_to_keep_the_rollback_of_the_stopped_run(self):
        readme = doc(README_MD)
        self.assertNotInDoc("最後に、元の版へ戻す手順を表示します", readme, README_MD)
        self.assertInDoc("止まった回の表示", readme, README_MD)


class SetupWithoutServiceTest(DocTestCase):
    """設定ファイルを読めないときの ``setup.sh`` は、``--no-service`` でも終了コード 1 で終わること。"""

    def test_the_script_exits_1_after_the_service_step_whether_or_not_it_ran(self):
        script = read_text(SETUP_SH)
        skipped = script.index("--no-service が指定されたため")
        guard = script.index('if [ "${check_status}" -eq 2 ]; then\n  log "導入は途中です"')
        self.assertLess(skipped, guard)
        self.assertIn("exit 1", script[guard:script.index('log "完了"')])

    def test_the_docs_do_not_exempt_no_service(self):
        spec = section(doc(SPECIFICATION_MD), "### 4.19", "\n---\n")
        steps = section(doc(SETUP_MD), "## 5. 本体を導入する", "## 6.")
        for name, text in ((SPECIFICATION_MD, spec), (SETUP_MD, steps), (README_MD, doc(README_MD))):
            with self.subTest(doc=name):
                self.assertInDoc("--no-service", text, name)
        self.assertRegex(spec, r"`--no-service` を付けても[^。]*終了コード 1")
        self.assertRegex(steps, r"`--no-service` を付けても[^。]*終了コード 1")
        self.assertRegex(doc(README_MD), r"`--no-service` を付けても[^。]*終了コード 1")


class HourReadingsInDocsTest(DocTestCase):
    """``time_signal.hour_readings`` が表でないとき・空のときの読み方が、仕様書のとおりであること。"""

    def reading(self, value, hour=16):
        settings = copy.deepcopy(DEFAULT_CONFIG["time_signal"])
        settings["hour_readings"] = value
        return timesignal.hour_parts(hour, settings)["hour_reading"]

    def test_anything_that_is_not_a_table_uses_the_default_readings(self):
        for value in (None, [], 0, False, "", "x", 5, True, [1]):
            with self.subTest(value=value):
                self.assertEqual(self.reading(value), "よじ")

    def test_an_empty_table_means_no_overrides(self):
        self.assertEqual(self.reading({}), "4時")

    def test_the_specification_says_both(self):
        text = section(doc(SPECIFICATION_MD), "### 4.2 ", "### 4.3 ")
        self.assertInDoc("辞書でない値", text, SPECIFICATION_MD)
        self.assertInDoc("空の辞書 `{}`", text, SPECIFICATION_MD)


class HourSpanInDocsTest(DocTestCase):
    """時報の時刻を調べる範囲の上限（10,000,000 件）の境目と、超えたときの直し方が、仕様書のとおりであること。"""

    def config_with(self, **hourly):
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["schedule"]["hourly"].update(hourly)
        return Config(data, base_dir=BASE_DIR)

    def test_the_span_is_allowed_up_to_the_limit_and_only_the_outside_hour_is_reset(self):
        limit = configcheck._MAX_HOUR_SPAN
        end = DEFAULT_CONFIG["schedule"]["hourly"]["end_hour"]
        edge = end + 1 - limit            # 範囲がちょうど上限になる start_hour
        fixed, errors = configcheck.sanitized(self.config_with(start_hour=edge))
        self.assertEqual(errors, [])
        self.assertEqual(fixed.get("schedule.hourly.start_hour"), edge)
        fixed, errors = configcheck.sanitized(self.config_with(start_hour=edge - 1))
        self.assertEqual([finding.key for finding in errors], ["schedule.hourly.start_hour"])
        self.assertEqual(fixed.get("schedule.hourly.start_hour"),
                         DEFAULT_CONFIG["schedule"]["hourly"]["start_hour"])
        self.assertEqual(fixed.get("schedule.hourly.end_hour"), end)

    def test_when_both_hours_are_outside_both_are_reset(self):
        fixed, errors = configcheck.sanitized(
            self.config_with(start_hour=-10 ** 7, end_hour=10 ** 7))
        self.assertEqual(sorted(finding.key for finding in errors),
                         ["schedule.hourly.end_hour", "schedule.hourly.start_hour"])
        self.assertEqual(fixed.get("schedule.hourly.start_hour"),
                         DEFAULT_CONFIG["schedule"]["hourly"]["start_hour"])
        self.assertEqual(fixed.get("schedule.hourly.end_hour"),
                         DEFAULT_CONFIG["schedule"]["hourly"]["end_hour"])

    def test_the_specification_states_the_boundary_and_the_reset(self):
        text = section(doc(SPECIFICATION_MD), "### 3.5", "\n## 4.")
        self.assertInDoc("10,000,000 件ちょうどまでは動かす", text, SPECIFICATION_MD)
        self.assertInDoc("0〜23 の外にあるほうの時（どちらも外なら両方）だけを既定値に戻す", text,
                         SPECIFICATION_MD)


class EnginesWithoutPrerecordedInDocsTest(DocTestCase):
    """``tts.engines`` に ``prerecorded`` が無いとき、各道具がどう言うかが、文書のとおりであること。"""

    EMPTY_ENGINES = (["voicevox"], [], "", {})

    def config_with(self, engines):
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["tts"]["engines"] = engines
        return Config(data, base_dir=BASE_DIR)

    def test_the_config_check_warns_and_keeps_the_value(self):
        for engines in self.EMPTY_ENGINES:
            with self.subTest(engines=engines):
                config = self.config_with(engines)
                found = [finding for finding in configcheck.validate(config)
                         if finding.key == "tts.engines"]
                self.assertEqual([finding.level for finding in found], [configcheck.WARNING])
                fixed, errors = configcheck.sanitized(config)
                self.assertEqual(errors, [])
                self.assertEqual(fixed.data, config.data)

    def test_check_calls_the_voices_ng_even_though_all_of_them_are_on_disk(self):
        for engines in self.EMPTY_ENGINES:
            with self.subTest(engines=engines):
                voices = check.check_voices(self.config_with(engines))
                self.assertEqual(voices[0].level, check.NG)
                self.assertEqual(voices[0].title, "作り置き")
                self.assertIn("無音", voices[0].detail)

    def test_check_does_not_count_the_default_engines_as_a_problem(self):
        voices = check.check_voices(self.config_with(["prerecorded", "voicevox"]))
        self.assertEqual([result.level for result in voices], [check.OK])

    def test_the_docs_say_so(self):
        spec = doc(SPECIFICATION_MD)
        for heading, end in (("### 3.5", "\n## 4."), ("### 4.14 ", "### 4.15 "), ("### 4.15 ", "### 4.16 ")):
            with self.subTest(section=heading):
                self.assertInDoc("`tts.engines` に `prerecorded` が無い", section(spec, heading, end),
                                 SPECIFICATION_MD)
        self.assertInDoc("tts.engines", section(doc(SETUP_MD), "### 6-2.", "### 6-3."), SETUP_MD)
        self.assertInDoc("`tts.engines`", section(doc(REQUIREMENTS_MD), "### FR-11", "### FR-12"),
                         REQUIREMENTS_MD)


class VoicevoxTimeoutsInDocsTest(DocTestCase):
    """VOICEVOX ENGINE の待ち時間の上限（これを超えると起動が例外になる）が、設定の検査と仕様書で一致すること。"""

    KEYS = ("tts.voicevox.timeout_seconds", "tts.voicevox.probe_timeout_seconds")

    def errors_for(self, key, value):
        data = copy.deepcopy(DEFAULT_CONFIG)
        set_at(data, key, value)
        return [finding.key for finding in configcheck.validate(Config(data, base_dir=BASE_DIR))
                if finding.level == configcheck.ERROR]

    def test_a_timeout_beyond_the_socket_limit_is_an_error_and_the_limit_itself_is_not(self):
        limit = configcheck._MAX_TIMEOUT_SECONDS
        for key in self.KEYS:
            for value, expected in ((9e9, []), (limit, []), (limit + 1, [key]), (1e12, [key]),
                                    (-1e12, [key]), (2.5, [])):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.errors_for(key, value), expected)

    def test_the_limit_is_what_the_socket_accepts(self):
        # 上限の値はソケットに渡せ、1 つ超えると OverflowError になる（文書が理由に挙げる動き）
        import socket
        probe = socket.socket()
        try:
            probe.settimeout(configcheck._MAX_TIMEOUT_SECONDS)
            with self.assertRaises(OverflowError):
                probe.settimeout(configcheck._MAX_TIMEOUT_SECONDS * 2)
        finally:
            probe.close()

    def test_the_specification_states_the_limit(self):
        spec = doc(SPECIFICATION_MD)
        found, wrong = check_claim(spec, r"`tts\.voicevox\.probe_timeout_seconds` の絶対値が ([\d,]+) 秒を超える",
                                   ("{0:,}".format(configcheck._MAX_TIMEOUT_SECONDS),))
        self.assertEqual((found, wrong), (1, []))
        self.assertInDoc("読み上げエンジンの状態を調べられませんでした",
                         section(spec, "### 4.10 ", "### 4.11 "), SPECIFICATION_MD)
        self.assertIn("読み上げエンジンの状態を調べられませんでした",
                      read_text(os.path.join(REPO_ROOT, "chime", "app.py")))


class WeatherTimeoutInDocsTest(DocTestCase):
    """``weather.timeout_seconds`` がソケットの受けられない値のときは、警告にとどめて置き換えないこと。

    天気予報の取得は例外になるが、放送を組み立てる側が受け止めて、天気予報を飛ばすだけで放送を続ける
    （VOICEVOX ENGINE の待ち時間と違い、起動も放送も止まらない）。
    """

    KEY = "weather.timeout_seconds"

    def levels_for(self, value):
        data = copy.deepcopy(DEFAULT_CONFIG)
        set_at(data, self.KEY, value)
        return [finding.level for finding in configcheck.validate(Config(data, base_dir=BASE_DIR))
                if finding.key == self.KEY]

    def test_a_timeout_beyond_the_socket_limit_is_a_warning_and_the_limit_itself_is_not_reported(self):
        limit = configcheck._MAX_TIMEOUT_SECONDS
        for value, expected in ((9e9, []), (limit, []), (2.5, []),
                                (limit + 1, [configcheck.WARNING]), (1e12, [configcheck.WARNING]),
                                (-1e12, [configcheck.WARNING])):
            with self.subTest(value=value):
                self.assertEqual(self.levels_for(value), expected)

    def test_a_value_that_cannot_be_read_as_a_number_is_still_an_error(self):
        for value in (None, "abc", [1]):
            with self.subTest(value=value):
                self.assertEqual(self.levels_for(value), [configcheck.ERROR])

    def test_the_value_is_kept_as_written(self):
        data = copy.deepcopy(DEFAULT_CONFIG)
        set_at(data, self.KEY, 1e12)
        config = Config(data, base_dir=BASE_DIR)
        fixed, errors = configcheck.sanitized(config)
        self.assertEqual(errors, [])
        self.assertEqual(fixed.data, config.data)

    def test_the_specification_puts_it_in_the_warning_column_with_the_same_limit(self):
        spec = doc(SPECIFICATION_MD)
        row = next(cells for cells in rule_table(spec) if "tts.voicevox.probe_timeout_seconds" in cells[1])
        claim = "`weather.timeout_seconds` の絶対値が"
        self.assertIn(claim, row[2])
        self.assertNotIn(claim, row[1])
        found, wrong = check_claim(spec, r"`weather\.timeout_seconds` の絶対値が ([\d,]+) 秒を超える",
                                   ("{0:,}".format(configcheck._MAX_TIMEOUT_SECONDS),))
        self.assertEqual((found, wrong), (1, []))
        self.assertInDoc("天気予報が流れない", section(spec, "### 3.5", "\n## 4."), SPECIFICATION_MD)

    def test_the_reading_guide_says_so(self):
        guide = section(doc(KNOWLEDGE_BASE_MD), "### 3-6.", "\n---\n")
        warnings = next(line for line in guide.splitlines() if line.startswith("- **警告（置き換えない）の例。**"))
        errors = next(line for line in guide.splitlines() if line.startswith("- **NG（置き換える）になるのは"))
        self.assertIn("weather.timeout_seconds", warnings)
        self.assertNotIn("weather.timeout_seconds", errors)


class CommandsAsOneStringInDocsTest(DocTestCase):
    """``audio.commands`` の項目を 1 つの文字列で書いたときの扱いが、仕様書のとおりであること。"""

    def test_a_string_entry_is_an_error_and_only_that_entry_is_replaced(self):
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["audio"]["commands"] = {".wav": "aplay -q {path}",
                                     ".mp3": ["mpg123", "-q", "{path}"],
                                     ".ogg": "ogg123 {path}"}
        fixed, errors = configcheck.sanitized(Config(data, base_dir=BASE_DIR))
        self.assertEqual([finding.key for finding in errors], ["audio.commands"])
        self.assertEqual(fixed.get("audio.commands"), {
            ".wav": DEFAULT_CONFIG["audio"]["commands"][".wav"],     # 既定の項目に置き換える
            ".mp3": ["mpg123", "-q", "{path}"]})                      # 読める項目はそのまま。.ogg は取り除く

    def test_an_empty_string_and_a_list_of_strings_are_not_errors(self):
        for command in ("", [], ["aplay", "-q", "{path}"]):
            with self.subTest(command=command):
                data = copy.deepcopy(DEFAULT_CONFIG)
                data["audio"]["commands"] = {".wav": command}
                self.assertEqual([finding for finding in configcheck.validate(
                    Config(data, base_dir=BASE_DIR)) if finding.level == configcheck.ERROR], [])

    def commands_findings(self, backend):
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["audio"]["backend"] = backend
        data["audio"]["commands"] = {".wav": "aplay -q {path}"}
        config = Config(data, base_dir=BASE_DIR)
        return config, [finding for finding in configcheck.validate(config) if finding.key == "audio.commands"]

    def test_a_string_entry_is_an_error_only_when_the_table_can_be_read(self):
        # 表を読むのは外部コマンドの再生だけ。auto は pygame が無いときに外部コマンドで再生する
        for backend in ("command", "auto", "aplay", "Command"):
            with self.subTest(backend=backend):
                config, found = self.commands_findings(backend)
                self.assertEqual([finding.level for finding in found], [configcheck.ERROR])
                self.assertNotEqual(configcheck.sanitized(config)[0].data, config.data)

    def test_a_string_entry_is_only_a_warning_when_the_backend_never_reads_the_table(self):
        # 壊れていても、読まない間はランタイムが動く。置き換えず、書いたとおりにする（v6.0.0 と同じ動き）
        for backend in ("mock", "pygame", "MOCK"):
            with self.subTest(backend=backend):
                config, found = self.commands_findings(backend)
                self.assertEqual([finding.level for finding in found], [configcheck.WARNING])
                self.assertIn("mock か pygame", found[0].message)
                fixed, errors = configcheck.sanitized(config)
                self.assertEqual(errors, [])
                self.assertEqual(fixed.data, config.data)

    def test_the_specification_says_what_happens(self):
        text = section(doc(SPECIFICATION_MD), "### 3.5", "\n## 4.")
        self.assertInDoc("1 つの文字列で書いた", text, SPECIFICATION_MD)
        self.assertInDoc("その拡張子の既定の項目に置き換え", text, SPECIFICATION_MD)
        sanitized_row = next(line for line in doc(SPECIFICATION_MD).splitlines()
                             if line.startswith("| `sanitized(config)`"))
        self.assertIn("既定の項目に置き換え", sanitized_row)

    def test_the_specification_says_the_table_is_an_error_only_for_the_backends_that_read_it(self):
        spec = doc(SPECIFICATION_MD)
        row = next(cells for cells in rule_table(spec) if cells[0] == "外部コマンドの表")
        for needle in ("`command`", "`auto`"):
            with self.subTest(column="error", needle=needle):
                self.assertIn(needle, row[1])
        for needle in ("`audio.backend`", "`mock`", "`pygame`", "置き換えない"):
            with self.subTest(column="warning", needle=needle):
                self.assertIn(needle, row[2])
        self.assertNotEqual(row[2], "なし", "再生方式が mock / pygame の間は、警告にとどめる")
        notes = section(spec, "表の補足:", "\n起動時は")
        self.assertInDoc("`audio.commands` も同じ", notes, SPECIFICATION_MD)
        section_row = next(cells for cells in rule_table(spec) if cells[0].startswith("節"))
        self.assertIn("`audio.backend` が `mock` / `pygame` の間", section_row[1])
        failures = next(line for line in spec.splitlines()
                        if line.startswith("| 設定値が、ランタイムが動けない値（error。"))
        self.assertIn("`audio.backend` が `mock` / `pygame` でないとき", failures)

    def test_the_reading_guide_lists_it_among_the_ng_values(self):
        guide = section(doc(KNOWLEDGE_BASE_MD), "### 3-6.", "\n---\n")
        self.assertInDoc("外部コマンド", guide, KNOWLEDGE_BASE_MD)
        errors = next(line for line in guide.splitlines() if line.startswith("- **NG（置き換える）になるのは"))
        warnings = next(line for line in guide.splitlines() if line.startswith("- **警告（置き換えない）の例。**"))
        # 再生方式が mock / pygame の間は表を読まないので、警告にとどまる
        self.assertIn("`audio.commands`", errors)
        self.assertIn("`mock` / `pygame`", errors)
        self.assertIn("`audio.commands`", warnings)
        self.assertIn("今は audio.backend が mock か pygame なので使われませんが", guide)


class CoverageLimitsInDocsTest(DocTestCase):
    """作り置きの数え上げの上限（件数・文字数・書式指定の幅）が、実装の定数と文書で一致すること。"""

    def test_the_limits_in_the_specification_are_the_constants(self):
        spec = doc(SPECIFICATION_MD)
        claims = (
            ("文字数の上限", r"既定は ([\d,]+) 文字（`MAX_COVERAGE_CHARS`）",
             ("{0:,}".format(phrases.MAX_COVERAGE_CHARS),)),
            ("書式指定の幅の上限", r"書式指定の幅や桁数は (\d+)（`MAX_FORMAT_SPEC`）まで",
             (phrases.MAX_FORMAT_SPEC,)),
            ("設定の検査が警告にする幅", r"書式指定の幅や桁数（[^）]*）が (\d+) を超える",
             (configcheck._MAX_FORMAT_SPEC,)),
        )
        for name, pattern, expected in claims:
            with self.subTest(name=name):
                found, wrong = check_claim(spec, pattern, expected)
                self.assertEqual((found, wrong), (1, []))

    def test_the_two_modules_agree_on_the_width(self):
        self.assertEqual(configcheck._MAX_FORMAT_SPEC, phrases.MAX_FORMAT_SPEC)

    def test_a_huge_width_or_label_is_refused_before_it_is_built(self):
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["weather"]["enabled"] = False
        data["weather"]["sentence_weather"] = "{label:>200000000}"
        config = Config(data, base_dir=BASE_DIR)
        with self.assertRaises(phrases.CoverageTooLarge) as caught:
            phrases.coverage(config, lambda text: None)
        self.assertIn("weather.sentence_weather", str(caught.exception))
        data["weather"]["sentence_weather"] = DEFAULT_CONFIG["weather"]["sentence_weather"]
        data["weather"]["open_meteo"]["locations"] = [
            {"label": "あ" * 3_000_000, "latitude": 35, "longitude": 135}]
        with self.assertRaises(phrases.CoverageTooLarge):
            phrases.coverage(Config(data, base_dir=BASE_DIR), lambda text: None)

    def test_the_docs_describe_what_the_tools_show_for_a_refused_count(self):
        # 利用者に例外の型名は見せない（--status）。--check は「数えられません」の NG
        spec = doc(SPECIFICATION_MD)
        self.assertInDoc("数え上げを省", section(spec, "### 4.12 ", "### 4.13 "), SPECIFICATION_MD)
        self.assertInDoc("数えられません", section(spec, "### 4.14 ", "### 4.15 "), SPECIFICATION_MD)
        self.assertInDoc("数えられません", section(spec, "### 4.15 ", "### 4.16 "), SPECIFICATION_MD)


class DegradedBroadcastInDocsTest(DocTestCase):
    """簡易の内容（最小のプラン）で鳴った放送を、``--status`` が成功のまま見逃さないこと。"""

    def test_the_history_line_carries_the_note(self):
        entry = {"kind": "hourly", "result": "ok", "at": "2026-10-09T10:00:00+09:00", "degraded": True}
        line = status.describe_entry(entry, ZONE)
        self.assertTrue(line.endswith(status.DEGRADED_NOTE), line)
        self.assertEqual(status.DEGRADED_NOTE, "簡易の内容で放送")
        entry["degraded"] = False
        self.assertNotIn(status.DEGRADED_NOTE, status.describe_entry(entry, ZONE))

    def test_the_last_broadcast_being_degraded_is_a_reason_to_check(self):
        sample = example_status(Config(DEFAULT_CONFIG, base_dir=BASE_DIR))
        values = dict(vars(sample))
        values["history"] = [{"result": "ok", "degraded": True}]
        reasons = status.attention(status.Status(**values))
        self.assertEqual(len(reasons), 1)
        values["history"] = [{"result": "ok", "degraded": True}, {"result": "ok"}]
        self.assertEqual(len(status.attention(status.Status(**values))), 1)
        values["history"] = [{"result": "ok"}, {"result": "ok", "degraded": True}]
        self.assertEqual(status.attention(status.Status(**values)), [])

    def test_the_docs_explain_the_note_and_the_reason(self):
        for name in (SPECIFICATION_MD, KNOWLEDGE_BASE_MD):
            with self.subTest(doc=name):
                self.assertInDoc(status.DEGRADED_NOTE, doc(name), name)
        self.assertInDoc("簡易の内容", section(doc(SPECIFICATION_MD), "### 4.15 ", "### 4.16 "), SPECIFICATION_MD)
        self.assertInDoc("簡易の内容", section(doc(REQUIREMENTS_MD), "### FR-12", "### FR-13"), REQUIREMENTS_MD)


#: MPEG-1 Layer III・128 kbps・44.1 kHz・パディングなしのフレーム 1 つ（417 バイト）。
MPEG_FRAME = b"\xff\xfb\x90\x00" + bytes(413)


def id3v2_tag(body: int) -> bytes:
    """中身が ``body`` バイト（0 埋め）の ID3v2.3 タグ（ヘッダー 10 バイトを含む）。"""
    size = bytes([(body >> 21) & 0x7F, (body >> 14) & 0x7F, (body >> 7) & 0x7F, body & 0x7F])
    return b"ID3\x03\x00\x00" + size + bytes(body)


def id3v2_total_length(data: bytes) -> int:
    """``data`` の先頭にある ID3v2 タグの全長（ヘッダー 10 バイトを含む）。"""
    sizes = data[6:10]
    return 10 + ((sizes[0] << 21) | (sizes[1] << 14) | (sizes[2] << 7) | sizes[3])


class Mp3AndStickyChecksInDocsTest(DocTestCase):
    """``--check`` の MP3 の確かめ方と、sticky ビットのフォルダの扱いが、文書のとおりであること。"""

    def test_a_cut_hotaru_is_ng_and_the_whole_file_is_not(self):
        source = os.path.join(REPO_ROOT, "assets", "hotaru.mp3")
        self.assertIsNone(check._sound_problem(source))
        with open(source, "rb") as handle:
            data = handle.read()
        self.assertGreater(len(data), 100_000)
        with tempfile.TemporaryDirectory() as directory:
            cut = os.path.join(directory, "hotaru.mp3")
            for size in (check.MIN_MP3_BYTES, 50_000, len(data) // 2, len(data) - 1):
                with self.subTest(size=size):
                    with open(cut, "wb") as handle:
                        handle.write(data[:size])
                    self.assertIsNotNone(check._sound_problem(cut))

    def test_the_trailing_data_the_docs_allow_is_accepted(self):
        # 同梱の蛍の光は、末尾に ID3v1 タグ（最後の 128 バイト）が付いている。それを外した本体で確かめる
        source = os.path.join(REPO_ROOT, "assets", "hotaru.mp3")
        with open(source, "rb") as handle:
            data = handle.read()
        self.assertEqual(data[-128:-125], b"TAG")
        body = data[:-128]
        id3v1 = b"TAG" + bytes(125)
        padding = bytes(check._MP3_MAX_PADDING)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "hotaru.mp3")
            for name, content, ok in (("タグなし", body, True), ("ID3v1", body + id3v1, True),
                                      ("0 埋め", body + padding, True),
                                      ("0 埋めと ID3v1", body + padding + id3v1, True),
                                      ("0 埋めが長すぎる", body + padding + bytes(1), False),
                                      ("フレームでないデータ", body + b"XYZ" * 10, False)):
                with self.subTest(tail=name):
                    with open(path, "wb") as handle:
                        handle.write(content)
                    problem = check._sound_problem(path)
                    self.assertEqual(problem is None, ok, problem)

    def test_a_tag_with_no_frames_behind_it_is_ng(self):
        # 電源断で、大きさは記録されたのに、先頭のブロック（ID3 タグ）しかディスクに届かなかったファイル。
        # ファイルの先頭は ID3 で、大きさも足りているので、先頭と大きさの確かめだけでは通ってしまう
        source = os.path.join(REPO_ROOT, "assets", "hotaru.mp3")
        with open(source, "rb") as handle:
            data = handle.read()
        tag_end = id3v2_total_length(data)
        self.assertEqual(data[tag_end], 0xFF, "同梱の蛍の光は、ID3 タグの直後からフレームが始まる")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "hotaru.mp3")
            for name, content in (
                    ("先頭の 4096 バイトだけ", data[:4096] + bytes(len(data) - 4096)),
                    ("先頭の 8192 バイトだけ", data[:8192] + bytes(len(data) - 8192)),
                    ("ID3 タグの終わりまで", data[:tag_end] + bytes(len(data) - tag_end)),
                    ("ID3 タグだけ", data[:tag_end]),
                    ("関係のないデータ", data[:tag_end] + b"XYZ" * 10000)):
                with self.subTest(written=name):
                    with open(path, "wb") as handle:
                        handle.write(content)
                    problem = check._sound_problem(path)
                    self.assertIsNotNone(problem)
                    self.assertIn("途中で切れているか壊れています", problem)

    def test_the_frames_may_start_later_than_the_tag_end_only_within_the_search_range(self):
        # タグの大きさに数えられていない 0 埋めが挟まる MP3 もある。仕様書の範囲の内なら正常、外なら NG
        frames = MPEG_FRAME * 12
        near, far = check._MP3_FRAME_SEARCH_BYTES - len(frames), check._MP3_FRAME_SEARCH_BYTES + 1
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.mp3")
            for gap, ok in ((0, True), (5000, True), (near, True), (far, False)):
                with self.subTest(gap=gap):
                    with open(path, "wb") as handle:
                        handle.write(id3v2_tag(1000) + bytes(gap) + frames)
                    problem = check._sound_problem(path)
                    self.assertEqual(problem is None, ok, problem)

    def test_stacked_id3v2_tags_are_skipped_up_to_the_documented_number(self):
        frames = MPEG_FRAME * 12
        cut = frames[:-5]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.mp3")
            for count in range(1, check._ID3V2_MAX_TAGS + 1):
                for name, body, ok in (("すべて書けた", frames, True), ("最後が切れた", cut, False)):
                    with self.subTest(tags=count, written=name):
                        with open(path, "wb") as handle:
                            handle.write(id3v2_tag(20) * count + body)
                        problem = check._sound_problem(path)
                        self.assertEqual(problem is None, ok, problem)

    def test_the_specification_describes_the_walk_and_its_limits(self):
        spec = section(doc(SPECIFICATION_MD), "### 4.14 ", "### 4.15 ")
        for needle in ("フレームをファイルの終わりまで", "ID3v1", "APEv2", "ID3 タグのあとにフレームが無い"):
            with self.subTest(needle=needle):
                self.assertInDoc(needle, spec, SPECIFICATION_MD)
        claims = (
            ("0 埋めの上限", r"(\d+) バイトまでの 0 埋め", (check._MP3_MAX_PADDING,)),
            ("タグのあとにフレームを探す範囲", r"タグの直後から ([\d,]+) バイトの範囲",
             ("{0:,}".format(check._MP3_FRAME_SEARCH_BYTES),)),
            ("読み飛ばす ID3v2 タグの数", r"重なっていても (\d+) つまで", (check._ID3V2_MAX_TAGS,)),
        )
        for name, pattern, expected in claims:
            with self.subTest(claim=name):
                found, wrong = check_claim(spec, pattern, expected)
                self.assertEqual((found, wrong), (1, []))

    def test_the_other_docs_no_longer_promise_more_than_they_check(self):
        # 先頭だけ・大きさだけを見ると書いても、ファイルの終わりまでたどると書いても、実装と食い違う。
        # フレームをたどること、タグのあとにフレームが無いものも見つけることを、どの文書も書く
        for name, start, end in ((SETUP_MD, "### 6-2.", "### 6-3."),
                                 (REQUIREMENTS_MD, "### FR-11", "### FR-12"),
                                 (KNOWLEDGE_BASE_MD, "### 3-6.", "\n---\n")):
            with self.subTest(doc=name):
                self.assertInDoc("フレーム", section(doc(name), start, end), name)
                self.assertInDoc("ID3 タグのあとにフレームが無い", section(doc(name), start, end), name)

    def test_the_test_table_says_what_the_check_tests_cover(self):
        row = next(line for line in doc(SPECIFICATION_MD).splitlines() if line.startswith("| `tests/test_check.py` |"))
        self.assertIn("MP3 は先頭・大きさ・フレームをファイルの終わりまでたどること", row)
        self.assertIn("ID3 タグのあとにフレームが無い", row)
        self.assertNotIn("MP3 の先頭と大きさ）", row, "先頭と大きさしか確かめない、という古い書き方が残っている")

    def test_a_sticky_directory_with_someone_elses_state_file_is_ng(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o1777)
            state = os.path.join(directory, "state.json")
            with open(state, "w", encoding="utf-8") as handle:
                handle.write("{}")
            data = copy.deepcopy(DEFAULT_CONFIG)
            data["state"]["file"] = state
            config = Config(data, base_dir=directory)
            with mock.patch.object(check, "_uid_of", return_value=0):   # 持ち主は、自分ではない
                result = check._sticky_problem(config, state, "pi:pi", 1000)
                self.assertIsNotNone(result)
                self.assertEqual(result.level, check.NG)
                self.assertIsNone(check._sticky_problem(config, state, "pi:pi", 0))   # root には当てはまらない
            with mock.patch.object(check, "_uid_of", return_value=1000):
                self.assertIsNone(check._sticky_problem(config, state, "pi:pi", 1000))
            os.chmod(directory, 0o755)
            with mock.patch.object(check, "_uid_of", return_value=0):
                self.assertIsNone(check._sticky_problem(config, state, "pi:pi", 1000))

    def test_the_docs_name_the_exception_to_the_root_owned_file_rule(self):
        for name, start, end in ((SPECIFICATION_MD, "### 4.14 ", "### 4.15 "),
                                 (KNOWLEDGE_BASE_MD, "### 3-6.", "\n---\n")):
            with self.subTest(doc=name):
                self.assertInDoc("sticky ビット", section(doc(name), start, end), name)


# -- 文書ではなく、ソースの書き方 ---------------------------------------------------------

def python_sources() -> List[str]:
    """リポジトリの Python のソース（``campus_chime.py``・``chime/``・``scripts/``・``tests/``）。"""
    paths = [os.path.join(REPO_ROOT, "campus_chime.py")]
    for directory in ("chime", "scripts", "tests"):
        paths.extend(sorted(glob.glob(os.path.join(REPO_ROOT, directory, "*.py"))))
    return [path for path in paths if os.path.isfile(path)]


def compile_warnings(source: str, name: str) -> List[str]:
    """``source`` をコンパイルして出る警告（無効なエスケープなど）を文字列にして返す。"""
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        compile(source, name, "exec", dont_inherit=True)
    return ["{0}:{1}: {2}: {3}".format(name, warning.lineno, warning.category.__name__, warning.message)
            for warning in caught]


class CompileWarningDetectionTest(unittest.TestCase):
    """検査そのものが、無効なエスケープを見つけ、raw 文字列と二重のバックスラッシュは通すこと。"""

    def test_an_invalid_escape_in_a_docstring_is_found(self):
        found = compile_warnings('def f():\n    """表の区切り（``\\|``）。"""\n', "x.py")
        self.assertEqual(len(found), 1)
        self.assertIn("x.py:2:", found[0])
        self.assertIn("invalid escape sequence", found[0])

    def test_a_raw_string_and_a_doubled_backslash_are_accepted(self):
        self.assertEqual(compile_warnings('def f():\n    r"""``\\|``"""\n', "x.py"), [])
        self.assertEqual(compile_warnings('def f():\n    """``\\\\|``"""\n', "x.py"), [])


class SourcesCompileWithoutWarningsTest(unittest.TestCase):
    """どのソースも、コンパイルで警告を出さないこと（3.12 以降の ``SyntaxWarning``、将来の ``SyntaxError``）。"""

    def test_no_source_file_warns_when_compiled(self):
        sources = python_sources()
        self.assertGreater(len(sources), 40)
        problems = []
        for path in sources:
            problems.extend(compile_warnings(read_text(path), os.path.relpath(path, REPO_ROOT)))
        self.assertEqual(problems, [], "raw 文字列（r\"\"\"…\"\"\"）にするか、バックスラッシュを二重にする")


# -- KNOWLEDGE_BASE の引用 ---------------------------------------------------------

def message_source() -> str:
    """画面やログに出す文面を持つファイル（``chime/*.py`` と ``scripts/*.sh``）の中身。"""
    paths = sorted(glob.glob(os.path.join(REPO_ROOT, "chime", "*.py"))
                   + glob.glob(os.path.join(REPO_ROOT, "scripts", "*.sh")))
    return "\n".join(read_text(path) for path in paths)


_PLACEHOLDER_WORD = re.compile(r"\b[NM]\b|…|\.\.\.")


def quoted_chunks(text: str) -> List[Tuple[str, str]]:
    """「…」で引用した文面を、``(引用, 実装に無ければならない一続きの文字列)`` に分ける。

    引用の中の ``…``・``N``・``M`` は、実際の値に置き換わる箇所なので、そこで区切る。
    4 文字に満たない断片は、偶然どこにでもあるので調べない。
    """
    found = []
    for quote in re.findall(r"「([^」\n]+)」", text):
        for chunk in _PLACEHOLDER_WORD.split(quote):
            chunk = chunk.strip(" ：:。、（）()")
            if len(chunk) >= 4:
                found.append((quote, chunk))
    return found


class QuotedChunkTest(unittest.TestCase):
    """検査そのものが、置き換わる箇所で区切り、短い断片は調べないこと。"""

    def test_placeholders_split_a_quote(self):
        self.assertEqual(quoted_chunks("「N 件中 M 件の声がありません」"),
                         [("N 件中 M 件の声がありません", "件の声がありません")])
        self.assertEqual(quoted_chunks("「… がまだありません（放送のときに自動で作ります）」"),
                         [("… がまだありません（放送のときに自動で作ります）",
                           "がまだありません（放送のときに自動で作ります")])

    def test_short_quotes_are_not_checked(self):
        self.assertEqual(quoted_chunks("「設定」と「NG」"), [])


class KnowledgeBaseQuotesTest(DocTestCase):
    """3-6 章で「こう表示される」と引用した文面が、実装に実在すること（実装に無い文面を例にしない）。"""

    def test_every_quoted_message_in_the_reading_guide_exists_in_the_code(self):
        source = message_source()
        # コードブロックの出力例は、実装に放送させた行と比べる別のテストがある（引用符の中に
        # 値が入る文は、文面が一続きでは実装に無い）
        guide = without_fenced_blocks(section(doc(KNOWLEDGE_BASE_MD), "### 3-6.", "\n---\n"))
        self.assertTrue(guide)
        invented = ["「{0}」の「{1}」".format(quote, chunk) for quote, chunk in quoted_chunks(guide)
                    if chunk not in source]
        self.assertEqual(invented, [], "実装に無い文面が引用されています（表示される文面に直す）")


if __name__ == "__main__":
    unittest.main()
