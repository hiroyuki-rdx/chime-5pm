"""版とコミットの表示（``.git`` を直接読む）のテスト。"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from typing import Dict, Optional
from unittest import mock

import chime
from chime import buildinfo
from chime.config import BASE_DIR

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "f" * 8 + "0123456789abcdef" * 3 + "01234567"


def write(path: str, text: str) -> None:
    """``path`` に UTF-8 でファイルを書く（親ディレクトリも作る）。"""
    write_bytes(path, text.encode("utf-8"))


def write_bytes(path: str, data: bytes) -> None:
    """``path`` にバイト列のままファイルを書く（親ディレクトリも作る）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def make_git_dir(git_dir: str, head: str, refs: Optional[Dict[str, str]] = None,
                 packed: Optional[str] = None) -> str:
    """``git_dir`` に偽の ``.git`` の中身を作る（``refs`` は ``{名前: コミット ID}``）。"""
    write(os.path.join(git_dir, "HEAD"), head)
    for name, value in (refs or {}).items():
        write(os.path.join(git_dir, *name.split("/")), value + "\n")
    if packed is not None:
        write(os.path.join(git_dir, "packed-refs"), packed)
    return git_dir


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        # 偽のチェックアウト（``<root>/work``）。``.git`` は各テストで作る。
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        self.dot_git = os.path.join(self.work, ".git")


class CommitIdFromFilesTest(TempDirTestCase):
    def test_branch_with_a_loose_ref(self):
        make_git_dir(self.dot_git, "ref: refs/heads/main\n",
                     refs={"refs/heads/main": SHA})
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_branch_name_with_a_slash(self):
        make_git_dir(self.dot_git, "ref: refs/heads/feature/chime\n",
                     refs={"refs/heads/feature/chime": SHA})
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_only_the_head_branch_is_used(self):
        make_git_dir(self.dot_git, "ref: refs/heads/main\n",
                     refs={"refs/heads/main": SHA, "refs/heads/other": OTHER_SHA})
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_branch_in_packed_refs_only(self):
        # git gc のあとは、ブランチのファイルが無く packed-refs にだけ残る。
        packed = ("# pack-refs with: peeled fully-peeled sorted \n"
                  "{other} refs/heads/other\n"
                  "{sha} refs/heads/main\n"
                  "{other} refs/tags/v1.0.0\n"
                  "^{sha}\n").format(sha=SHA, other=OTHER_SHA)
        make_git_dir(self.dot_git, "ref: refs/heads/main\n", packed=packed)
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_packed_refs_does_not_match_a_longer_name(self):
        packed = "{0} refs/heads/main-old\n".format(OTHER_SHA)
        make_git_dir(self.dot_git, "ref: refs/heads/main\n", packed=packed)
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_a_loose_ref_wins_over_packed_refs(self):
        make_git_dir(self.dot_git, "ref: refs/heads/main\n",
                     refs={"refs/heads/main": SHA},
                     packed="{0} refs/heads/main\n".format(OTHER_SHA))
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_detached_head(self):
        make_git_dir(self.dot_git, SHA + "\n")
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_detached_head_without_a_trailing_newline(self):
        make_git_dir(self.dot_git, SHA)
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_sha256_repository(self):
        make_git_dir(self.dot_git, SHA256 + "\n")
        self.assertEqual(buildinfo.commit_id(self.work), SHA256[:7])

    def test_result_is_seven_characters(self):
        make_git_dir(self.dot_git, SHA + "\n")
        self.assertEqual(len(buildinfo.commit_id(self.work)), buildinfo.SHORT_LENGTH)
        self.assertEqual(buildinfo.SHORT_LENGTH, 7)


