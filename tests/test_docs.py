"""文書の記載と実装の一致を確かめるテスト（標準ライブラリのみ）。

文書に書いた ``--say`` の例が作り置きに無い文言だと、読者が試しても無音になる
（Pi には実行時の合成手段が無い）。版の表記が文書ごとにずれると、どれが最新か
分からなくなる。どちらも目で見つけにくいので、機械的に確かめる。
"""

from __future__ import annotations

import glob
import json
import os
import re
import unittest

from tests.support import REPO_ROOT

from chime import __version__, cli

MANIFEST_PATH = os.path.join(REPO_ROOT, "assets", "voice", "manifest.json")

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


if __name__ == "__main__":
    unittest.main()
