"""版のタグ付けスクリプト（``scripts/tag_releases.py``）のテスト。

ネットワークには出ない。git の履歴は、テストごとに一時ディレクトリへ作り捨てる
リポジトリで再現する（PR のマージは、機能ブランチで版を上げて main に
``git merge --no-ff`` する形で真似る）。手元や CI の git 設定に左右されないよう、
環境変数で git の設定を隔離している。

- 版や見出しを読む関数（``parse_version`` など）の単体テスト
- 実際の git リポジトリでの ``first_appearances`` と ``main``
  （履歴の読み取りの失敗・タグの作成の失敗・取りこぼしの警告を含む）
- ``.github/workflows/tag.yml`` の中身の静的な確認
"""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Dict, List, Optional, Tuple
from unittest import mock

from tests.support import REPO_ROOT

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import tag_releases  # noqa: E402

WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "tag.yml")
CI_WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")

#: コミットとタグの日時（実行した日時に左右されないよう固定する）。
FIXED_DATE = "2026-09-29T12:00:00+0900"


# -- 単体テスト（git を使わない） ----------------------------------------------------

class ParseVersionTest(unittest.TestCase):
    """``parse_version`` / ``VERSION_RE``: ``chime/__init__.py`` から版を読む。"""

    def test_reads_a_double_quoted_version(self):
        self.assertEqual(tag_releases.parse_version('__version__ = "5.3.0"\n'), "5.3.0")

    def test_reads_a_single_quoted_version(self):
        self.assertEqual(tag_releases.parse_version("__version__ = '5.3.0'\n"), "5.3.0")

    def test_whitespace_around_the_equal_sign_does_not_matter(self):
        for text in ('__version__="5.3.0"', '__version__ =  "5.3.0"',
                     '__version__\t=\t"5.3.0"', '__version__   =   "5.3.0"   '):
            with self.subTest(text=text):
                self.assertEqual(tag_releases.parse_version(text), "5.3.0")

    def test_finds_the_line_among_other_lines(self):
        text = '"""Campus Evening Chime System."""\n\nimport os\n\n__version__ = "6.0.0"\n\nX = 1\n'
        self.assertEqual(tag_releases.parse_version(text), "6.0.0")

    def test_multi_digit_parts_are_kept_whole(self):
        self.assertEqual(tag_releases.parse_version('__version__ = "10.20.30"'), "10.20.30")

    def test_a_trailing_comment_is_fine(self):
        self.assertEqual(tag_releases.parse_version('__version__ = "5.3.0"  # 版\n'), "5.3.0")

    def test_the_first_line_wins_when_there_are_two(self):
        text = '__version__ = "1.0.0"\n__version__ = "2.0.0"\n'
        self.assertEqual(tag_releases.parse_version(text), "1.0.0")

    def test_no_match(self):
        for text in (
            "",
            "import os\n",
            '__author__ = "x"\n',
            "__version__ = 5\n",
            '__version__ = "5.3"\n',              # 数字 2 つ
            '__version__ = "5.3.0.1"\n',          # 数字 4 つ
            '__version__ = "5.3.0rc1"\n',         # 版の後ろに文字
            '__version__ = "v5.3.0"\n',           # 版の前に文字
            '__version__ = "5.3.0\n',            # 引用符が閉じていない
            '    __version__ = "5.3.0"\n',        # インデントした行は拾わない
            'x = 1  # __version__ = "5.3.0"\n',   # 行の途中
            '__version__ =\n"5.3.0"\n',           # 改行をまたがない
        ):
            with self.subTest(text=text):
                self.assertIsNone(tag_releases.parse_version(text))

    def test_regex_captures_only_the_digits(self):
        match = tag_releases.VERSION_RE.search('__version__ = "5.3.0"')
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "5.3.0")
        self.assertTrue(tag_releases.VERSION_RE.flags & re.MULTILINE)


