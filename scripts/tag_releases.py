#!/usr/bin/env python3
"""main の版ごとのタグ（``vX.Y.Z``）を付ける。

main を first-parent でたどり、``chime/__init__.py`` の ``__version__`` が
最初にその版になったコミット（＝その版を入れた PR のマージコミット）に、
注釈付きタグ ``vX.Y.Z`` を付ける。PR のブランチ上の個々のコミット（版を
上げた最初のコミットなど）には付けない。

``CHANGELOG.md`` にその版の見出し（``## [X.Y.Z]``）が無いコミットには付けない。
見出しが後のコミットで足された場合は、版と見出しの両方がそろった最初の
コミットに付ける。

既にあるタグは決して動かさない・消さない。同じ名前のタグが別のコミットに
付いていたときは、警告を出すだけで何もしない（付け直すなら人が判断する）。
そのため何度実行しても結果は変わらない（まだ付いていない版にだけ付ける）。

PR は「Create a merge commit」（または「Squash and merge」）でマージする。
「Rebase and merge」や fast-forward でマージすると、PR の中のコミットがそのまま
main の first-parent に並ぶため、タグが PR の最終状態ではなく、PR の中で版を
上げたコミットに付いてしまう。

GitHub Actions（``.github/workflows/tag.yml``）から、main への push のたびに
実行する::

    python scripts/tag_releases.py --since 5.2.0 --push

手元で何が付くかだけ確かめるには ``--dry-run`` を付ける（タグは作らず、
push もしない）::

    python3 scripts/tag_releases.py --dry-run
    python3 scripts/tag_releases.py --dry-run --since 5.2.0 --ref origin/main

履歴をすべてたどるため、浅いクローン（``git clone --depth 1`` や、
``actions/checkout`` の既定の ``fetch-depth: 1``）では動かない。Actions では
``fetch-depth: 0`` を指定する。``git config user.name`` / ``user.email`` は
このスクリプトでは設定しない（注釈付きタグに必要なので、呼ぶ側で設定する）。

版を読めなかった・見出しが無くてタグを付けなかった、といった取りこぼしは
警告で知らせる（終了コードは変わらない）。

終了コード: 0 = 正常（付けるものが無かった場合や、警告だけの場合を含む）、
1 = git の実行（履歴の読み取り・タグの作成・push）に失敗、2 = 使い方や環境の問題
（git リポジトリでない・浅いクローン・ref が無い・引数が不正）。
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "GitError", "VERSION_RE", "first_appearances", "has_changelog_entry", "main",
    "parse_version", "read_file_at", "tag_commit", "version_key",
]

#: ``__version__ = "5.3.0"`` の行（引用符は ' でも " でもよい）。X.Y.Z の数字だけを取り出す。
#: 行頭から始まる行だけを見る（インデントした行や docstring 内の例は拾わない）。
VERSION_RE = re.compile(r"""^__version__[ \t]*=[ \t]*["']([0-9]+\.[0-9]+\.[0-9]+)["']""",
                        re.MULTILINE)

_VERSION_ONLY_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

#: 版を書いているファイルと、その版の見出しを書いているファイル（リポジトリ直下からの相対）。
VERSION_FILE = "chime/__init__.py"
CHANGELOG_FILE = "CHANGELOG.md"


class GitError(RuntimeError):
    """git の実行に失敗した。"""


def parse_version(text: str) -> Optional[str]:
    """``chime/__init__.py`` の中身から ``__version__`` の版（``X.Y.Z``）を取り出す。

    ``__version__`` の行が無い、または ``X.Y.Z``（数字 3 つ）の形でなければ ``None``。
    行が複数あるときは最初のもの。
    """
    match = VERSION_RE.search(text)
    return match.group(1) if match else None


def version_key(version: str) -> Tuple[int, int, int]:
    """``"5.10.0"`` を ``(5, 10, 0)`` にする（版の大小を数として比べるため）。

    ``X.Y.Z`` の形でなければ ``ValueError``。
    """
    if not _VERSION_ONLY_RE.fullmatch(version):
        raise ValueError("版は X.Y.Z の形で指定してください: {0!r}".format(version))
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def has_changelog_entry(text: str, version: str) -> bool:
    """``CHANGELOG.md`` の中身に、``version`` の見出し（``## [X.Y.Z]``）があるか。

    行頭の ``## [X.Y.Z]`` だけを見る。``version`` の ``.`` は文字どおりの点として
    扱い、``5.3`` が ``## [5.3.0]`` に当たることはない。ファイル末尾の
    リンク参照（``[5.3.0]: https://…``）は見出しではない。
    """
    pattern = r"^## \[{0}\]".format(re.escape(version))
    return re.search(pattern, text, re.MULTILINE) is not None