class GitdirFileTest(TempDirTestCase):
    """``.git`` が ``gitdir: <場所>`` と書いたファイルのとき（worktree・サブモジュール）。"""

    def test_absolute_gitdir(self):
        real = make_git_dir(os.path.join(self.root, "real.git"), SHA + "\n")
        write(self.dot_git, "gitdir: {0}\n".format(real))
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_relative_gitdir_is_relative_to_the_checkout(self):
        make_git_dir(os.path.join(self.root, "modules", "chime"), SHA + "\n")
        write(self.dot_git, "gitdir: ../modules/chime\n")
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_worktree_resolves_the_branch_through_commondir(self):
        main = os.path.join(self.root, "main", ".git")
        make_git_dir(main, "ref: refs/heads/main\n", refs={"refs/heads/main": OTHER_SHA})
        write(os.path.join(main, "refs", "heads", "wt"), SHA + "\n")
        worktree = make_git_dir(os.path.join(main, "worktrees", "wt"), "ref: refs/heads/wt\n")
        write(os.path.join(worktree, "commondir"), "../..\n")
        write(self.dot_git, "gitdir: {0}\n".format(worktree))
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_worktree_with_packed_refs_in_the_common_directory(self):
        main = os.path.join(self.root, "main", ".git")
        make_git_dir(main, "ref: refs/heads/main\n",
                     packed="{0} refs/heads/wt\n".format(SHA))
        worktree = make_git_dir(os.path.join(main, "worktrees", "wt"), "ref: refs/heads/wt\n")
        write(os.path.join(worktree, "commondir"), "../..\n")
        write(self.dot_git, "gitdir: {0}\n".format(worktree))
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_worktree_detached_head(self):
        main = os.path.join(self.root, "main", ".git")
        make_git_dir(main, "ref: refs/heads/main\n")
        worktree = make_git_dir(os.path.join(main, "worktrees", "wt"), SHA + "\n")
        write(os.path.join(worktree, "commondir"), "../..\n")
        write(self.dot_git, "gitdir: {0}\n".format(worktree))
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")

    def test_gitdir_pointing_nowhere(self):
        write(self.dot_git, "gitdir: {0}\n".format(os.path.join(self.root, "gone")))
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_dot_git_file_without_gitdir(self):
        write(self.dot_git, "これは gitdir の書き方ではありません\n")
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_a_dot_git_file_without_the_gitdir_prefix_is_not_followed(self):
        # 先頭の 7 文字を機械的に落とすと、"1234567real" が "real" になって
        # 本物の .git を指してしまう。"gitdir:" で始まる行だけを辿る。
        make_git_dir(os.path.join(self.work, "real"), SHA + "\n")
        write(self.dot_git, "1234567real\n")
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")


class UnknownCommitTest(TempDirTestCase):
    """調べられないときは、例外ではなく ``unknown``。"""

    def test_no_git_at_all(self):
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_base_dir_does_not_exist(self):
        self.assertEqual(buildinfo.commit_id(os.path.join(self.root, "nothing")), "unknown")

    def test_git_dir_without_head(self):
        os.makedirs(self.dot_git)
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_unborn_branch(self):
        # git init した直後。HEAD はブランチを指すが、ブランチのファイルはまだ無い。
        make_git_dir(self.dot_git, "ref: refs/heads/main\n")
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_empty_head(self):
        make_git_dir(self.dot_git, "")
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_garbage_head(self):
        make_git_dir(self.dot_git, "壊れた HEAD\n")
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_head_with_a_short_or_non_hex_id(self):
        for text in ("a1b2c3d\n", "g" * 40 + "\n", SHA + "00\n", SHA.upper() + "\n"):
            with self.subTest(text=text):
                make_git_dir(self.dot_git, text)
                self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_head_that_is_not_utf8(self):
        write_bytes(os.path.join(self.dot_git, "HEAD"), "ref: 閉館\n".encode("shift_jis"))
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_dot_git_that_is_binary_garbage(self):
        write_bytes(self.dot_git, b"\x00\xff\xfe\x80\x81")
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_head_that_is_a_directory(self):
        os.makedirs(os.path.join(self.dot_git, "HEAD"))
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_ref_file_with_garbage(self):
        make_git_dir(self.dot_git, "ref: refs/heads/main\n",
                     refs={"refs/heads/main": "中身が壊れています"})
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_empty_ref_file(self):
        make_git_dir(self.dot_git, "ref: refs/heads/main\n", refs={"refs/heads/main": ""})
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_ref_that_is_a_directory(self):
        make_git_dir(self.dot_git, "ref: refs/heads/feature\n",
                     refs={"refs/heads/feature/x": SHA})
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_packed_refs_with_a_bad_id(self):
        make_git_dir(self.dot_git, "ref: refs/heads/main\n",
                     packed="nothex refs/heads/main\n")
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_packed_refs_that_is_not_utf8(self):
        make_git_dir(self.dot_git, "ref: refs/heads/main\n")
        write_bytes(os.path.join(self.dot_git, "packed-refs"), b"\xff\xfe" + b"\x80" * 8)
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_head_cannot_point_outside_the_git_directory(self):
        # HEAD が ../ で外のファイルを指しても、そのファイルは読まない。
        write(os.path.join(self.work, "outside"), SHA + "\n")
        for ref in ("../outside", "refs/../../outside", "/etc/hostname", "refs", "refs//x",
                    "refs/./x", ""):
            with self.subTest(ref=ref):
                make_git_dir(self.dot_git, "ref: {0}\n".format(ref))
                self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_dotdot_is_refused_even_when_the_directories_exist(self):
        # refs/heads/../../../outside は、途中のフォルダが実在すると、文字どおりに
        # 辿れば .git の外のファイルになる。実在しても辿らない。
        write(os.path.join(self.work, "outside"), SHA + "\n")
        make_git_dir(self.dot_git, "ref: refs/heads/../../../outside\n")
        os.makedirs(os.path.join(self.dot_git, "refs", "heads"), exist_ok=True)
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_a_ref_outside_refs_is_refused(self):
        make_git_dir(self.dot_git, "ref: heads/main\n")
        write(os.path.join(self.dot_git, "heads", "main"), SHA + "\n")
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_an_empty_loose_ref_is_unknown_not_a_fallback_to_packed_refs(self):
        # 空のブランチのファイルは壊れた状態。packed-refs の古い値に切り替えない。
        make_git_dir(self.dot_git, "ref: refs/heads/main\n", refs={"refs/heads/main": ""},
                     packed="{0} refs/heads/main\n".format(SHA))
        self.assertEqual(buildinfo.commit_id(self.work), "unknown")

    def test_unreadable_does_not_raise(self):
        make_git_dir(self.dot_git, SHA + "\n")
        with mock.patch("builtins.open", side_effect=PermissionError("権限がありません")):
            self.assertEqual(buildinfo.commit_id(self.work), "unknown")


