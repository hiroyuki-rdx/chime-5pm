"""手元のコードがどの版・どのコミットかを調べる。

Raspberry Pi の現地で「今動いているのは、どのコミットか」を確かめるために、
``--version`` や ``--status`` が使う。``git`` コマンドは呼ばず（Pi に入っていない
こともあり、遅くもなるので）、``.git`` の中のファイルを直接読む。

調べられない場合（``.git`` が無い、読めない、形式が違う）は、例外ではなく
``"unknown"`` を返す。調べものが原因で起動や表示を止めないため。
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

from . import __version__
from .config import BASE_DIR

#: コミットを調べられなかったときの表記。
UNKNOWN = "unknown"

#: 短く表すときの桁数（``git rev-parse --short`` の既定と同じ）。
SHORT_LENGTH = 7

#: コミット ID（SHA-1 は 40 桁、SHA-256 は 64 桁の 16 進数）。
_COMMIT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

_SYMBOLIC_PREFIX = "ref:"
_GITDIR_PREFIX = "gitdir:"


def commit_id(base_dir: str = BASE_DIR) -> str:
    """``base_dir`` のチェックアウトの現在のコミットを、短い形（7 桁）で返す。

    ``.git`` はディレクトリでも、``gitdir: <場所>`` と書いたファイル（git worktree や
    サブモジュール）でもよい。``HEAD`` はブランチ名（``ref: refs/heads/main``）でも
    コミット ID そのもの（detached HEAD）でもよい。ブランチ名は、個別のファイル
    （``refs/heads/main``）、無ければ ``packed-refs`` から探す。
    分からなければ ``"unknown"``。例外は出さない。
    """
    try:
        full = _head_commit(base_dir)
    except (OSError, ValueError):
        # 読めない（権限・ディレクトリだった など）、UTF-8 でない（UnicodeDecodeError）。
        return UNKNOWN
    return full[:SHORT_LENGTH] if full else UNKNOWN


def version_string(base_dir: str = BASE_DIR) -> str:
    """``campus-chime 6.1.0 (a1b2c3d)`` の形の版の表記を返す（コミット不明なら ``(unknown)``）。"""
    return "campus-chime {0} ({1})".format(__version__, commit_id(base_dir))


def _head_commit(base_dir: str) -> str:
    """``HEAD`` の指すコミット ID（完全な形）を返す。分からなければ空文字。"""
    git_dir = _find_git_dir(base_dir)
    if git_dir is None:
        return ""
    head = _read_line(os.path.join(git_dir, "HEAD")) or ""
    if head.startswith(_SYMBOLIC_PREFIX):
        ref = head[len(_SYMBOLIC_PREFIX):].strip()
        head = _resolve_ref(_ref_dirs(git_dir), ref)
    return head if _COMMIT_ID.match(head) else ""


def _find_git_dir(base_dir: str) -> Optional[str]:
    """``base_dir/.git`` の実体（ディレクトリ）を返す。無ければ ``None``。"""
    dot_git = os.path.join(base_dir, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    line = _read_line(dot_git)
    if line is None or not line.startswith(_GITDIR_PREFIX):
        return None
    # 相対パスは ``.git`` ファイルのあるディレクトリから見た場所。絶対パスなら join はそのまま返す。
    git_dir = os.path.join(base_dir, line[len(_GITDIR_PREFIX):].strip())
    return git_dir if os.path.isdir(git_dir) else None


def _ref_dirs(git_dir: str) -> List[str]:
    """ブランチ名の実体を探すディレクトリ（worktree なら共通側の ``.git`` も）を返す。"""
    common = _read_line(os.path.join(git_dir, "commondir"))
    if not common:
        return [git_dir]
    return [git_dir, os.path.normpath(os.path.join(git_dir, common))]


def _resolve_ref(directories: List[str], ref: str) -> str:
    """``refs/heads/main`` のような名前のコミット ID を返す。見つからなければ空文字。"""
    parts = ref.split("/")
    # ``HEAD`` が ``.git`` の外を指して、無関係なファイルを読まないようにする。
    if len(parts) < 2 or parts[0] != "refs" or any(part in ("", ".", "..") for part in parts):
        return ""
    for directory in directories:
        value = _read_line(os.path.join(directory, *parts))
        if value is not None:
            return value
    for directory in directories:
        value = _packed_ref(os.path.join(directory, "packed-refs"), ref)
        if value:
            return value
    return ""


def _packed_ref(path: str, ref: str) -> str:
    """``packed-refs``（``<コミット ID> <名前>`` の行の並び）から ``ref`` を探す。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                # ``#`` は見出し、``^`` は直前のタグの指す先（ここでは使わない）。
                if line.startswith(("#", "^")):
                    continue
                value, _, name = line.strip().partition(" ")
                if name == ref:
                    return value
    except FileNotFoundError:
        pass
    return ""


def _read_line(path: str) -> Optional[str]:
    """ファイルの先頭の 1 行（前後の空白なし）を返す。ファイルが無ければ ``None``。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.readline().strip()
    except FileNotFoundError:
        return None