def _git(repo: str, *args: str, env: Optional[dict] = None) -> "subprocess.CompletedProcess[str]":
    """``repo`` で git を実行する（シェルは通さない）。終了コードは呼び出し側が見る。"""
    return subprocess.run(["git"] + list(args), cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env)


def _detail(result: "subprocess.CompletedProcess[str]") -> str:
    """失敗した git の説明（標準エラー。空なら終了コード）。"""
    return result.stderr.strip() or "git が終了コード {0} で失敗しました".format(result.returncode)


def read_file_at(repo: str, commit: str, path: str) -> Optional[str]:
    """``commit`` 時点の ``path`` の中身を返す。そのコミットに ``path`` が無ければ ``None``。

    ``git cat-file`` が失敗しても、それだけでは「ファイルが無い」とは言えない
    （オブジェクトが欠けている・壊れている、部分クローンで取れない、など）。
    ``None`` にすると、タグがもっと後のコミットに付いて、しかも後から直せない。
    そこで ``git ls-tree`` で確かめ、本当に ``path`` が無いときだけ ``None`` にする。
    それ以外（``path`` はあるのに読めない場合を含む）は :class:`GitError`。
    """
    cat = _git(repo, "cat-file", "blob", "{0}:{1}".format(commit, path))
    if cat.returncode == 0:
        return cat.stdout
    listing = _git(repo, "ls-tree", "--name-only", commit, "--", path)
    if listing.returncode == 0 and not listing.stdout.strip():
        return None
    raise GitError("{0} の {1} を読み取れませんでした（履歴にあるのにファイルとして読めません。"
                   "オブジェクトの欠け・破損や、ファイルでないことなどが考えられます）: {2}"
                   .format(commit[:7], path, _detail(listing if listing.returncode != 0 else cat)))


def _scan(repo: str, ref: str) -> Tuple[List[Tuple[str, str]], List[str], bool]:
    """``ref`` の first-parent の履歴を 1 回たどる。``(found, unrecorded, tip_unreadable)`` を返す。

    - ``found``: 版ごとに、タグを付けるべきコミット（古い順の ``(版, 完全な sha)``）。
    - ``unrecorded``: 履歴の途中で ``__version__`` がその版になったことはあるのに、
      その版と ``CHANGELOG.md`` の見出しが同時にそろったコミットが 1 つも無く、
      記録されなかった版（古い順）。見出しが後から足されても、その時にはもう
      ``__version__`` が先へ進んでいると、この版にタグが付かないまま残る。
    - ``tip_unreadable``: ``ref`` の先頭のコミットに ``chime/__init__.py`` はあるが、
      ``__version__`` を読み取れない。

    git の実行に失敗したら :class:`GitError`。
    """
    listing = _git(repo, "rev-list", "--first-parent", "--reverse", ref, "--")
    if listing.returncode != 0:
        raise GitError("コミットの一覧を取得できませんでした（{0}）: {1}"
                       .format(ref, listing.stderr.strip()))

    commits = listing.stdout.split()
    found: List[Tuple[str, str]] = []
    recorded = set()
    seen: List[str] = []       # 履歴の中で __version__ がなった版（初めて見た順）
    tip_unreadable = False
    for index, commit in enumerate(commits):
        init_text = read_file_at(repo, commit, VERSION_FILE)
        if init_text is None:
            continue
        version = parse_version(init_text)
        if version is None:
            if index == len(commits) - 1:
                tip_unreadable = True
            continue
        if version not in seen:
            seen.append(version)
        if version in recorded:
            continue
        changelog = read_file_at(repo, commit, CHANGELOG_FILE)
        if changelog is None or not has_changelog_entry(changelog, version):
            continue
        recorded.add(version)
        found.append((version, commit))
    unrecorded = sorted((version for version in seen if version not in recorded),
                        key=version_key)
    return found, unrecorded, tip_unreadable


def first_appearances(repo: str, ref: str = "HEAD") -> List[Tuple[str, str]]:
    """版ごとに、タグを付けるべきコミットを返す（古い順の ``(版, 完全な sha)``）。

    ``git rev-list --first-parent --reverse <ref>`` の順にたどり、
    ``__version__`` がその版で、かつその時点の ``CHANGELOG.md`` にその版の
    見出しがある最初のコミットを、版ごとに 1 つ記録する。``chime/__init__.py``
    が無い、または版を読み取れないコミットは飛ばす。

    first-parent なので、PR のブランチ上で版を上げたコミットではなく、
    それを main に取り込んだマージコミットが選ばれる。

    git の実行に失敗したら :class:`GitError`。
    """
    return _scan(repo, ref)[0]