class NoSubprocessTest(TempDirTestCase):
    def test_git_command_is_never_run(self):
        make_git_dir(self.dot_git, "ref: refs/heads/main\n", refs={"refs/heads/main": SHA})
        names = ("run", "Popen", "call", "check_call", "check_output")
        patches = [mock.patch.object(subprocess, name, side_effect=AssertionError(name))
                   for name in names]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertEqual(buildinfo.commit_id(self.work), "a1b2c3d")
        self.assertEqual(buildinfo.commit_id(self.root), "unknown")
        self.assertTrue(buildinfo.version_string(self.work).endswith("(a1b2c3d)"))

    def test_module_does_not_import_subprocess(self):
        self.assertFalse(hasattr(buildinfo, "subprocess"))


class VersionStringTest(TempDirTestCase):
    def test_format(self):
        make_git_dir(self.dot_git, SHA + "\n")
        self.assertEqual(buildinfo.version_string(self.work),
                         "campus-chime {0} (a1b2c3d)".format(chime.__version__))

    def test_unknown_commit(self):
        self.assertEqual(buildinfo.version_string(self.work),
                         "campus-chime {0} (unknown)".format(chime.__version__))

    def test_version_is_not_hard_coded(self):
        make_git_dir(self.dot_git, SHA + "\n")
        with mock.patch.object(buildinfo, "__version__", "9.8.7"):
            self.assertEqual(buildinfo.version_string(self.work), "campus-chime 9.8.7 (a1b2c3d)")

    def test_looks_like_a_version_line(self):
        self.assertRegex(buildinfo.version_string(self.work),
                         r"^campus-chime \d+\.\d+\.\d+ \((?:[0-9a-f]{7}|unknown)\)$")

    def test_matches_the_version_option_of_the_cli(self):
        # --version の「campus-chime <版>」の部分と同じ書き方であること。
        self.assertTrue(buildinfo.version_string(self.work).startswith(
            "campus-chime {0} (".format(chime.__version__)))


def _git_head(path: str) -> Optional[str]:
    """読み取り専用の ``git rev-parse HEAD`` の結果（使えなければ ``None``）。"""
    if not os.path.exists(os.path.join(path, ".git")):
        return None
    try:
        result = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


class RealRepositoryTest(unittest.TestCase):
    """このリポジトリ自身のチェックアウトで、git の答えと一致すること。"""

    def setUp(self):
        self.expected = _git_head(BASE_DIR)
        if not self.expected or not re.match(r"^[0-9a-f]{40,64}$", self.expected):
            self.skipTest("git のチェックアウトではないため、実物との照合を省きます")

    def test_commit_id_matches_git(self):
        self.assertEqual(buildinfo.commit_id(BASE_DIR), self.expected[:7])

    def test_default_base_dir_is_the_repository(self):
        self.assertEqual(buildinfo.commit_id(), self.expected[:7])

    def test_version_string_shows_the_commit(self):
        self.assertEqual(buildinfo.version_string(),
                         "campus-chime {0} ({1})".format(chime.__version__, self.expected[:7]))


if __name__ == "__main__":
    unittest.main()