class VersionKeyTest(unittest.TestCase):

    def test_splits_into_integers(self):
        self.assertEqual(tag_releases.version_key("5.3.0"), (5, 3, 0))
        self.assertEqual(tag_releases.version_key("10.0.12"), (10, 0, 12))

    def test_compares_numerically_not_as_text(self):
        self.assertGreater(tag_releases.version_key("5.10.0"), tag_releases.version_key("5.9.0"))
        self.assertGreater(tag_releases.version_key("10.0.0"), tag_releases.version_key("9.9.9"))
        self.assertGreater(tag_releases.version_key("5.3.1"), tag_releases.version_key("5.3.0"))
        self.assertEqual(tag_releases.version_key("5.2.0"), tag_releases.version_key("5.2.0"))

    def test_sorts_versions_in_release_order(self):
        versions = ["5.10.0", "5.2.0", "6.0.0", "5.3.0", "3.0.0", "5.9.1"]
        self.assertEqual(sorted(versions, key=tag_releases.version_key),
                         ["3.0.0", "5.2.0", "5.3.0", "5.9.1", "5.10.0", "6.0.0"])

    def test_rejects_anything_that_is_not_x_y_z(self):
        for value in ("", "5", "5.3", "v5.3.0", "5.3.0.1", "5.3.x", "a.b.c", " 5.3.0",
                      "5.3.0\n", "5.3.0-rc1", "５.３.０"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    tag_releases.version_key(value)


class HasChangelogEntryTest(unittest.TestCase):

    CHANGELOG = (
        "# 変更履歴\n\n"
        "## [未リリース]\n\n"
        "## [5.3.0] - 2026-09-29\n\n- 変更\n\n"
        "## [5.2.0] - 2026-09-29\n\n- 変更\n\n"
        "[5.3.0]: https://example.com/releases/tag/v5.3.0\n"
        "[5.2.0]: https://example.com/releases/tag/v5.2.0\n"
    )

    def test_finds_the_heading(self):
        self.assertTrue(tag_releases.has_changelog_entry(self.CHANGELOG, "5.3.0"))
        self.assertTrue(tag_releases.has_changelog_entry(self.CHANGELOG, "5.2.0"))

    def test_a_missing_version_is_not_found(self):
        self.assertFalse(tag_releases.has_changelog_entry(self.CHANGELOG, "5.4.0"))
        self.assertFalse(tag_releases.has_changelog_entry(self.CHANGELOG, "6.0.0"))

    def test_a_prefix_of_the_version_does_not_match(self):
        self.assertFalse(tag_releases.has_changelog_entry("## [5.3.0] - 2026-09-29\n", "5.3"))
        self.assertFalse(tag_releases.has_changelog_entry("## [5.3.0] - 2026-09-29\n", "5"))

    def test_a_longer_version_does_not_match_a_shorter_heading(self):
        self.assertFalse(tag_releases.has_changelog_entry("## [5.3] - 2026-09-29\n", "5.3.0"))
        self.assertFalse(tag_releases.has_changelog_entry("## [5.3.0.1]\n", "5.3.0"))
        self.assertFalse(tag_releases.has_changelog_entry("## [15.3.0]\n", "5.3.0"))

    def test_the_dots_are_literal(self):
        self.assertFalse(tag_releases.has_changelog_entry("## [5x3x0]\n", "5.3.0"))

    def test_the_heading_must_start_the_line(self):
        self.assertFalse(tag_releases.has_changelog_entry("  ## [5.3.0]\n", "5.3.0"))
        self.assertFalse(tag_releases.has_changelog_entry("見出し ## [5.3.0]\n", "5.3.0"))

    def test_a_link_reference_is_not_a_heading(self):
        self.assertFalse(tag_releases.has_changelog_entry(
            "[5.3.0]: https://example.com/releases/tag/v5.3.0\n", "5.3.0"))

    def test_the_heading_may_be_the_whole_text_without_a_newline(self):
        self.assertTrue(tag_releases.has_changelog_entry("## [5.3.0]", "5.3.0"))

    def test_empty_text(self):
        self.assertFalse(tag_releases.has_changelog_entry("", "5.3.0"))


# -- git のリポジトリを作って試すテスト ---------------------------------------------------

def init_py(version: Optional[str]) -> str:
    """``chime/__init__.py`` の中身。``version`` が ``None`` なら版の行が無い。"""
    text = '"""テスト用のパッケージ。"""\n'
    if version is not None:
        text += '\n__version__ = "{0}"\n'.format(version)
    return text


def changelog(*versions: str) -> str:
    """``CHANGELOG.md`` の中身。古い順に渡した版の見出しを、新しい順に並べる。"""
    text = "# 変更履歴\n\n## [未リリース]\n\n"
    for version in reversed(versions):
        text += "## [{0}] - 2026-09-29\n\n- 変更\n\n".format(version)
    return text


@unittest.skipUnless(shutil.which("git"), "git がインストールされていません")
class GitRepoTestCase(unittest.TestCase):
    """作り捨ての git リポジトリ（``self.repo``）を用意する土台。

    git の設定（利用者・システムの設定、``GIT_*`` 環境変数）は隔離する。
    作ったディレクトリは、テストの終わりに必ず消す。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="tag-releases-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)

        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        # 開発者の環境（フックの中から実行したときの GIT_DIR など）を引き継がない。
        for name in [name for name in os.environ if name.startswith("GIT_")]:
            del os.environ[name]
        os.environ.pop("GITHUB_ACTIONS", None)
        os.environ.update({
            "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.com",
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.com",
            "GIT_AUTHOR_DATE": FIXED_DATE,
            "GIT_COMMITTER_DATE": FIXED_DATE,
            # 一時ディレクトリの外のリポジトリを、親をたどって拾わない。
            "GIT_CEILING_DIRECTORIES": self.tmp,
        })

        self.repo = os.path.join(self.tmp, "repo")
        self.init_repo(self.repo)

    # -- git の操作 --

    def git(self, *args: str, cwd: Optional[str] = None) -> str:
        """git を実行して標準出力（末尾の改行を除く）を返す。失敗したらテストを失敗させる。"""
        result = subprocess.run(["git"] + list(args), cwd=cwd or self.repo,
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            self.fail("git {0} が失敗しました: {1}".format(" ".join(args), result.stderr))
        return result.stdout.strip()

    def init_repo(self, path: str) -> None:
        os.makedirs(path)
        self.git("init", "-q", cwd=path)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=path)

    def write(self, files: Dict[str, str]) -> None:
        for relpath, text in files.items():
            path = os.path.join(self.repo, relpath)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)

    def commit(self, message: str, files: Dict[str, str]) -> str:
        """今のブランチにコミットして、その完全な sha を返す。"""
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def merge_pr(self, number: int, version: Optional[str], versions_in_changelog: List[str],
                 extra_commits: Tuple[Tuple[str, Dict[str, str]], ...] = ()) -> Tuple[str, str]:
        """PR のマージを真似る。``(版を上げたコミット, マージコミット)`` を返す。

        機能ブランチで版を上げ（と必要なら ``extra_commits``）、main に
        ``--no-ff`` でマージする。版を上げたコミットは main の first-parent の
        履歴には現れず、マージコミットだけが現れる。
        """
        branch = "feature-{0}".format(number)
        self.git("checkout", "-q", "-b", branch)
        bump = self.commit("v{0}: 機能ブランチでの変更".format(version), {
            "chime/__init__.py": init_py(version),
            "CHANGELOG.md": changelog(*versions_in_changelog),
        })
        for message, files in extra_commits:
            self.commit(message, files)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "Merge pull request #{0}".format(number), branch)
        return bump, self.git("rev-parse", "HEAD")

    def build_history(self) -> None:
        """版が上がっていく main の履歴を作り、``self.bump`` と ``self.merge`` に控える。

        ``chime/__init__.py`` が無い時代と、あっても版の行が無い時代があり、
        そのあと PR のマージで 3.0.0 → 5.2.0 → 5.3.0 と上がる。
        各マージの間に、PR を経ない直接のコミットも挟む。
        """
        self.base = self.commit("初期コミット", {"README.md": "# chime\n"})
        self.no_version = self.commit("版の行がない __init__.py", {
            "chime/__init__.py": init_py(None)})
        self.bump = {}
        self.merge = {}
        self.bump["3.0.0"], self.merge["3.0.0"] = self.merge_pr(1, "3.0.0", ["3.0.0"])
        self.commit("直接のコミット", {"docs/a.md": "a\n"})
        self.bump["5.2.0"], self.merge["5.2.0"] = self.merge_pr(
            2, "5.2.0", ["3.0.0", "5.2.0"])
        self.commit("直接のコミット その 2", {"docs/b.md": "b\n"})
        self.bump["5.3.0"], self.merge["5.3.0"] = self.merge_pr(
            3, "5.3.0", ["3.0.0", "5.2.0", "5.3.0"])

    def tags(self, repo: Optional[str] = None) -> List[str]:
        out = self.git("tag", "-l", cwd=repo)
        return sorted(out.split()) if out else []

    def tag_target(self, tag: str, repo: Optional[str] = None) -> str:
        """タグが指すコミットを、git に直接聞く（スクリプトの ``tag_commit`` に頼らない）。"""
        return self.git("rev-parse", "{0}^{{commit}}".format(tag), cwd=repo)

    # -- スクリプトの実行 --

    @staticmethod
    def git_calls(run: mock.Mock, subcommand: str) -> List[List[str]]:
        """``subprocess.run`` のモックに記録された、``git <subcommand> …`` の呼び出し。"""
        return [list(call.args[0]) for call in run.call_args_list
                if call.args[0][:2] == ["git", subcommand]]

    def run_main(self, *argv: str, repo: Optional[str] = None) -> Tuple[int, str, str]:
        """``main`` を呼んで ``(終了コード, 標準出力, 標準エラー)`` を返す。"""
        args = list(argv)
        if "--repo" not in args:
            args = ["--repo", repo or self.repo] + args
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = tag_releases.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def add_bare_remote(self, name: str = "origin", dirname: str = "remote.git") -> str:
        """空の bare リポジトリをリモートとして足し、そのパスを返す。"""
        path = os.path.join(self.tmp, dirname)
        os.makedirs(path)
        self.git("init", "-q", "--bare", cwd=path)
        self.git("remote", "add", name, path)
        return path


class ReadFileAtTest(GitRepoTestCase):

    def test_reads_the_file_as_of_that_commit(self):
        first = self.commit("1", {"chime/__init__.py": init_py("1.0.0")})
        second = self.commit("2", {"chime/__init__.py": init_py("2.0.0")})
        self.assertEqual(tag_releases.read_file_at(self.repo, first, "chime/__init__.py"),
                         init_py("1.0.0"))
        self.assertEqual(tag_releases.read_file_at(self.repo, second, "chime/__init__.py"),
                         init_py("2.0.0"))

    def test_a_missing_path_is_none(self):
        first = self.commit("1", {"README.md": "x\n"})
        self.assertIsNone(tag_releases.read_file_at(self.repo, first, "chime/__init__.py"))
        self.assertIsNone(tag_releases.read_file_at(self.repo, first, "CHANGELOG.md"))

    def test_a_path_added_later_is_none_at_an_older_commit(self):
        first = self.commit("1", {"README.md": "x\n"})
        second = self.commit("2", {"CHANGELOG.md": changelog("1.0.0")})
        self.assertIsNone(tag_releases.read_file_at(self.repo, first, "CHANGELOG.md"))
        self.assertEqual(tag_releases.read_file_at(self.repo, second, "CHANGELOG.md"),
                         changelog("1.0.0"))

    def test_decodes_utf8(self):
        sha = self.commit("1", {"CHANGELOG.md": "## [1.0.0] - 日本語の見出し\n"})
        self.assertEqual(tag_releases.read_file_at(self.repo, sha, "CHANGELOG.md"),
                         "## [1.0.0] - 日本語の見出し\n")

    def test_a_directory_is_an_error_not_an_absent_file(self):
        # ファイルの場所にディレクトリがある。「無い」ではなく「読めない」なので GitError
        sha = self.commit("1", {"chime/__init__.py": init_py("1.0.0")})
        with self.assertRaises(tag_releases.GitError):
            tag_releases.read_file_at(self.repo, sha, "chime")

    def test_an_unknown_commit_is_an_error_not_an_absent_file(self):
        self.commit("1", {"README.md": "x\n"})
        with self.assertRaises(tag_releases.GitError) as caught:
            tag_releases.read_file_at(self.repo, "0" * 40, "CHANGELOG.md")
        self.assertIn("CHANGELOG.md", str(caught.exception))

    def test_a_path_that_is_absent_at_a_commit_with_other_files_is_none(self):
        sha = self.commit("1", {"README.md": "x\n", "docs/a.md": "a\n"})
        self.assertIsNone(tag_releases.read_file_at(self.repo, sha, "docs/CHANGELOG.md"))
        self.assertIsNone(tag_releases.read_file_at(self.repo, sha, "chime/__init__.py"))


class TagCommitTest(GitRepoTestCase):

    def test_no_such_tag_is_none(self):
        self.commit("1", {"README.md": "x\n"})
        self.assertIsNone(tag_releases.tag_commit(self.repo, "v1.0.0"))

    def test_lightweight_tag(self):
        sha = self.commit("1", {"README.md": "x\n"})
        self.git("tag", "v1.0.0", sha)
        self.assertEqual(tag_releases.tag_commit(self.repo, "v1.0.0"), sha)

    def test_annotated_tag_is_peeled_to_the_commit(self):
        sha = self.commit("1", {"README.md": "x\n"})
        self.git("tag", "-a", "v1.0.0", sha, "-m", "v1.0.0")
        tag_object = self.git("rev-parse", "refs/tags/v1.0.0")
        self.assertNotEqual(tag_object, sha)   # タグ自体のオブジェクトはコミットとは別物
        self.assertEqual(tag_releases.tag_commit(self.repo, "v1.0.0"), sha)

    def test_a_branch_with_the_same_name_is_not_a_tag(self):
        sha = self.commit("1", {"README.md": "x\n"})
        self.git("branch", "v1.0.0", sha)
        self.assertIsNone(tag_releases.tag_commit(self.repo, "v1.0.0"))


class FirstAppearancesTest(GitRepoTestCase):

    def test_returns_the_merge_commits_oldest_first(self):
        self.build_history()
        found = tag_releases.first_appearances(self.repo)
        self.assertEqual(found, [("3.0.0", self.merge["3.0.0"]),
                                 ("5.2.0", self.merge["5.2.0"]),
                                 ("5.3.0", self.merge["5.3.0"])])

    def test_the_commit_that_bumped_the_version_on_the_branch_is_not_chosen(self):
        self.build_history()
        chosen = dict(tag_releases.first_appearances(self.repo))
        for version, bump in self.bump.items():
            self.assertNotEqual(chosen[version], bump, version)
            # 選ばれたのは、版を上げたコミットを取り込んだ（2 つの親を持つ）マージ
            parents = self.git("rev-list", "--parents", "-n", "1", chosen[version]).split()
            self.assertEqual(len(parents), 3, version)
            self.assertEqual(parents[2], bump, version)

    def test_shas_are_full_length(self):
        self.build_history()
        for _version, sha in tag_releases.first_appearances(self.repo):
            self.assertRegex(sha, r"^[0-9a-f]{40}$")

    def test_commits_before_the_version_file_existed_are_skipped(self):
        self.build_history()
        shas = [sha for _version, sha in tag_releases.first_appearances(self.repo)]
        self.assertNotIn(self.base, shas)
        self.assertNotIn(self.no_version, shas)

    def test_history_without_any_version_is_empty(self):
        self.commit("初期コミット", {"README.md": "# chime\n"})
        self.commit("版の行がない", {"chime/__init__.py": init_py(None),
                                     "CHANGELOG.md": changelog("1.0.0")})
        self.assertEqual(tag_releases.first_appearances(self.repo), [])

    def test_ref_limits_how_far_the_history_is_followed(self):
        self.build_history()
        found = tag_releases.first_appearances(self.repo, self.merge["5.2.0"])
        self.assertEqual([version for version, _ in found], ["3.0.0", "5.2.0"])

    def test_a_branch_name_works_as_ref_and_head_is_the_default(self):
        self.build_history()
        self.git("checkout", "-q", "-b", "elsewhere", self.merge["3.0.0"])
        self.assertEqual([v for v, _ in tag_releases.first_appearances(self.repo, "main")],
                         ["3.0.0", "5.2.0", "5.3.0"])
        self.assertEqual([v for v, _ in tag_releases.first_appearances(self.repo)], ["3.0.0"])

    def test_only_the_first_parent_chain_is_followed(self):
        # 機能ブランチ側のコミットは、版が入っていても候補にならない
        self.build_history()
        listed = set(self.git("rev-list", "--first-parent", "main").split())
        for bump in self.bump.values():
            self.assertNotIn(bump, listed)
        for _version, sha in tag_releases.first_appearances(self.repo):
            self.assertIn(sha, listed)

    def test_a_version_without_a_changelog_heading_is_not_recorded(self):
        self.build_history()
        self.merge_pr(4, "5.4.0", ["3.0.0", "5.2.0", "5.3.0"])   # 見出しを足し忘れた
        versions = [version for version, _ in tag_releases.first_appearances(self.repo)]
        self.assertEqual(versions, ["3.0.0", "5.2.0", "5.3.0"])

    def test_a_missing_changelog_file_means_nothing_is_recorded(self):
        self.commit("1", {"chime/__init__.py": init_py("1.0.0")})
        self.assertEqual(tag_releases.first_appearances(self.repo), [])

    def test_a_late_heading_is_recorded_where_the_heading_arrives(self):
        self.build_history()
        _bump, merge = self.merge_pr(4, "5.4.0", ["3.0.0", "5.2.0", "5.3.0"])
        late = self.commit("CHANGELOG に 5.4.0 の見出しを足す",
                           {"CHANGELOG.md": changelog("3.0.0", "5.2.0", "5.3.0", "5.4.0")})
        found = dict(tag_releases.first_appearances(self.repo))
        self.assertEqual(found["5.4.0"], late)
        self.assertNotEqual(found["5.4.0"], merge)

    def test_a_heading_that_arrives_before_the_version_does_not_count_by_itself(self):
        # 見出しだけ先に入り（版はまだ 5.3.0）、版は後の PR で上がる → 版が上がった所
        self.build_history()
        self.commit("見出しだけ先に足す",
                    {"CHANGELOG.md": changelog("3.0.0", "5.2.0", "5.3.0", "5.4.0")})
        _bump, merge = self.merge_pr(4, "5.4.0", ["3.0.0", "5.2.0", "5.3.0", "5.4.0"])
        found = dict(tag_releases.first_appearances(self.repo))
        self.assertEqual(found["5.4.0"], merge)

    def test_a_version_is_recorded_only_once_even_if_it_comes_back(self):
        self.build_history()
        # 5.3.0 → 5.2.0 に戻し（revert）、また 5.3.0 に上げる
        self.commit("5.2.0 に戻す", {"chime/__init__.py": init_py("5.2.0")})
        self.commit("また 5.3.0", {"chime/__init__.py": init_py("5.3.0")})
        found = tag_releases.first_appearances(self.repo)
        self.assertEqual([version for version, _ in found], ["3.0.0", "5.2.0", "5.3.0"])
        self.assertEqual(dict(found)["5.3.0"], self.merge["5.3.0"])
        self.assertEqual(dict(found)["5.2.0"], self.merge["5.2.0"])

    def test_a_branch_that_never_reached_main_is_not_followed(self):
        self.build_history()
        self.git("checkout", "-q", "-b", "side", "main")
        self.commit("side で 9.0.0", {"chime/__init__.py": init_py("9.0.0"),
                                      "CHANGELOG.md": changelog("9.0.0")})
        self.git("checkout", "-q", "main")
        versions = [version for version, _ in tag_releases.first_appearances(self.repo)]
        self.assertNotIn("9.0.0", versions)

    def test_an_unknown_ref_raises_git_error(self):
        self.build_history()
        with self.assertRaises(tag_releases.GitError):
            tag_releases.first_appearances(self.repo, "no-such-ref")

    def test_a_branch_named_like_a_file_is_still_read_as_a_ref(self):
        self.build_history()
        self.write({"main": "ファイル名と同じ名前\n"})   # ref の main とファイルの main
        found = tag_releases.first_appearances(self.repo, "main")
        self.assertEqual(len(found), 3)


class MainTest(GitRepoTestCase):

    def setUp(self):
        super().setUp()
        self.build_history()
        self.refs_before = self.git("for-each-ref", "--format=%(refname) %(objectname)")

    # -- タグが付く場所 --

    def test_tags_land_on_the_merge_commits(self):
        code, stdout, stderr = self.run_main()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0", "v5.3.0"])
        for version, merge in self.merge.items():
            self.assertEqual(self.tag_target("v" + version), merge, version)
            self.assertNotEqual(self.tag_target("v" + version), self.bump[version], version)

    def test_the_output_names_each_tag_with_its_short_sha_and_subject(self):
        _code, stdout, _stderr = self.run_main()
        for version, merge in self.merge.items():
            line = "v{0} を {1} に付けました（Merge pull request #".format(version, merge[:7])
            self.assertIn(line, stdout)
        self.assertEqual(len(stdout.splitlines()), 3)

    def test_only_tags_are_added_and_nothing_else_changes(self):
        head = self.git("rev-parse", "HEAD")
        self.run_main()
        added = [line for line in self.git("for-each-ref", "--format=%(refname) %(objectname)")
                 .splitlines() if line not in self.refs_before.splitlines()]
        self.assertEqual(len(added), 3)
        self.assertTrue(all(line.startswith("refs/tags/v") for line in added), added)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_the_script_does_not_set_a_git_identity(self):
        self.run_main()
        config = self.git("config", "--local", "--list")
        self.assertNotIn("user.name", config)
        self.assertNotIn("user.email", config)

    def test_tags_are_annotated_and_the_message_is_the_tag_name(self):
        self.run_main()
        for version in self.merge:
            tag = "v" + version
            self.assertEqual(self.git("cat-file", "-t", tag), "tag", tag)
            self.assertEqual(self.git("for-each-ref", "refs/tags/" + tag,
                                      "--format=%(contents)"), tag)
            self.assertEqual(self.git("for-each-ref", "refs/tags/" + tag,
                                      "--format=%(objecttype)"), "tag")

    def test_a_version_without_a_heading_is_not_tagged(self):
        self.merge_pr(4, "5.4.0", ["3.0.0", "5.2.0", "5.3.0"])
        code, stdout, _stderr = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0", "v5.3.0"])
        self.assertNotIn("5.4.0", stdout)

    def test_when_the_heading_arrives_later_the_tag_goes_there(self):
        _bump, merge = self.merge_pr(4, "5.4.0", ["3.0.0", "5.2.0", "5.3.0"])
        self.run_main()
        self.assertNotIn("v5.4.0", self.tags())
        late = self.commit("CHANGELOG に 5.4.0 の見出しを足す",
                           {"CHANGELOG.md": changelog("3.0.0", "5.2.0", "5.3.0", "5.4.0")})
        code, stdout, _stderr = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.tag_target("v5.4.0"), late)
        self.assertNotEqual(self.tag_target("v5.4.0"), merge)
        # 先の 3 つは付いたまま動かない
        for version, sha in self.merge.items():
            self.assertEqual(self.tag_target("v" + version), sha)
        self.assertIn("v5.4.0 を {0} に付けました".format(late[:7]), stdout)

    # -- --since / --ref --

    def test_since_excludes_older_versions(self):
        code, stdout, _stderr = self.run_main("--since", "5.2.0")
        self.assertEqual(code, 0)
        self.assertEqual(self.tags(), ["v5.2.0", "v5.3.0"])
        self.assertNotIn("v3.0.0", stdout)

    def test_since_includes_the_boundary_version_itself(self):
        self.run_main("--since", "5.3.0")
        self.assertEqual(self.tags(), ["v5.3.0"])

    def test_since_compares_numbers_not_text(self):
        self.merge_pr(4, "5.10.0", ["3.0.0", "5.2.0", "5.3.0", "5.10.0"])
        self.run_main("--since", "5.9.0")
        self.assertEqual(self.tags(), ["v5.10.0"])

    def test_since_later_than_everything_tags_nothing(self):
        code, stdout, stderr = self.run_main("--since", "9.0.0")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.tags(), [])
        # 版は見つかっている。「CHANGELOG の見出しが無い」とは言わない
        self.assertIn("--since 9.0.0 以降の版はまだありません", stdout)
        self.assertIn("5.3.0", stdout)
        self.assertNotIn("CHANGELOG", stdout)
        self.assertNotIn("対象になる版が見つかりませんでした", stdout)

    def test_the_latest_version_in_the_since_message_is_compared_as_numbers(self):
        """「見つかった最新の版」は、版を数として比べて選ぶこと（5.10.0 が 5.9.0 より新しい）。"""
        self.merge_pr(4, "5.9.0", ["3.0.0", "5.2.0", "5.3.0", "5.9.0"])
        self.merge_pr(5, "5.10.0", ["3.0.0", "5.2.0", "5.3.0", "5.9.0", "5.10.0"])
        code, stdout, _stderr = self.run_main("--since", "9.0.0")
        self.assertEqual(code, 0)
        self.assertIn("（見つかった最新の版は 5.10.0）", stdout)

    def test_the_latest_version_in_the_since_message_is_the_newest_not_the_last_found(self):
        """「見つかった最新の版」は、履歴で最後に見つけた版ではなく、いちばん新しい版であること。"""
        self.merge_pr(4, "6.0.0", ["3.0.0", "5.2.0", "5.3.0", "6.0.0"])
        self.merge_pr(5, "5.9.1", ["3.0.0", "5.2.0", "5.3.0", "6.0.0", "5.9.1"])   # 版が戻っている
        code, stdout, _stderr = self.run_main("--since", "9.0.0")
        self.assertEqual(code, 0)
        self.assertIn("（見つかった最新の版は 6.0.0）", stdout)

    def test_ref_limits_the_history(self):
        code, _stdout, _stderr = self.run_main("--ref", self.merge["5.2.0"])
        self.assertEqual(code, 0)
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0"])

    # -- 既にあるタグ・冪等 --

    def test_an_existing_tag_at_the_right_commit_is_left_alone(self):
        self.git("tag", "-a", "v5.2.0", self.merge["5.2.0"], "-m", "手で付けたタグ")
        before = self.git("rev-parse", "refs/tags/v5.2.0")
        code, stdout, stderr = self.run_main("--since", "5.2.0")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.git("rev-parse", "refs/tags/v5.2.0"), before)
        self.assertEqual(self.git("for-each-ref", "refs/tags/v5.2.0", "--format=%(contents)"),
                         "手で付けたタグ")
        self.assertIn("v5.2.0 は付いています（{0}）".format(self.merge["5.2.0"][:7]), stdout)
        self.assertIn("v5.3.0 を {0} に付けました".format(self.merge["5.3.0"][:7]), stdout)

    def test_a_lightweight_tag_at_the_right_commit_is_also_left_alone(self):
        self.git("tag", "v5.2.0", self.merge["5.2.0"])
        code, stdout, _stderr = self.run_main("--since", "5.2.0")
        self.assertEqual(code, 0)
        self.assertEqual(self.git("cat-file", "-t", "v5.2.0"), "commit")   # 注釈付きに直さない
        self.assertIn("v5.2.0 は付いています", stdout)

    def test_a_second_run_creates_nothing(self):
        self.run_main()
        after_first = self.git("for-each-ref", "refs/tags", "--format=%(refname) %(objectname)")
        code, stdout, stderr = self.run_main()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.git("for-each-ref", "refs/tags", "--format=%(refname) %(objectname)"),
                         after_first)
        self.assertIn("新しく付けるタグはありません", stdout)
        self.assertNotIn("に付けました", stdout)
        for version, merge in self.merge.items():
            self.assertIn("v{0} は付いています（{1}）".format(version, merge[:7]), stdout)

    def test_a_new_release_only_adds_the_new_tag(self):
        self.run_main()
        before = self.git("for-each-ref", "refs/tags", "--format=%(refname) %(objectname)")
        _bump, merge = self.merge_pr(4, "5.4.0", ["3.0.0", "5.2.0", "5.3.0", "5.4.0"])
        code, stdout, _stderr = self.run_main()
        self.assertEqual(code, 0)
        after = self.git("for-each-ref", "refs/tags", "--format=%(refname) %(objectname)")
        self.assertEqual(sorted(set(after.splitlines()) - set(before.splitlines())),
                         [line for line in after.splitlines() if line.startswith("refs/tags/v5.4.0 ")])
        self.assertTrue(set(before.splitlines()) <= set(after.splitlines()))
        self.assertEqual(self.tag_target("v5.4.0"), merge)

    def test_an_existing_tag_at_another_commit_is_not_moved(self):
        wrong = self.merge["3.0.0"]
        self.git("tag", "v5.2.0", wrong)
        code, stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.tag_target("v5.2.0"), wrong)     # 動かさない
        self.assertEqual(self.git("cat-file", "-t", "v5.2.0"), "commit")
        # 警告を出す（何が付いていて、本来どこか）
        self.assertIn("警告", stderr)
        self.assertIn("v5.2.0", stderr)
        self.assertIn(wrong[:7], stderr)
        self.assertIn(self.merge["5.2.0"][:7], stderr)
        self.assertNotIn("::warning::", stdout + stderr)
        # 他のタグは付く
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0", "v5.3.0"])
        self.assertEqual(self.tag_target("v5.3.0"), self.merge["5.3.0"])
        self.assertNotIn("v5.2.0 を", stdout)

    def test_a_moved_annotated_tag_is_not_touched_either(self):
        wrong = self.merge["3.0.0"]
        self.git("tag", "-a", "v5.2.0", wrong, "-m", "別の場所")
        before = self.git("rev-parse", "refs/tags/v5.2.0")
        code, _stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.git("rev-parse", "refs/tags/v5.2.0"), before)
        self.assertIn("警告", stderr)

    def test_a_tag_is_never_created_with_force_even_if_the_precheck_is_fooled(self):
        """既にあるタグの確認をすり抜けても、タグの作成は強制（-f）にせず、既にあるタグを動かさない。

        確認（``tag_commit``）が「無い」と答える状況を作る。作成の git が別の場所の既存タグを
        上書きしないで失敗し、1 件の失敗として終了コード 1 になること。
        """
        self.git("tag", "v5.2.0", self.merge["3.0.0"])   # 別のコミットに付いている
        before = self.git("rev-parse", "refs/tags/v5.2.0")
        with mock.patch.object(tag_releases, "tag_commit", return_value=None):
            code, _stdout, stderr = self.run_main("--since", "5.2.0")
        self.assertEqual(code, 1)
        self.assertEqual(self.git("rev-parse", "refs/tags/v5.2.0"), before)   # 動いていない
        self.assertEqual(self.tag_target("v5.2.0"), self.merge["3.0.0"])
        self.assertIn("v5.2.0 を付けられませんでした", stderr)

    def test_the_warning_is_a_github_actions_annotation_on_actions(self):
        self.git("tag", "v5.2.0", self.merge["3.0.0"])
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            code, stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        warnings = [line for line in stdout.splitlines() if "v5.2.0" in line
                    and line.startswith("::warning::")]
        self.assertEqual(len(warnings), 1, stdout)
        self.assertIn(self.merge["5.2.0"][:7], warnings[0])
        self.assertEqual(stderr, "")
        self.assertEqual(self.tag_target("v5.2.0"), self.merge["3.0.0"])

    def test_other_values_of_github_actions_do_not_switch_the_format(self):
        self.git("tag", "v5.2.0", self.merge["3.0.0"])
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            _code, stdout, stderr = self.run_main()
        self.assertNotIn("::warning::", stdout + stderr)
        self.assertIn("警告", stderr)

    # -- --dry-run --

    def test_dry_run_creates_no_tags(self):
        code, stdout, stderr = self.run_main("--dry-run")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.tags(), [])
        self.assertEqual(self.git("for-each-ref", "--format=%(refname) %(objectname)"),
                         self.refs_before)
        for version, merge in self.merge.items():
            self.assertIn("v{0} を {1} に付ける予定です".format(version, merge[:7]), stdout)
        self.assertIn("何も作成していません", stdout)
        self.assertNotIn("付けました", stdout)

    def test_dry_run_still_reports_the_existing_tags(self):
        self.git("tag", "v5.2.0", self.merge["5.2.0"])
        self.git("tag", "v3.0.0", self.merge["5.3.0"])   # 場所違い
        code, stdout, stderr = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("v5.2.0 は付いています", stdout)
        self.assertIn("警告", stderr)
        self.assertIn("v5.3.0 を", stdout)
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0"])

    def test_dry_run_with_nothing_to_do_says_so(self):
        self.run_main()
        code, stdout, _stderr = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("新しく付けるタグはありません", stdout)

    def test_dry_run_never_pushes(self):
        remote = self.add_bare_remote()
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            code, stdout, _stderr = self.run_main("--dry-run", "--push")
        self.assertEqual(code, 0)
        self.assertEqual(self.tags(), [])
        self.assertEqual(self.tags(remote), [])
        self.assertEqual(self.git_calls(run, "push"), [])
        self.assertIn("push もしません", stdout)

    # -- --push --

    def test_push_sends_exactly_the_new_tags_in_one_command(self):
        remote = self.add_bare_remote()
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            code, stdout, stderr = self.run_main("--push")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.tags(remote), ["v3.0.0", "v5.2.0", "v5.3.0"])
        for version, merge in self.merge.items():
            self.assertEqual(self.tag_target("v" + version, remote), merge)
            self.assertEqual(self.git("cat-file", "-t", "v" + version, cwd=remote), "tag")
        pushes = self.git_calls(run, "push")
        self.assertEqual(pushes, [["git", "push", "origin", "refs/tags/v3.0.0",
                                   "refs/tags/v5.2.0", "refs/tags/v5.3.0"]])
        self.assertIn("3 件のタグを origin に push しました", stdout)

    def test_push_does_not_resend_tags_made_in_an_earlier_run(self):
        # 前回までに（ローカルに）付いていたタグは、今回作ったものではないので送らない
        remote = self.add_bare_remote()
        self.git("tag", "-a", "v5.2.0", self.merge["5.2.0"], "-m", "v5.2.0")
        code, _stdout, _stderr = self.run_main("--since", "5.2.0", "--push")
        self.assertEqual(code, 0)
        self.assertEqual(self.tags(remote), ["v5.3.0"])

    def test_nothing_new_means_no_push_call(self):
        self.add_bare_remote()
        self.run_main("--push")
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            code, stdout, stderr = self.run_main("--push")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.git_calls(run, "push"), [])
        self.assertIn("新しく付けるタグはありません", stdout)

    def test_without_push_the_remote_is_left_alone(self):
        remote = self.add_bare_remote()
        self.run_main()
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0", "v5.3.0"])
        self.assertEqual(self.tags(remote), [])

    def test_remote_names_the_destination(self):
        origin = self.add_bare_remote("origin", "origin.git")
        upstream = self.add_bare_remote("upstream", "upstream.git")
        code, stdout, _stderr = self.run_main("--push", "--remote", "upstream")
        self.assertEqual(code, 0)
        self.assertEqual(self.tags(upstream), ["v3.0.0", "v5.2.0", "v5.3.0"])
        self.assertEqual(self.tags(origin), [])
        self.assertIn("upstream に push しました", stdout)

    def test_push_failure_returns_1(self):
        missing = os.path.join(self.tmp, "does-not-exist.git")
        self.git("remote", "add", "origin", missing)
        code, stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        self.assertIn("push できませんでした", stderr)
        self.assertIn("origin", stderr)
        self.assertNotIn("push しました", stdout)
        # タグ自体はローカルに作ってある（再実行では「付いています」になる）
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0", "v5.3.0"])
        # 手で送り直すための、そのまま打てるコマンドを示す
        self.assertIn("手で送るには: git push origin "
                      "refs/tags/v3.0.0 refs/tags/v5.2.0 refs/tags/v5.3.0", stderr)

    def test_an_unknown_remote_returns_1(self):
        code, _stdout, stderr = self.run_main("--push", "--remote", "nowhere")
        self.assertEqual(code, 1)
        self.assertIn("nowhere", stderr)

    def test_a_tag_that_differs_on_the_remote_is_never_forced(self):
        remote = self.add_bare_remote()
        self.git("push", "-q", "origin", "main")
        self.git("tag", "v5.3.0", self.merge["3.0.0"], cwd=remote)   # リモートでは別の場所
        code, _stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        self.assertIn("push できませんでした", stderr)
        self.assertEqual(self.tag_target("v5.3.0", remote), self.merge["3.0.0"])

    # -- 作成の失敗 --

    def test_a_failure_to_create_a_tag_returns_1_and_pushes_nothing(self):
        remote = self.add_bare_remote()
        # 利用者名・メールが決まらない環境（Actions で git config を忘れた場合）を再現する
        self.git("config", "user.useConfigOnly", "true")
        for name in ("GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            del os.environ[name]
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            code, stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        # 1 つ失敗しても残りも試す（どれも同じ理由で失敗する）。作れたタグが無いので push しない
        for tag in ("v3.0.0", "v5.2.0", "v5.3.0"):
            self.assertIn("{0} を付けられませんでした".format(tag), stderr)
        self.assertEqual(self.tags(), [])
        self.assertEqual(self.tags(remote), [])
        self.assertEqual(self.git_calls(run, "push"), [])
        self.assertNotIn("手で送るには", stderr)

    # -- 実行の仕方 --

    def test_git_is_never_run_through_a_shell(self):
        self.add_bare_remote()
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            self.run_main("--push")
        self.assertGreater(run.call_count, 0)
        for call in run.call_args_list:
            self.assertIsInstance(call.args[0], list)
            self.assertEqual(call.args[0][0], "git")
            self.assertFalse(call.kwargs.get("shell", False))
            self.assertEqual(call.kwargs.get("cwd"), self.repo)

    def test_the_repository_defaults_to_the_current_directory(self):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.repo)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = tag_releases.main(["--since", "5.3.0"])
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        self.assertEqual(self.tags(), ["v5.3.0"])

    def test_runs_from_a_subdirectory_of_the_repository(self):
        code, _stdout, stderr = self.run_main(repo=os.path.join(self.repo, "docs"))
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0", "v5.3.0"])

    def test_the_script_runs_as_a_command(self):
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "tag_releases.py"),
             "--repo", self.repo, "--since", "5.3.0", "--dry-run"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("v5.3.0 を", result.stdout)
        self.assertEqual(self.tags(), [])


class UnreadableHistoryTest(GitRepoTestCase):
    """履歴のオブジェクトが読めないときは、「ファイルが無い」扱いにせず、何も作らずに失敗する。

    読めないのを「無い」と見てしまうと、タグがもっと後のコミットに付き、しかもそれは
    後から直せない（既にあるタグは動かさない）。
    """

    def setUp(self):
        super().setUp()
        self.build_history()
        self.remote = self.add_bare_remote()

    def break_blob(self, commit: str, path: str) -> str:
        """``commit`` 時点の ``path`` のゆるい（loose）オブジェクトを消す。消した blob の id を返す。"""
        blob = self.git("rev-parse", "{0}:{1}".format(commit, path))
        loose = os.path.join(self.repo, ".git", "objects", blob[:2], blob[2:])
        # まとめられて（pack）いたら消せない。作ったばかりのリポジトリなので、ゆるいはず
        self.assertTrue(os.path.isfile(loose), "ゆるいオブジェクトがありません: " + loose)
        os.chmod(loose, 0o644)
        os.remove(loose)
        broken = subprocess.run(["git", "cat-file", "blob", blob], cwd=self.repo,
                                capture_output=True)
        self.assertNotEqual(broken.returncode, 0)   # 本当に読めなくなった
        return blob

    def assert_nothing_was_done(self, run: mock.Mock) -> None:
        self.assertEqual(self.tags(), [])
        self.assertEqual(self.tags(self.remote), [])
        self.assertEqual(self.git_calls(run, "tag"), [])
        self.assertEqual(self.git_calls(run, "push"), [])

    def test_a_missing_changelog_blob_stops_everything_with_exit_1(self):
        self.break_blob(self.merge["5.2.0"], "CHANGELOG.md")
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            code, stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("CHANGELOG.md", stderr)
        self.assertIn(self.merge["5.2.0"][:7], stderr)
        self.assertIn("読み取れませんでした", stderr)
        self.assert_nothing_was_done(run)   # 3.0.0 は読めていても、途中までは付けない

    def test_a_missing_version_file_blob_stops_everything_with_exit_1(self):
        self.break_blob(self.merge["3.0.0"], "chime/__init__.py")
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            code, stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("chime/__init__.py", stderr)
        self.assert_nothing_was_done(run)

    def test_dry_run_fails_the_same_way(self):
        self.break_blob(self.merge["5.2.0"], "CHANGELOG.md")
        code, stdout, stderr = self.run_main("--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("CHANGELOG.md", stderr)

    def test_it_is_not_silently_pushed_to_a_later_commit(self):
        # 5.2.0 のマージの CHANGELOG が読めないまま、後のコミット（5.3.0 のマージ）で
        # 5.2.0 の見出しが読めるようになっていても、5.2.0 を後のコミットに付けない
        self.break_blob(self.merge["5.2.0"], "CHANGELOG.md")
        self.run_main()
        self.assertNotIn("v5.2.0", self.tags())
        self.assertNotIn("v5.3.0", self.tags())

    def test_first_appearances_and_read_file_at_raise_git_error(self):
        self.break_blob(self.merge["5.2.0"], "CHANGELOG.md")
        with self.assertRaises(tag_releases.GitError) as caught:
            tag_releases.read_file_at(self.repo, self.merge["5.2.0"], "CHANGELOG.md")
        self.assertIn("CHANGELOG.md", str(caught.exception))
        with self.assertRaises(tag_releases.GitError):
            tag_releases.first_appearances(self.repo)

    def test_the_message_names_the_short_sha_the_file_and_gits_own_reason(self):
        """読めなかったときの知らせに、短い sha・ファイル名・git 自身の説明が入っていること。

        どのコミットのどのファイルが、なぜ読めないのかが分からないと、欠けたオブジェクトを探せない。
        """
        sha = self.merge["5.2.0"]
        self.break_blob(sha, "CHANGELOG.md")
        reason = subprocess.run(["git", "cat-file", "blob", "{0}:CHANGELOG.md".format(sha)],
                                cwd=self.repo, capture_output=True, text=True,
                                encoding="utf-8").stderr.strip()
        self.assertNotEqual(reason, "")   # git は理由を言っているはず
        code, stdout, stderr = self.run_main()
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("{0} の CHANGELOG.md を読み取れませんでした".format(sha[:7]), stderr)
        self.assertIn(reason, stderr)

    def test_when_git_says_nothing_the_exit_code_is_shown_instead(self):
        """git が何も説明せずに失敗したときは、説明の代わりに終了コードを知らせること。"""
        silent = subprocess.CompletedProcess(["git"], 3, stdout="", stderr="\n")
        with mock.patch.object(tag_releases.subprocess, "run", return_value=silent):
            with self.assertRaises(tag_releases.GitError) as caught:
                tag_releases.read_file_at(self.repo, self.merge["5.2.0"], "CHANGELOG.md")
        self.assertIn("git が終了コード 3 で失敗しました", str(caught.exception))

    def test_files_that_are_truly_absent_are_still_skipped(self):
        # 履歴の前半は chime/__init__.py も CHANGELOG.md も無い。それは「無い」だけで失敗ではない
        code, _stdout, stderr = self.run_main()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0", "v5.3.0"])

    def test_a_git_error_during_the_scan_returns_1_and_creates_nothing(self):
        error = tag_releases.GitError("履歴を読めませんでした（テスト）")
        with mock.patch.object(tag_releases, "_scan", side_effect=error), \
                mock.patch.object(tag_releases.subprocess, "run", wraps=subprocess.run) as run:
            code, stdout, stderr = self.run_main("--push")
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr, "履歴を読めませんでした（テスト）\n")
        self.assert_nothing_was_done(run)


class TagCreationFailureTest(GitRepoTestCase):
    """タグを 1 つ作れなくても、ほかのタグは付けて push し、最後に終了コード 1 にする。"""

    def setUp(self):
        super().setUp()
        self.build_history()
        self.remote = self.add_bare_remote()

    def lock(self, tag: str) -> str:
        """``refs/tags/<tag>.lock`` を置いて、そのタグだけ git に作らせない。"""
        path = os.path.join(self.repo, ".git", "refs", "tags", tag + ".lock")
        with open(path, "w", encoding="utf-8"):
            pass
        return path

    def test_the_other_tags_are_still_created_and_pushed(self):
        self.lock("v5.2.0")
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            code, stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        self.assertEqual(self.tags(), ["v3.0.0", "v5.3.0"])
        self.assertEqual(self.tags(self.remote), ["v3.0.0", "v5.3.0"])
        self.assertEqual(self.tag_target("v3.0.0", self.remote), self.merge["3.0.0"])
        self.assertEqual(self.tag_target("v5.3.0", self.remote), self.merge["5.3.0"])
        # 作れたものを 1 回の git push でまとめて送る（作れなかったものは含めない）
        self.assertEqual(self.git_calls(run, "push"),
                         [["git", "push", "origin", "refs/tags/v3.0.0", "refs/tags/v5.3.0"]])
        self.assertIn("v3.0.0 を {0} に付けました".format(self.merge["3.0.0"][:7]), stdout)
        self.assertIn("v5.3.0 を {0} に付けました".format(self.merge["5.3.0"][:7]), stdout)
        self.assertIn("2 件のタグを origin に push しました", stdout)

    def test_stderr_names_the_failed_tag_with_gits_message(self):
        self.lock("v5.2.0")
        _code, _stdout, stderr = self.run_main()
        failures = [line for line in stderr.splitlines() if "を付けられませんでした:" in line]
        self.assertEqual(len(failures), 1, stderr)
        self.assertTrue(failures[0].startswith("v5.2.0 を付けられませんでした: "), failures[0])
        # git 自身の説明（どのロックで失敗したか）が続く
        self.assertIn("v5.2.0.lock", stderr)
        self.assertIn("1 件のタグを付けられませんでした（v5.2.0）", stderr)
        self.assertNotIn("v3.0.0 を付けられませんでした", stderr)
        self.assertNotIn("v5.3.0 を付けられませんでした", stderr)

    def test_without_push_the_others_are_created_locally_and_exit_is_1(self):
        self.lock("v3.0.0")
        code, stdout, _stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.tags(), ["v5.2.0", "v5.3.0"])
        self.assertEqual(self.tags(self.remote), [])
        self.assertNotIn("push", stdout)

    def test_when_every_tag_fails_nothing_is_pushed(self):
        for tag in ("v3.0.0", "v5.2.0", "v5.3.0"):
            self.lock(tag)
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            code, _stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        self.assertEqual(self.tags(), [])
        self.assertEqual(self.git_calls(run, "push"), [])
        for tag in ("v3.0.0", "v5.2.0", "v5.3.0"):
            self.assertIn("{0} を付けられませんでした".format(tag), stderr)
        self.assertIn("3 件のタグを付けられませんでした", stderr)

    def test_a_failed_push_after_a_failed_creation_is_also_exit_1(self):
        self.lock("v5.2.0")
        self.git("remote", "set-url", "origin", os.path.join(self.tmp, "does-not-exist.git"))
        code, _stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        self.assertIn("v5.2.0 を付けられませんでした", stderr)
        self.assertIn("push できませんでした", stderr)
        # 手で送るコマンドには、作れたタグだけが並ぶ
        self.assertIn("手で送るには: git push origin refs/tags/v3.0.0 refs/tags/v5.3.0\n", stderr)
        manual = [line for line in stderr.splitlines() if line.startswith("手で送るには")]
        self.assertEqual(len(manual), 1)
        self.assertNotIn("v5.2.0", manual[0])

    def test_after_the_cause_is_fixed_a_rerun_adds_only_the_missing_tag(self):
        lock = self.lock("v5.2.0")
        self.run_main("--push")
        os.remove(lock)
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            code, stdout, stderr = self.run_main("--push")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0", "v5.3.0"])
        self.assertEqual(self.tags(self.remote), ["v3.0.0", "v5.2.0", "v5.3.0"])
        self.assertEqual(self.tag_target("v5.2.0"), self.merge["5.2.0"])
        self.assertEqual(self.git_calls(run, "push"),
                         [["git", "push", "origin", "refs/tags/v5.2.0"]])
        self.assertIn("v3.0.0 は付いています", stdout)

    def test_the_failure_does_not_touch_the_tags_that_already_exist(self):
        self.git("tag", "-a", "v3.0.0", self.merge["3.0.0"], "-m", "手で付けたタグ")
        before = self.git("rev-parse", "refs/tags/v3.0.0")
        self.lock("v5.3.0")
        code, _stdout, _stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.git("rev-parse", "refs/tags/v3.0.0"), before)
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0"])


class PushDiagnosticsTest(GitRepoTestCase):
    """push の失敗の知らせ方と、push を呼ぶときの環境。"""

    def setUp(self):
        super().setUp()
        self.build_history()

    def test_the_manual_command_can_be_run_as_printed(self):
        missing = os.path.join(self.tmp, "later.git")
        self.git("remote", "add", "origin", missing)
        code, _stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        lines = [line for line in stderr.splitlines() if line.startswith("手で送るには: ")]
        self.assertEqual(lines, ["手で送るには: git push origin "
                                 "refs/tags/v3.0.0 refs/tags/v5.2.0 refs/tags/v5.3.0"])
        # 送り先を用意してから、表示されたとおりに打てばタグが届く
        os.makedirs(missing)
        self.git("init", "-q", "--bare", cwd=missing)
        command = lines[0][len("手で送るには: "):]
        pushed = subprocess.run(shlex.split(command), cwd=self.repo, capture_output=True,
                                text=True, encoding="utf-8")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(self.tags(missing), ["v3.0.0", "v5.2.0", "v5.3.0"])

    def test_the_manual_command_uses_the_remote_that_was_asked_for(self):
        self.git("remote", "add", "upstream", os.path.join(self.tmp, "nowhere.git"))
        _code, _stdout, stderr = self.run_main("--push", "--remote", "upstream")
        self.assertIn("手で送るには: git push upstream refs/tags/v3.0.0", stderr)

    def test_a_failed_push_says_the_tags_exist_locally(self):
        """push に失敗したときは、タグは手元に作ってあることを、タグの一覧つきで知らせること。

        これが無いと、タグが作れなかったのか、送れなかっただけなのかが分からない。
        """
        self.git("remote", "add", "origin", os.path.join(self.tmp, "does-not-exist.git"))
        code, _stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        self.assertIn("タグはこのリポジトリには作ってあります（v3.0.0, v5.2.0, v5.3.0）。", stderr)

    def test_a_failed_push_carries_gits_reason(self):
        """push の失敗の知らせに、git 自身の説明（なぜ送れなかったか）が入っていること。"""
        missing = os.path.join(self.tmp, "does-not-exist.git")
        self.git("remote", "add", "origin", missing)
        code, _stdout, stderr = self.run_main("--push")
        self.assertEqual(code, 1)
        # 同じ push を git に直接させて、git の言い分を取る（タグは今できている）
        reason = subprocess.run(
            ["git", "push", "origin", "refs/tags/v3.0.0", "refs/tags/v5.2.0", "refs/tags/v5.3.0"],
            cwd=self.repo, capture_output=True, text=True, encoding="utf-8").stderr.strip()
        self.assertIn(missing, reason)   # 送り先が無い、という説明のはず
        self.assertIn("タグを origin に push できませんでした: " + reason, stderr)

    def test_the_manual_command_quotes_a_remote_with_a_space(self):
        """手で送るコマンドは、空白を含む送り先を引用符で囲み、打てば 1 つの送り先として読まれること。"""
        remote = os.path.join(self.tmp, "no such dir", "r.git")
        code, _stdout, stderr = self.run_main("--push", "--remote", remote)
        self.assertEqual(code, 1)
        lines = [line for line in stderr.splitlines() if line.startswith("手で送るには: ")]
        self.assertEqual(len(lines), 1, stderr)
        self.assertEqual(shlex.split(lines[0][len("手で送るには: "):]),
                         ["git", "push", remote,
                          "refs/tags/v3.0.0", "refs/tags/v5.2.0", "refs/tags/v5.3.0"])

    def test_no_manual_command_when_the_push_succeeded(self):
        self.add_bare_remote()
        _code, stdout, stderr = self.run_main("--push")
        self.assertNotIn("手で送るには", stdout + stderr)

    def test_the_push_never_waits_for_a_password(self):
        self.add_bare_remote()
        with mock.patch.object(tag_releases.subprocess, "run",
                               wraps=subprocess.run) as run:
            code, _stdout, _stderr = self.run_main("--push")
        self.assertEqual(code, 0)
        pushes = [call for call in run.call_args_list if call.args[0][:2] == ["git", "push"]]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0].kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        # このプロセスの環境そのものは書き換えない
        self.assertNotIn("GIT_TERMINAL_PROMPT", os.environ)


class WarningTest(GitRepoTestCase):
    """タグが付かないまま黙って過ぎてしまう取りこぼしは、警告で知らせる（終了コードは 0 のまま）。"""

    def setUp(self):
        super().setUp()
        self.build_history()

    ANNOTATED = '"""テスト用のパッケージ。"""\n\n__version__: str = "6.1.0"\n'

    def warning_lines(self, text: str) -> List[str]:
        return [line for line in text.splitlines() if "警告" in line or "::warning::" in line]

    def run_on_actions(self, *argv: str) -> Tuple[int, str, str]:
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            return self.run_main(*argv)

    # -- 警告が出ない場合 --

    def test_a_normal_history_has_no_warning(self):
        for argv in ((), ("--since", "5.2.0"), ("--dry-run",), ("--dry-run", "--since", "5.2.0")):
            with self.subTest(argv=argv):
                code, stdout, stderr = self.run_main(*argv)
                self.assertEqual((code, stderr), (0, ""))
                self.assertNotIn("::warning::", stdout)
                self.assertNotIn("警告", stdout)

    def test_a_normal_history_has_no_annotation_on_actions_either(self):
        code, stdout, stderr = self.run_on_actions("--since", "5.2.0")
        self.assertEqual((code, stderr), (0, ""))
        self.assertNotIn("::warning::", stdout)

    # -- (a) 先頭のコミットで版を読めない --

    def test_a_tip_with_an_annotated_version_warns_and_exits_0(self):
        self.commit("型注釈つきの __version__", {"chime/__init__.py": self.ANNOTATED})
        code, stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        warnings = self.warning_lines(stderr)
        self.assertEqual(len(warnings), 1, stderr)
        self.assertIn('__version__ = "X.Y.Z"', warnings[0])
        self.assertIn("HEAD", warnings[0])
        # これまでの版には、いつもどおりタグが付く
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0", "v5.3.0"])
        self.assertNotIn("::warning::", stdout)

    def test_the_tip_warning_is_an_annotation_on_actions(self):
        self.commit("型注釈つきの __version__", {"chime/__init__.py": self.ANNOTATED})
        code, stdout, stderr = self.run_on_actions()
        self.assertEqual((code, stderr), (0, ""))
        warnings = self.warning_lines(stdout)
        self.assertEqual(len(warnings), 1, stdout)
        self.assertTrue(warnings[0].startswith("::warning::"), warnings[0])
        self.assertIn('__version__ = "X.Y.Z"', warnings[0])

    def test_the_tip_warning_is_printed_in_dry_run_too(self):
        self.commit("型注釈つきの __version__", {"chime/__init__.py": self.ANNOTATED})
        code, _stdout, stderr = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.warning_lines(stderr)), 1, stderr)
        self.assertEqual(self.tags(), [])

    def test_the_tip_warning_ignores_since(self):
        self.commit("型注釈つきの __version__", {"chime/__init__.py": self.ANNOTATED})
        _code, _stdout, stderr = self.run_main("--since", "9.0.0")
        self.assertEqual(len(self.warning_lines(stderr)), 1, stderr)

    def test_a_tip_without_a_version_line_warns(self):
        self.commit("版の行を消す", {"chime/__init__.py": init_py(None)})
        code, _stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.warning_lines(stderr)), 1, stderr)

    def test_a_tip_without_the_version_file_does_not_warn(self):
        self.git("rm", "-q", "-r", "chime")
        self.git("commit", "-q", "-m", "chime を消す")
        code, _stdout, stderr = self.run_main()
        self.assertEqual((code, stderr), (0, ""))

    def test_an_unreadable_version_before_the_tip_does_not_warn(self):
        self.commit("型注釈つきの __version__", {"chime/__init__.py": self.ANNOTATED})
        self.commit("元に戻す", {"chime/__init__.py": init_py("5.3.0")})
        code, _stdout, stderr = self.run_main()
        self.assertEqual((code, stderr), (0, ""))

    def test_it_is_the_tip_of_the_given_ref_that_counts(self):
        self.commit("型注釈つきの __version__", {"chime/__init__.py": self.ANNOTATED})
        code, _stdout, stderr = self.run_main("--ref", self.merge["5.3.0"])
        self.assertEqual((code, stderr), (0, ""))
        _code, _stdout, stderr = self.run_main("--ref", "main")
        self.assertIn("main", self.warning_lines(stderr)[0])

    # -- (b) 見出しが無くて付けなかった版 --

    def skip_a_version(self) -> str:
        """5.4.0 を見出しなしで上げ、次の PR（5.5.0）で 5.4.0 と 5.5.0 の見出しを足す。"""
        self.merge_pr(4, "5.4.0", ["3.0.0", "5.2.0", "5.3.0"])
        _bump, merge = self.merge_pr(5, "5.5.0", ["3.0.0", "5.2.0", "5.3.0", "5.4.0", "5.5.0"])
        return merge

    def test_a_skipped_version_is_named_in_a_warning(self):
        merge = self.skip_a_version()
        code, stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        warnings = self.warning_lines(stderr)
        self.assertEqual(len(warnings), 1, stderr)
        self.assertIn("v5.4.0", warnings[0])
        self.assertIn("CHANGELOG.md", warnings[0])
        self.assertIn("## [5.4.0]", warnings[0])
        self.assertIn("付けませんでした", warnings[0])
        # タグは、そろっていた版にだけ付く
        self.assertEqual(self.tags(), ["v3.0.0", "v5.2.0", "v5.3.0", "v5.5.0"])
        self.assertEqual(self.tag_target("v5.5.0"), merge)
        self.assertNotIn("5.4.0", stdout)

    def test_a_skipped_version_is_an_annotation_on_actions(self):
        self.skip_a_version()
        code, stdout, stderr = self.run_on_actions()
        self.assertEqual((code, stderr), (0, ""))
        warnings = self.warning_lines(stdout)
        self.assertEqual(len(warnings), 1, stdout)
        self.assertTrue(warnings[0].startswith("::warning::"), warnings[0])
        self.assertIn("v5.4.0", warnings[0])
        self.assertIn("## [5.4.0]", warnings[0])

    def test_a_skipped_version_is_reported_in_dry_run_too(self):
        self.skip_a_version()
        code, _stdout, stderr = self.run_main("--dry-run", "--since", "5.2.0")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.warning_lines(stderr)), 1, stderr)
        self.assertIn("v5.4.0", stderr)
        self.assertEqual(self.tags(), [])

    def test_the_warning_is_repeated_on_every_run_until_it_is_dealt_with(self):
        self.skip_a_version()
        for _ in range(2):
            _code, _stdout, stderr = self.run_main()
            self.assertEqual(len(self.warning_lines(stderr)), 1, stderr)

    def test_skipped_versions_are_listed_in_version_order(self):
        """見出しなしで過ぎた版が複数あるときは、版の大小の順に並べること（5.9.0 が 5.10.0 より前）。

        文字として並べると、5.10.0 が先に来てしまう。
        """
        self.merge_pr(4, "5.9.0", ["3.0.0", "5.2.0", "5.3.0"])
        self.merge_pr(5, "5.10.0", ["3.0.0", "5.2.0", "5.3.0"])
        code, _stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        warnings = self.warning_lines(stderr)
        self.assertEqual(len(warnings), 2, stderr)
        self.assertIn("v5.9.0", warnings[0])
        self.assertIn("v5.10.0", warnings[1])

    def test_skipped_versions_are_in_version_order_even_if_history_is_not(self):
        """履歴で版が戻っていても（5.10.0 のあとに 5.9.0）、警告は版の大小の順に並べること。"""
        self.merge_pr(4, "5.10.0", ["3.0.0", "5.2.0", "5.3.0"])
        self.merge_pr(5, "5.9.0", ["3.0.0", "5.2.0", "5.3.0"])
        code, _stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        warnings = self.warning_lines(stderr)
        self.assertEqual(len(warnings), 2, stderr)
        self.assertIn("v5.9.0", warnings[0])
        self.assertIn("v5.10.0", warnings[1])

    def test_since_decides_which_skipped_versions_are_reported(self):
        self.skip_a_version()
        _code, _stdout, stderr = self.run_main("--since", "5.4.0")
        self.assertIn("v5.4.0", stderr)
        _code, _stdout, stderr = self.run_main("--since", "5.5.0")
        self.assertEqual(stderr, "")

    def test_a_hand_made_tag_silences_the_warning(self):
        self.skip_a_version()
        self.git("tag", "v5.4.0", self.merge["5.3.0"])
        code, _stdout, stderr = self.run_main()
        self.assertEqual((code, stderr), (0, ""))

    def test_a_late_heading_for_the_current_version_clears_the_warning(self):
        self.merge_pr(4, "5.4.0", ["3.0.0", "5.2.0", "5.3.0"])   # 見出しを足し忘れた
        _code, _stdout, stderr = self.run_main()
        self.assertEqual(len(self.warning_lines(stderr)), 1, stderr)
        self.assertIn("v5.4.0", stderr)
        late = self.commit("CHANGELOG に 5.4.0 の見出しを足す",
                           {"CHANGELOG.md": changelog("3.0.0", "5.2.0", "5.3.0", "5.4.0")})
        code, _stdout, stderr = self.run_main()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.tag_target("v5.4.0"), late)

    def test_a_missing_changelog_file_is_a_skipped_version_not_an_error(self):
        fresh = os.path.join(self.tmp, "no-changelog")
        self.init_repo(fresh)
        os.makedirs(os.path.join(fresh, "chime"))
        with open(os.path.join(fresh, "chime", "__init__.py"), "w", encoding="utf-8") as handle:
            handle.write(init_py("1.0.0"))
        self.git("add", "-A", cwd=fresh)
        self.git("commit", "-q", "-m", "CHANGELOG.md の無い履歴", cwd=fresh)
        code, stdout, stderr = self.run_main(repo=fresh)
        self.assertEqual(code, 0)
        self.assertIn("対象になる版が見つかりませんでした", stdout)
        self.assertEqual(len(self.warning_lines(stderr)), 1, stderr)
        self.assertIn("v1.0.0", stderr)
        self.assertEqual(self.tags(fresh), [])

    def test_both_kinds_of_warning_can_appear_together(self):
        self.skip_a_version()
        self.commit("型注釈つきの __version__", {"chime/__init__.py": self.ANNOTATED})
        code, _stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.warning_lines(stderr)), 2, stderr)


class SkippedBelowTheFloorTest(GitRepoTestCase):
    """--since より古い版が見出しなしで過ぎていても、それは知らせない（対象外）。"""

    def setUp(self):
        super().setUp()
        self.commit("初期コミット", {"README.md": "# chime\n"})
        self.merge_pr(1, "2.0.0", [])                # 見出しを足し忘れた古い版
        self.merge_pr(2, "5.2.0", ["5.2.0"])

    def test_without_since_the_old_version_is_reported(self):
        code, _stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("v2.0.0", stderr)
        self.assertEqual(self.tags(), ["v5.2.0"])

    def test_with_since_above_it_nothing_is_reported(self):
        for since in ("2.0.1", "5.2.0"):
            with self.subTest(since=since):
                code, _stdout, stderr = self.run_main("--since", since)
                self.assertEqual((code, stderr), (0, ""))

    def test_with_since_at_it_the_version_is_reported(self):
        _code, _stdout, stderr = self.run_main("--since", "2.0.0")
        self.assertIn("v2.0.0", stderr)


class ExitCodeContractTest(unittest.TestCase):
    """ドキュメントの文言（終了コードとマージ方法）。"""

    def test_the_docstring_explains_the_exit_codes(self):
        doc = tag_releases.__doc__
        self.assertIn("1 = git の実行（履歴の読み取り・タグの作成・push）に失敗", doc)
        self.assertIn("2 = 使い方や環境の問題", doc)
        self.assertNotIn("1 = タグの作成または push に失敗", doc)

    def test_the_docstring_explains_which_merge_methods_are_fine(self):
        doc = tag_releases.__doc__
        self.assertIn("Create a merge commit", doc)
        self.assertIn("Squash and merge", doc)
        self.assertIn("Rebase and merge", doc)


class EnvironmentProblemTest(GitRepoTestCase):
    """タグ付けを始められない環境では、何も作らずに終了コード 2 を返す。"""

    def shallow_clone(self) -> str:
        """``self.repo`` の履歴を作ってから、深さ 1 の浅いクローンを作り、そのパスを返す。"""
        self.build_history()
        shallow = os.path.join(self.tmp, "shallow")
        self.git("clone", "-q", "--depth", "1", "file://" + self.repo, shallow, cwd=self.tmp)
        self.assertEqual(self.git("rev-parse", "--is-shallow-repository", cwd=shallow), "true")
        return shallow

    def test_a_shallow_clone_is_refused(self):
        shallow = self.shallow_clone()
        code, stdout, stderr = self.run_main(repo=shallow)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("fetch-depth: 0", stderr)
        self.assertEqual(self.tags(shallow), [])

    def test_the_shallow_message_also_gives_the_local_remedy(self):
        """浅いクローンを断る文は、Actions での直し方（fetch-depth: 0）に加えて、手元での直し方も示すこと。"""
        _code, _stdout, stderr = self.run_main(repo=self.shallow_clone())
        self.assertIn("git fetch --unshallow", stderr)

    def test_a_full_clone_is_accepted(self):
        self.build_history()
        full = os.path.join(self.tmp, "full")
        self.git("clone", "-q", "file://" + self.repo, full, cwd=self.tmp)
        code, _stdout, stderr = self.run_main("--ref", "origin/main", repo=full)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(self.tags(full), ["v3.0.0", "v5.2.0", "v5.3.0"])

    def test_a_directory_that_is_not_a_repository_is_refused(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        code, stdout, stderr = self.run_main(repo=plain)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("git リポジトリではありません", stderr)

    def test_the_message_for_a_non_repository_includes_gits_own_reason(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        gits_reason = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=plain,
                                     capture_output=True, text=True, encoding="utf-8").stderr.strip()
        self.assertNotEqual(gits_reason, "")
        code, _stdout, stderr = self.run_main(repo=plain)
        self.assertEqual(code, 2)
        self.assertIn("git リポジトリではありません: " + plain, stderr)
        self.assertIn(gits_reason, stderr)

    def test_a_repository_git_refuses_to_open_shows_gits_reason(self):
        # 所有者が違うリポジトリ（dubious ownership）を git が開かない状況を、git のテスト用の
        # 環境変数で真似る。この変数を知らない git では、そのまま開けるので確かめない。
        with mock.patch.dict(os.environ, {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}):
            probe = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=self.repo,
                                   capture_output=True, text=True, encoding="utf-8")
            if probe.returncode == 0:
                self.skipTest("この git では所有者の違いを真似られません")
            code, _stdout, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn(probe.stderr.strip(), stderr)

    def test_a_missing_directory_is_refused(self):
        code, _stdout, stderr = self.run_main(repo=os.path.join(self.tmp, "missing"))
        self.assertEqual(code, 2)
        self.assertIn("git リポジトリではありません", stderr)

    def test_an_unknown_ref_is_refused(self):
        self.build_history()
        code, stdout, stderr = self.run_main("--ref", "no-such-branch")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("ref が見つかりません: no-such-branch", stderr)
        self.assertEqual(self.tags(), [])

    def test_a_ref_that_is_not_a_commit_is_refused(self):
        """コミットを指さない ref（ツリー）は、履歴をたどる前に使い方の問題として断ること。

        履歴を読む段階まで進むと、終了コードが 1（git の失敗）になってしまう。
        """
        self.build_history()
        code, stdout, stderr = self.run_main("--ref", "HEAD^{tree}")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("ref が見つかりません: HEAD^{tree}", stderr)
        self.assertEqual(self.tags(), [])

    def test_an_empty_repository_is_refused_because_head_does_not_exist(self):
        code, _stdout, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("ref が見つかりません", stderr)

    def test_git_missing_from_the_path_is_reported(self):
        with mock.patch.object(tag_releases.subprocess, "run", side_effect=FileNotFoundError):
            code, _stdout, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("git コマンドが見つかりません", stderr)


class ArgumentTest(GitRepoTestCase):
    """引数の誤りは argparse の使い方エラー（終了コード 2、何も作らない）。"""

    def parse_failure(self, *argv: str) -> Tuple[int, str]:
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                tag_releases.main(list(argv))
        return caught.exception.code, stderr.getvalue()

    def test_a_malformed_since_is_a_usage_error(self):
        self.commit("1", {"README.md": "x\n"})
        for value in ("5.3", "v5.3.0", "abc", "", "5.3.0.1", "5.x.0"):
            with self.subTest(since=value):
                code, message = self.parse_failure("--repo", self.repo, "--since", value)
                self.assertEqual(code, 2)
                self.assertIn("--since", message)
                self.assertIn("X.Y.Z", message)

    def test_a_ref_or_remote_that_looks_like_an_option_is_refused(self):
        for option in ("--ref", "--remote"):
            with self.subTest(option=option):
                code, message = self.parse_failure("--repo", self.repo, option + "=--force")
                self.assertEqual(code, 2)
                self.assertIn(option, message)

    def test_an_unknown_option_is_a_usage_error(self):
        code, _message = self.parse_failure("--repo", self.repo, "--force")
        self.assertEqual(code, 2)

    def test_help_is_in_japanese_and_lists_every_option(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                tag_releases.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        text = stdout.getvalue()
        for option in ("--since", "--ref", "--repo", "--dry-run", "--push", "--remote"):
            self.assertIn(option, text)
        self.assertIn("first-parent", text)
        self.assertRegex(text, r"[ぁ-んァ-ヶ一-龠]")


# -- ワークフロー（.github/workflows/tag.yml）の静的な確認 --------------------------------

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _without_comments(text: str) -> str:
    """YAML の行頭コメントを除いた本文（コメントに書いた語句で通ってしまわないように）。"""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


class WorkflowTest(unittest.TestCase):
    """タグを付けるワークフローが、意図した条件で・意図した形で動くこと。"""

    JOB_IF = "    if: github.ref == 'refs/heads/main'"
    GIT_CONFIG_NAME = 'git config user.name "github-actions[bot]"'
    GIT_CONFIG_EMAIL = 'git config user.email "41898282+github-actions[bot]@users.noreply.github.com"'
    TAG_COMMAND = "python scripts/tag_releases.py --since 5.2.0 --push"

    @classmethod
    def setUpClass(cls):
        cls.raw = _read(WORKFLOW_PATH)
        cls.text = _without_comments(cls.raw)
        cls.lines = cls.text.splitlines()

    def run_block(self) -> List[str]:
        """``run: |`` ブロックの中身（前後の空白を除いた行。空行は除く）。"""
        runs = [index for index, line in enumerate(self.lines) if re.match(r"\s*(-\s+)?run:", line)]
        self.assertEqual(len(runs), 1, "run: は 1 か所だけのはず")
        start = runs[0]
        self.assertEqual(self.lines[start].strip(), "run: |")   # 文字どおりのブロック
        indent = len(self.lines[start]) - len(self.lines[start].lstrip())
        block = []
        for line in self.lines[start + 1:]:
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) <= indent:
                break
            block.append(line.strip())
        return block

    def top_level_block(self, header: str) -> List[str]:
        """行頭の ``header``（``on:`` や ``jobs:`` など）の下に続く行（字下げのまま。空行は除く）。

        次に行頭から始まる行（次のトップレベルのキー）の手前までを返す。
        """
        block = []
        for line in self.lines[self.lines.index(header) + 1:]:
            if line.strip() and not line.startswith(" "):
                break
            if line.strip():
                block.append(line)
        return block

    def test_the_workflow_file_exists_next_to_ci(self):
        self.assertTrue(os.path.isfile(WORKFLOW_PATH))
        self.assertTrue(os.path.isfile(CI_WORKFLOW_PATH))

    def test_it_is_named_in_japanese(self):
        self.assertRegex(self.text, r"(?m)^name: タグ付け$")
        self.assertRegex(self.text, r"(?m)^    name: 版のタグを付ける$")

    def test_it_runs_on_pushes_to_main_and_by_hand(self):
        self.assertRegex(self.text, r"(?m)^on:\n  push:\n    branches: \[\s*[\"']?main[\"']?\s*\]\n")
        self.assertRegex(self.text, r"(?m)^  workflow_dispatch:")

    def test_the_trigger_is_exactly_push_to_main_and_manual_run(self):
        """起動の条件は main への push と手動実行の 2 つだけで、絞り込み（paths・tags・schedule など）が無いこと。

        絞り込みを足すと、版を入れたマージのあとにタグ付けが走らない、思わぬ時に走る、といった
        ことが起きる。
        """
        trigger = self.top_level_block("on:")
        self.assertEqual(len(trigger), 3, trigger)
        self.assertEqual(trigger[0], "  push:")
        self.assertRegex(trigger[1], r"^    branches: \[\s*[\"']?main[\"']?\s*\]$")
        self.assertEqual(trigger[2], "  workflow_dispatch:")

    def test_it_does_not_run_on_pull_requests_or_other_branches(self):
        self.assertNotIn("pull_request", self.text)
        self.assertNotIn('"**"', self.text)
        self.assertEqual(len(re.findall(r"branches:", self.text)), 1)

    def test_the_job_only_runs_on_the_main_branch(self):
        # 条件は 1 つだけ。ジョブの行そのもので、「|| …」などで広げていない
        ifs = [line for line in self.lines if re.match(r"\s*if:", line)]
        self.assertEqual(ifs, [self.JOB_IF])
        index = self.lines.index(self.JOB_IF)
        self.assertLess(self.lines.index("  tag:"), index)    # ジョブ自体の条件
        self.assertLess(index, self.lines.index("    steps:"))

    def test_there_is_exactly_one_job_named_tag(self):
        """ジョブは ``tag`` の 1 つだけ（別のジョブが増えると、条件（main だけ）の外で動いてしまう）。"""
        jobs = [line for line in self.top_level_block("jobs:") if re.match(r"  \S", line)]
        self.assertEqual(jobs, ["  tag:"])

    def test_the_workflow_may_write_contents_to_push_tags(self):
        # permissions は 1 か所・先頭の列で、contents: write だけ
        found = [index for index, line in enumerate(self.lines)
                 if re.match(r"\s*permissions:", line)]
        self.assertEqual(len(found), 1)
        index = found[0]
        self.assertEqual(self.lines[index], "permissions:")
        self.assertEqual(self.lines[index + 1], "  contents: write")
        after = self.lines[index + 2] if index + 2 < len(self.lines) else ""
        self.assertFalse(after.startswith((" ", "\t")), "contents: write のほかに権限がある")
        self.assertEqual(len(re.findall(r"contents:", self.text)), 1)

    def test_runs_are_serialized_and_never_cancelled(self):
        self.assertRegex(
            self.text,
            r"(?m)^concurrency:\n  group: tag-\$\{\{ github\.ref \}\}\n  cancel-in-progress: false\n")
        self.assertEqual(len(re.findall(r"(?m)^\s*group:", self.text)), 1)
        self.assertEqual(len(re.findall(r"cancel-in-progress:", self.text)), 1)

    def test_the_default_token_is_used_as_it_is(self):
        # 資格情報の扱いを変える設定・失敗を見逃す設定・別の ref や環境変数の差し込みは無い
        for pattern in (r"persist-credentials", r"continue-on-error", r"token:",
                        r"(?m)^\s+ref:", r"(?m)^\s*env:"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, self.text))

    def test_it_runs_on_ubuntu_latest(self):
        self.assertEqual([line for line in self.lines if "runs-on" in line],
                         ["    runs-on: ubuntu-latest"])

    def test_the_run_block_sets_the_identity_then_runs_the_script(self):
        self.assertEqual(self.run_block(),
                         [self.GIT_CONFIG_NAME, self.GIT_CONFIG_EMAIL, self.TAG_COMMAND])

    def test_the_steps_are_laid_out_as_a_valid_yaml_list(self):
        """steps の字下げが、YAML として読める形であること（ずれると、ワークフローが動かない）。

        各ステップの「- 」は同じ列に並び、その中のキー（``with:`` や ``run:``）は 2 つ深い列、
        さらにその中身（``with`` の値や ``run`` の本文）はもう 2 つ深い列にある。
        """
        job = self.top_level_block("jobs:")
        self.assertIn("    steps:", job)
        steps = job[job.index("    steps:") + 1:]
        self.assertTrue(steps)
        step_key = re.compile(r"(uses|with|name|run|if|env|shell|working-directory):")
        for line in steps:
            indent = len(line) - len(line.lstrip())
            if line.lstrip().startswith("- "):
                self.assertEqual(indent, 6, line)       # ステップの頭
            elif step_key.match(line.lstrip()):
                self.assertEqual(indent, 8, line)       # ステップのキー
            else:
                self.assertEqual(indent, 10, line)      # キーの中身

    def test_the_run_step_changes_neither_shell_nor_directory(self):
        """シェルも作業ディレクトリも既定のまま（``shell:`` ``working-directory:`` ``defaults:`` が無い）。

        スクリプトは、リポジトリ直下からの相対（``scripts/tag_releases.py``）で呼んでいる。
        """
        self.assertIsNone(re.search(r"(?m)^\s*(-\s+)?(shell|working-directory|defaults):",
                                    self.text))

    def test_there_are_no_tab_characters(self):
        self.assertNotIn("\t", self.raw)

    def test_the_whole_history_and_tags_are_fetched(self):
        self.assertRegex(self.text, r"actions/checkout@v\d+\n\s+with:\n\s+fetch-depth: 0\n")

    def test_the_action_versions_match_ci(self):
        ci = _read(CI_WORKFLOW_PATH)
        for action in ("actions/checkout", "actions/setup-python"):
            with self.subTest(action=action):
                used = set(re.findall(re.escape(action) + r"@(v\d+)", self.text))
                self.assertEqual(len(used), 1)
                self.assertTrue(used <= set(re.findall(re.escape(action) + r"@(v\d+)", ci)))

    def test_python_311_is_set_up(self):
        self.assertRegex(self.text, r"python-version: \"3\.11\"")

    def test_it_sets_the_bot_identity_before_tagging(self):
        name = self.text.index(self.GIT_CONFIG_NAME)
        email = self.text.index(self.GIT_CONFIG_EMAIL)
        run = self.text.index("python scripts/tag_releases.py")
        self.assertLess(name, run)
        self.assertLess(email, run)

    def test_it_runs_the_script_with_since_and_push(self):
        lines = [line.strip() for line in self.text.splitlines()
                 if "scripts/tag_releases.py" in line]
        self.assertEqual(lines, [self.TAG_COMMAND])

    def test_the_floor_is_not_hard_coded_in_the_script(self):
        script = _read(os.path.join(REPO_ROOT, "scripts", "tag_releases.py"))
        self.assertNotIn('"5.2.0"', script)
        self.assertNotIn("'5.2.0'", script)

    def test_the_why_is_explained_at_the_top_in_japanese(self):
        head = self.raw.split("name:", 1)[0]
        self.assertTrue(head.startswith("#"))
        self.assertIn("first-parent", head)
        self.assertIn("動かさない", head)
        self.assertRegex(head, r"[ぁ-んァ-ヶ一-龠]")

    def test_the_header_says_how_prs_must_be_merged(self):
        head = self.raw.split("name:", 1)[0]
        self.assertIn("Create a merge commit", head)
        self.assertIn("Squash and merge", head)
        self.assertIn("Rebase and merge", head)

    def test_the_script_it_runs_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, "scripts", "tag_releases.py")))


if __name__ == "__main__":
    unittest.main()