def tag_commit(repo: str, tag: str) -> Optional[str]:
    """タグ ``tag`` が指すコミットの完全な sha を返す。注釈付きタグは中身まで辿る。

    そのタグが無ければ ``None``。
    """
    result = _git(repo, "rev-parse", "--verify", "--quiet",
                  "refs/tags/{0}^{{commit}}".format(tag))
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _since_type(value: str) -> str:
    """``--since`` の値の検査（``X.Y.Z`` の形でなければ argparse が使い方エラーにする）。"""
    try:
        version_key(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "X.Y.Z の形で指定してください（例: 5.2.0）: {0!r}".format(value))
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", metavar="X.Y.Z", type=_since_type, default=None,
                        help="この版以降だけを対象にする（その版を含む）。"
                             "それより古い版には付けない。既定: すべての版")
    parser.add_argument("--ref", default="HEAD",
                        help="この ref の first-parent の履歴をたどる（既定: HEAD）")
    parser.add_argument("--repo", metavar="PATH", default=".",
                        help="git リポジトリのパス（既定: カレントディレクトリ）")
    parser.add_argument("--dry-run", action="store_true",
                        help="付ける予定を表示するだけで、タグは作らない（push もしない）")
    parser.add_argument("--push", action="store_true",
                        help="この実行で作ったタグを --remote へ push する"
                             "（作ったタグが無ければ push しない。強制 push はしない）")
    parser.add_argument("--remote", default="origin", metavar="NAME",
                        help="--push の送り先（既定: origin）")
    return parser


def _check_repository(repo: str, ref: str) -> Optional[str]:
    """タグ付けを始められる状態か調べる。だめなら、その理由（利用者向けの文）を返す。"""
    if not os.path.isdir(repo):
        return "git リポジトリではありません（ディレクトリがありません）: {0}".format(repo)
    probe = _git(repo, "rev-parse", "--git-dir")
    if probe.returncode != 0:
        # 所有者が違う（dubious ownership）など、理由は git のメッセージに出る。
        return "git リポジトリではありません: {0}\ngit のメッセージ: {1}".format(
            repo, _detail(probe))
    shallow = _git(repo, "rev-parse", "--is-shallow-repository")
    if shallow.stdout.strip() == "true":
        return ("履歴が途中までしかない（shallow）リポジトリです。版が最初に現れた"
                "コミットを探すには全履歴が必要です。GitHub Actions では "
                "actions/checkout の fetch-depth: 0 を指定してください"
                "（手元なら git fetch --unshallow）。")
    if _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}").returncode != 0:
        return "ref が見つかりません: {0}".format(ref)
    return None


def _subject(repo: str, commit: str) -> str:
    """コミットの 1 行目のメッセージ。取れなければ空文字。"""
    result = _git(repo, "log", "-1", "--format=%s", commit, "--")
    return result.stdout.strip() if result.returncode == 0 else ""


def _warn(message: str) -> None:
    """警告を出す。GitHub Actions 上では、画面の注釈に出る ``::warning::`` の形にする。"""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::warning::" + message)
    else:
        print("警告: " + message, file=sys.stderr)


def _push_tags(repo: str, remote: str, tags: List[str]) -> Optional[str]:
    """``tags`` を 1 回の ``git push`` でまとめて送る。失敗したら git のメッセージを返す。"""
    refspecs = ["refs/tags/{0}".format(tag) for tag in tags]
    # 認証を求められても対話で待たずに失敗させる（Actions や cron で固まらないように）。
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    result = _git(repo, "push", remote, *refspecs, env=env)
    if result.returncode != 0:
        return _detail(result)
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # 先頭が - の値は git のオプションとして解釈されてしまうため、受け付けない。
    for option, value in (("--ref", args.ref), ("--remote", args.remote)):
        if value.startswith("-"):
            parser.error("{0} の値が - で始まっています: {1!r}".format(option, value))

    try:
        problem = _check_repository(args.repo, args.ref)
    except FileNotFoundError:
        print("git コマンドが見つかりません。git をインストールしてください。", file=sys.stderr)
        return 2
    if problem:
        print(problem, file=sys.stderr)
        return 2

    try:
        found, unrecorded, tip_unreadable = _scan(args.repo, args.ref)
    except GitError as exc:
        # 履歴を読み切れていないので、タグは 1 つも作らず、push もしない。
        print(str(exc), file=sys.stderr)
        return 1

    floor = version_key(args.since) if args.since is not None else None
    appearances = found
    if floor is not None:
        appearances = [(version, sha) for version, sha in found
                       if version_key(version) >= floor]

    # 取りこぼし（タグが付かないまま黙って過ぎてしまうもの）は警告で知らせる。
    if tip_unreadable:
        _warn('{0} の先頭のコミットに {1} はありますが、__version__ を読み取れませんでした。'
              '行頭に __version__ = "X.Y.Z"（数字 3 つ）の形で書かないと、この先の版に'
              'タグが付きません。'.format(args.ref, VERSION_FILE))
    for version in unrecorded:
        if floor is not None and version_key(version) < floor:
            continue
        if tag_commit(args.repo, "v" + version) is not None:
            continue
        _warn('v{0} は付けませんでした。__version__ がその版だったコミットはありましたが、'
              'その時点の {1} に "## [{0}]" の見出しがありませんでした。'
              '付けるなら、付けるコミットを決めて手で付けてください。'
              .format(version, CHANGELOG_FILE))

    if not appearances:
        if found:
            # 版は見つかっているので、CHANGELOG の見出しのせいではない。
            latest = max((version for version, _sha in found), key=version_key)
            print("--since {0} 以降の版はまだありません（見つかった最新の版は {1}）。"
                  .format(args.since, latest))
        else:
            print("対象になる版が見つかりませんでした"
                  "（版と CHANGELOG.md の見出しがそろったコミットがありません）。")
        return 0

    created: List[str] = []   # この実行で作ったタグ（--push で送るのはこれだけ）
    failed: List[str] = []    # 作れなかったタグ
    planned = 0               # --dry-run で「付ける予定」になった数
    for version, sha in appearances:
        tag = "v" + version
        short = sha[:7]
        existing = tag_commit(args.repo, tag)
        if existing == sha:
            print("{0} は付いています（{1}）".format(tag, short))
            continue
        if existing is not None:
            # 既にあるタグは動かさない。付け直すかどうかは人が決める。
            _warn("{0} は {1} に付いています。この版が最初に現れたのは {2} ですが、"
                  "既にあるタグは動かしません。".format(tag, existing[:7], short))
            continue

        subject = _subject(args.repo, sha)
        if args.dry_run:
            print("{0} を {1} に付ける予定です（{2}）".format(tag, short, subject))
            planned += 1
            continue

        result = _git(args.repo, "tag", "-a", tag, sha, "-m", tag)
        if result.returncode != 0:
            # 1 つ失敗しても、ほかのタグは続けて付ける（原因はタグごとに違うことがある。
            # 例えば同名の ref を別の処理が掴んでいるだけなら、ほかの版は付けられる）。
            # 失敗は集めて、最後に終了コード 1 にする。push するのは付けられたタグだけ。
            # 付けられなかった版は、原因を直してから再実行すれば付く。
            print("{0} を付けられませんでした: {1}".format(tag, _detail(result)),
                  file=sys.stderr)
            failed.append(tag)
            continue
        created.append(tag)
        print("{0} を {1} に付けました（{2}）".format(tag, short, subject))

    if args.dry_run:
        if planned == 0:
            print("新しく付けるタグはありません。")
            return 0
        print("--dry-run のため、タグは何も作成していません（付ける予定: {0} 件）。"
              .format(planned))
        if args.push:
            print("--dry-run のため、push もしません。")
        return 0

    exit_code = 0
    if failed:
        print("{0} 件のタグを付けられませんでした（{1}）。原因を直してから再実行してください。"
              .format(len(failed), ", ".join(failed)), file=sys.stderr)
        exit_code = 1
    elif not created:
        print("新しく付けるタグはありません。")
        return 0

    if args.push and created:
        failure = _push_tags(args.repo, args.remote, created)
        if failure is not None:
            print("タグを {0} に push できませんでした: {1}".format(args.remote, failure),
                  file=sys.stderr)
            print("タグはこのリポジトリには作ってあります（{0}）。"
                  .format(", ".join(created)), file=sys.stderr)
            print("手で送るには: git push {0} {1}".format(
                shlex.quote(args.remote),
                " ".join("refs/tags/{0}".format(tag) for tag in created)), file=sys.stderr)
            exit_code = 1
        else:
            print("{0} 件のタグを {1} に push しました（{2}）。"
                  .format(len(created), args.remote, ", ".join(created)))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
