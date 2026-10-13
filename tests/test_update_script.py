"""更新スクリプト（``scripts/update.sh``）のテスト。

update.sh は Pi の上で人が手で実行するもので、CI でも他のテストでも走らない。
更新の途中で止まると運用中の放送に響くので、次を固定する。

* 止まる条件（root・手元の変更・detached HEAD・取り込みの失敗）では、**何も変えずに**
  理由を表示して終了する。``config.json``（Git 管理外）は止める理由にならない。
* ``git pull`` の失敗は、git の表示から理由を見分けて案内する（権限・手元の Git 管理外のファイル・
  ディスク［空き容量の不足・読み取り専用・入出力エラー・割り当て超過］、それ以外はネットワークか
  履歴の食い違い）。ディスクの語句は、権限の語句と一緒に出ても、ディスクとして案内する。
* 呼ぶ順番（``git pull`` →``--wait-idle`` → ``setup.sh --no-apt`` → ``--status``）。
  すでに最新で、サービスにも反映済みなら、``--status`` だけ見せて何もしない。
* **反映済みの記録**（``cache/deployed_commit``。``setup.sh`` がサービスの再起動に成功した
  ときに書く）。取り込みのあとで ``--wait-idle`` や ``setup.sh`` が失敗すると、コードは
  最新なのにサービスは古い版のままになる。もう一度実行したとき「すでに最新版」で終わらず、
  記録が無い・食い違うなら続きを行う。
* ``--wait-idle`` の終了コード 2（``config.json`` を読めない）は、「放送が終わらない」と
  言わずに、設定を直すよう案内する。
* 手元で書き換えたファイルの戻し方は、**表示されたコマンドを実際に動かして**、状態が
  きれいになることまで確かめる（``git add`` 済みの変更や、空白・日本語・引用符を含む名前も）。
* ``git status`` 自体が失敗したとき（持ち主が違う・リポジトリでない）は、「中止:」と理由を出す。
* 破壊的なことをしない。元の版への戻し方は表示するだけで、実行しない
  （``git reset`` などが呼ばれた記録が残らない）。

スクリプトは本物を ``bash`` で動かし、``git`` も本物を使う。ただし、リポジトリは
一時フォルダに作った使い捨て（手元の ``origin.git`` から ``clone`` した ``pi``）で、
通信はしない。``python3``・``id``・``sudo``・``systemctl`` と ``setup.sh`` は、呼ばれた
記録だけ残す偽物に差し替える（``git`` も、記録してから本物に渡す入れ物を挟む）。
``setup.sh`` の偽物は、成功したときに本物と同じ約束（``cache/deployed_commit`` に
コミットを書く）を守る。本物の ``setup.sh`` と組み合わせた通しの確認は
``RealSetupIntegrationTest``。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest

from tests.support import REPO_ROOT

UPDATE_SH = os.path.join(REPO_ROOT, "scripts", "update.sh")
SETUP_SH = os.path.join(REPO_ROOT, "scripts", "setup.sh")
REAL_GIT = shutil.which("git")

#: 追跡しておく、名前に癖のあるファイル（空白・日本語・引用符・``$``・先頭のハイフン・改行）。
AWKWARD_NAMES = ("docs/a b.txt", "docs/日本語 のメモ.txt", "it's \"q\" $HOME.txt", "-dash.txt",
                 "line\nbreak.txt")

#: update.sh が呼んでよい git のサブコマンド（読むものと ``pull`` だけ）。
ALLOWED_GIT = ("status", "symbolic-ref", "rev-parse", "pull")

#: 偽物の ``setup.sh``。呼ばれた記録を残し、成功したときは本物と同じく、反映した版を
#: ``cache/deployed_commit`` に書く（update.sh が見る約束）。
SETUP_STUB = """#!/usr/bin/env bash
echo "setup.sh $*" >> "$CALLS_LOG"
if [ "${SETUP_EXIT:-0}" -eq 0 ]; then
  cd "$(dirname "$0")/.."
  mkdir -p cache
  "$REAL_GIT" rev-parse HEAD > cache/deployed_commit
fi
exit "${SETUP_EXIT:-0}"
"""

#: 偽物の ``python3``。版は ``VERSION`` の中身とコミットの短縮形で返す。
PYTHON_STUB = """#!/bin/sh
echo "python3 $*" >> "$CALLS_LOG"
case "$*" in
  *--version*) echo "campus-chime $(cat VERSION) ($("$REAL_GIT" rev-parse --short HEAD))" ;;
  *--wait-idle*) exit "${WAIT_IDLE_EXIT:-0}" ;;
  *--status*) echo "STATUS-OUTPUT"; exit "${STATUS_EXIT:-0}" ;;
  *--generate-assets*) exit "${GENERATE_EXIT:-0}" ;;
  *--check*) echo "CHECK-OUTPUT"; exit "${CHECK_EXIT:-0}" ;;
esac
"""

#: ``git`` は記録してから本物に渡す。``GIT_STATUS_ERROR`` があれば、``git status`` だけ
#: その文面で失敗させる（持ち主が違うリポジトリを git が開かない状況）。``GIT_PULL_ERROR`` が
#: あれば、``git pull`` だけその文面（標準エラー出力）で失敗させる（権限の問題など、root で動く
#: テストでは本物の git に起こせない失敗）。
GIT_STUB = """#!/bin/sh
echo "git $*" >> "$CALLS_LOG"
if [ "$1" = "status" ] && [ -n "$GIT_STATUS_ERROR" ]; then
  echo "$GIT_STATUS_ERROR" >&2
  exit 128
fi
if [ "$1" = "pull" ]; then
  # pull がどの言語設定で呼ばれたか（git の表示を英語に固定して読むため）。別のファイルに残す。
  echo "${LC_ALL-unset}" > "$CALLS_LOG.pull-locale"
  if [ -n "$GIT_PULL_ERROR" ]; then
    printf '%s\\n' "$GIT_PULL_ERROR" >&2
    exit 1
  fi
fi
exec "$REAL_GIT" "$@"
"""

#: ``id -un`` と ``id -gn`` は、実行している利用者とグループの名前（``FAKE_USER`` / ``FAKE_GROUP``）。
ID_STUB = """#!/bin/sh
echo "id $*" >> "$CALLS_LOG"
case "$1" in
  -un) echo "${FAKE_USER:-pi}" ;;
  -gn) echo "${FAKE_GROUP:-pi}" ;;
  *) echo "${FAKE_UID:-1000}" ;;
esac
"""

LOG_ONLY_STUB = """#!/bin/sh
echo "{name} $*" >> "$CALLS_LOG"
"""


def header_block() -> str:
    """update.sh 先頭のコメントブロック（``--help`` が表示するはずの範囲）。"""
    block = []
    with open(UPDATE_SH, "r", encoding="utf-8") as handle:
        for line in handle.read().splitlines()[1:]:
            if not line.startswith("#"):
                break
            block.append(line)
    return "".join(line + "\n" for line in block)


@unittest.skipUnless(REAL_GIT, "git が無い環境では、更新スクリプトを試せない")
class UpdateScriptCase(unittest.TestCase):
    """使い捨ての ``origin.git`` と、そこから ``clone`` した ``pi`` を作る。"""

    #: 真なら、origin の ``setup.sh`` を偽物でなく本物にする（通しの確認用）。
    REAL_SETUP = False

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.calls_log = os.path.join(self.tmp, "calls.log")
        self.work = os.path.join(self.tmp, "origin-work")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.pi = os.path.join(self.tmp, "pi")
        self.elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(self.elsewhere)

        self.git_env = {key: value for key, value in os.environ.items()
                        if not key.startswith("GIT_")}
        self.git_env.update(
            HOME=self.tmp, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="tester", GIT_AUTHOR_EMAIL="tester@example.invalid",
            GIT_COMMITTER_NAME="tester", GIT_COMMITTER_EMAIL="tester@example.invalid",
            GIT_TERMINAL_PROMPT="0")

        self.make_origin()
        self.git("clone", "-q", self.origin, self.pi, cwd=self.tmp)
        self.make_bin()

    # -- 使い捨てのリポジトリ -------------------------------------------
    def git(self, *args, cwd):
        return subprocess.run([REAL_GIT] + list(args), cwd=cwd, env=self.git_env, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()

    def write(self, directory, name, text, mode=None):
        path = os.path.join(directory, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mode:
            os.chmod(path, mode)
        return path

    def make_origin(self):
        os.mkdir(self.work)
        self.git("init", "-q", cwd=self.work)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.work)
        shutil.copy(UPDATE_SH, self.write(self.work, "scripts/update.sh", ""))
        if self.REAL_SETUP:
            shutil.copy(SETUP_SH, self.write(self.work, "scripts/setup.sh", "", 0o755))
        else:
            self.write(self.work, "scripts/setup.sh", SETUP_STUB)
        self.write(self.work, "campus_chime.py", "# 偽物（python3 も偽物なので読まれない）\n")
        self.write(self.work, "VERSION", "6.0.0\n")
        self.write(self.work, ".gitignore", "config.json\ncache/\n")
        for name in AWKWARD_NAMES:
            self.write(self.work, name, "元の内容\n")
        self.git("add", "-A", cwd=self.work)
        self.git("commit", "-q", "-m", "初期", cwd=self.work)
        self.git("clone", "-q", "--bare", self.work, self.origin, cwd=self.tmp)

    def advance_origin(self, version="6.1.0"):
        """origin に新しいコミット（版の更新）を足す。"""
        self.write(self.work, "VERSION", version + "\n")
        self.git("commit", "-q", "-am", "更新 " + version, cwd=self.work)
        self.git("push", "-q", self.origin, "main", cwd=self.work)

    def add_to_origin(self, name, text="新しい内容\n"):
        """origin に、新しいファイルを足したコミットを足す。"""
        self.write(self.work, name, text)
        self.git("add", name, cwd=self.work)
        self.git("commit", "-q", "-m", "追加 " + name, cwd=self.work)
        self.git("push", "-q", self.origin, "main", cwd=self.work)

    def pull_locale(self):
        """``git pull`` が呼ばれたときの ``LC_ALL``（呼ばれていなければ ``None``）。"""
        path = self.calls_log + ".pull-locale"
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()

    def head(self):
        return self.git("rev-parse", "HEAD", cwd=self.pi)

    # -- 偽物のコマンド -------------------------------------------------
    def make_bin(self):
        bin_dir = os.path.join(self.tmp, "bin")
        stubs = {"git": GIT_STUB, "python3": PYTHON_STUB, "id": ID_STUB}
        for name in ("sudo", "systemctl", "apt-get", "timedatectl", "sleep"):
            stubs[name] = LOG_ONLY_STUB.format(name=name)
        for name, text in stubs.items():
            self.write(bin_dir, name, text, 0o755)
        self.env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""),
                        CALLS_LOG=self.calls_log, REAL_GIT=REAL_GIT)
        for key in [key for key in self.env if key.startswith("GIT_")]:
            del self.env[key]
        self.env.update(HOME=self.tmp, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_TERMINAL_PROMPT="0")

    def run_update(self, *args, **env):
        """``pi`` の update.sh を、リポジトリの外から実行する。"""
        return subprocess.run(
            ["bash", os.path.join(self.pi, "scripts", "update.sh")] + list(args),
            capture_output=True, text=True, encoding="utf-8", cwd=self.elsewhere,
            env=dict(self.env, **env), timeout=60)

    # -- 記録 -----------------------------------------------------------
    def calls(self):
        """呼ばれたコマンドを、呼ばれた順に返す（``id`` 以降）。"""
        if not os.path.exists(self.calls_log):
            return []
        with open(self.calls_log, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()

    def forget_calls(self):
        """記録を空にする（1 つのテストで update.sh を続けて動かすとき、回ごとに見るため）。"""
        if os.path.exists(self.calls_log):
            os.remove(self.calls_log)

    def setup_calls(self):
        return [call for call in self.calls() if call.startswith("setup.sh")]

    def restarts(self):
        """サービスの再起動の記録（本物の setup.sh を使う通しの確認で見る）。"""
        return [call for call in self.calls() if call == "sudo systemctl restart campus_chime.service"]

    def deployed(self):
        """``cache/deployed_commit`` の中身（無ければ ``None``）。"""
        path = os.path.join(self.pi, "cache", "deployed_commit")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()

    def assert_nothing_destructive(self):
        calls = self.calls()
        git_commands = [call.split()[1] for call in calls if call.startswith("git ")]
        self.assertTrue(set(git_commands) <= set(ALLOWED_GIT), git_commands)
        for call in calls:
            self.assertFalse(call.startswith(("sudo", "systemctl", "apt-get")), call)
        self.assertNotIn("--hard", " ".join(calls))


class HelpTest(UpdateScriptCase):
    def test_help_prints_exactly_the_header_block(self):
        expected = header_block()
        self.assertIn("bash scripts/update.sh", expected)
        self.assertNotIn("set -euo pipefail", expected)
        result = self.run_update("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.calls(), [])

    def test_short_help_is_the_same(self):
        self.assertEqual(self.run_update("-h").stdout, header_block())

    def test_an_unknown_option_stops_before_anything_runs(self):
        result = self.run_update("--bogus")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("不明なオプション: --bogus", result.stderr)
        self.assertEqual(self.calls(), [])


class RefusalTest(UpdateScriptCase):
    """止まる条件では、何も変えずに理由を表示して終了する。"""

    def test_it_refuses_to_run_as_root_before_touching_anything(self):
        result = self.run_update(FAKE_UID="0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("root では実行できません", result.stderr)
        self.assertIn("pi ユーザー", result.stderr)
        self.assertEqual(self.calls(), ["id -u"])

    def test_it_prints_the_current_version_first(self):
        result = self.run_update()
        self.assertIn("== いまの版", result.stdout)
        self.assertLess(result.stdout.index("== いまの版"), result.stdout.index("campus-chime 6.0.0 ("))

    def test_a_modified_tracked_file_stops_the_update_and_is_listed_with_its_fix(self):
        self.write(self.pi, "VERSION", "hacked\n")
        self.write(self.pi, "campus_chime.py", "# edited\n")
        self.advance_origin()
        before = self.head()
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn(" M VERSION", result.stderr)
        self.assertIn("  git restore --source=HEAD --staged --worktree -- VERSION\n", result.stderr)
        self.assertIn("  git restore --source=HEAD --staged --worktree -- campus_chime.py\n", result.stderr)
        self.assertIn("何も変更していません", result.stderr)
        self.assertEqual(self.head(), before)
        self.assertNotIn("git pull --ff-only", self.calls())
        self.assertEqual(self.calls()[:3], ["id -u", "python3 campus_chime.py --version",
                                            "git status --porcelain -z --untracked-files=no"])
        self.assertEqual(len(self.calls()), 3)
        self.assert_nothing_destructive()
        # 書き換えたファイルはそのまま残る（このスクリプトは戻さない）。
        with open(os.path.join(self.pi, "VERSION"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hacked\n")

    def test_the_older_form_of_the_fix_is_mentioned_for_a_git_without_restore(self):
        self.write(self.pi, "VERSION", "hacked\n")
        result = self.run_update()
        self.assertIn("2.23", result.stderr)
        self.assertIn("git checkout HEAD -- <ファイル>", result.stderr)

    def test_a_staged_change_also_stops_the_update(self):
        self.write(self.pi, "VERSION", "staged\n")
        self.git("add", "VERSION", cwd=self.pi)
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("M  VERSION", result.stderr)
        self.assertIn("git restore --source=HEAD --staged --worktree -- VERSION", result.stderr)

    def test_untracked_files_including_config_json_do_not_block(self):
        self.write(self.pi, "config.json", '{"_comment": "現地"}\n')
        self.write(self.pi, "notes.txt", "memo\n")
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("手元の変更はありません。", result.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.pi, "config.json")))
        self.assertTrue(os.path.exists(os.path.join(self.pi, "notes.txt")))

    def test_a_detached_head_stops_the_update(self):
        self.git("checkout", "-q", "--detach", cwd=self.pi)
        self.advance_origin()
        before = self.head()
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("detached HEAD", result.stderr)
        self.assertIn("何も変更していません", result.stderr)
        self.assertEqual(self.head(), before)
        self.assertNotIn("git pull --ff-only", self.calls())
        self.assert_nothing_destructive()

    def test_a_failed_pull_is_explained_in_plain_language_and_changes_nothing(self):
        # この機械にだけあるコミットがあり、origin にも別のコミットがある（履歴が食い違う）。
        self.write(self.pi, "LOCAL", "この機械だけの変更\n")
        self.git("add", "LOCAL", cwd=self.pi)
        self.git("commit", "-q", "-m", "手元", cwd=self.pi)
        self.advance_origin()
        before = self.head()
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("最新版を取り込めませんでした", result.stderr)
        self.assertIn("食い違っています", result.stderr)
        self.assertIn("何も変更していません", result.stderr)
        self.assertEqual(self.head(), before)
        calls = self.calls()
        self.assertIn("git pull --ff-only", calls)
        self.assertFalse([call for call in calls if call.startswith("setup.sh")])
        self.assertFalse([call for call in calls if "--wait-idle" in call])
        self.assert_nothing_destructive()

    def test_an_unreachable_origin_is_explained_the_same_way(self):
        self.git("remote", "set-url", "origin", os.path.join(self.tmp, "no-such-origin.git"), cwd=self.pi)
        before = self.head()
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("最新版を取り込めませんでした", result.stderr)
        self.assertEqual(self.head(), before)


class AlreadyCurrentTest(UpdateScriptCase):
    """コードが最新で、サービスにも反映済み（記録が HEAD と一致）なら、何もしない。"""

    def mark_deployed(self, text=None):
        """反映済みの記録を書く（省略時は、いまの HEAD）。"""
        self.write(self.pi, "cache/deployed_commit", self.head() + "\n" if text is None else text)

    def test_it_only_shows_the_status_and_changes_nothing(self):
        self.mark_deployed()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("すでに最新版です。", result.stdout)
        self.assertIn("STATUS-OUTPUT", result.stdout)
        calls = self.calls()
        self.assertEqual(calls[-1], "python3 campus_chime.py --status")
        self.assertFalse([call for call in calls if "--wait-idle" in call])
        self.assertFalse([call for call in calls if call.startswith("setup.sh")])
        self.assertEqual(result.stderr, "")
        self.assert_nothing_destructive()

    def test_a_status_that_needs_attention_does_not_make_the_update_fail(self):
        self.mark_deployed()
        result = self.run_update(STATUS_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_calls_of_a_clean_up_to_date_run(self):
        self.mark_deployed()
        self.run_update()
        self.assertEqual(self.calls(), [
            "id -u",
            "python3 campus_chime.py --version",
            "git status --porcelain -z --untracked-files=no",
            "git symbolic-ref -q HEAD",
            "git rev-parse HEAD",
            "git pull --ff-only",
            "git rev-parse HEAD",
            "python3 campus_chime.py --status",
        ])

    def test_a_record_without_a_newline_or_with_spaces_still_counts(self):
        for text in (self.head(), "  " + self.head() + "  \r\n"):
            with self.subTest(text=repr(text)):
                self.mark_deployed(text)
                self.forget_calls()
                result = self.run_update()
                self.assertIn("すでに最新版です。", result.stdout)
                self.assertEqual(self.setup_calls(), [])


class StaleServiceTest(UpdateScriptCase):
    """コードは最新でも、サービスが古い版のままかもしれないとき（前回の更新が途中で止まった）。"""

    CONTINUING = "続けて反映します"

    def test_a_rerun_after_the_broadcast_wait_failed_restarts_the_service(self):
        self.advance_origin()
        first = self.run_update(WAIT_IDLE_EXIT="1")
        self.assertEqual(first.returncode, 1)
        self.assertEqual(self.setup_calls(), [])
        self.assertEqual(self.deployed(), None)

        self.forget_calls()
        second = self.run_update()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertNotIn("すでに最新版です", second.stdout)
        self.assertIn("古い版のままかもしれません", second.stderr)
        self.assertIn(self.CONTINUING, second.stderr)
        calls = self.calls()
        self.assertEqual(calls[-4:], ["python3 campus_chime.py --wait-idle", "setup.sh --no-apt",
                                      "python3 campus_chime.py --version", "python3 campus_chime.py --status"])
        self.assertEqual(self.deployed(), self.head())

    def test_a_rerun_after_setup_failed_restarts_the_service(self):
        self.advance_origin()
        first = self.run_update(SETUP_EXIT="1")
        self.assertEqual(first.returncode, 1)
        self.assertEqual(self.deployed(), None)

        self.forget_calls()
        second = self.run_update()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.setup_calls(), ["setup.sh --no-apt"])
        self.assertIn("古い版のままかもしれません", second.stderr)
        self.assertEqual(self.deployed(), self.head())

    def test_a_clean_run_after_a_successful_update_does_not_restart(self):
        self.advance_origin()
        first = self.run_update()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.deployed(), self.head())

        self.forget_calls()
        second = self.run_update()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("すでに最新版です。", second.stdout)
        self.assertEqual(self.setup_calls(), [])
        self.assertFalse([call for call in self.calls() if "--wait-idle" in call])
        self.assertEqual(second.stderr, "")

    def test_the_run_that_follows_the_recovery_is_clean_again(self):
        self.advance_origin()
        self.run_update(WAIT_IDLE_EXIT="1")
        self.run_update()
        self.forget_calls()
        third = self.run_update()
        self.assertIn("すでに最新版です。", third.stdout)
        self.assertEqual(self.setup_calls(), [])

    def test_a_missing_record_is_treated_as_not_deployed(self):
        """6.1.0 より前から動いている機械には、記録が無い。最新版でも、一度は反映し直す。"""
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("すでに最新版です", result.stdout)
        self.assertIn("記録（cache/deployed_commit）がありません", result.stderr)
        self.assertEqual(self.setup_calls(), ["setup.sh --no-apt"])
        self.assertEqual(self.deployed(), self.head())

    def test_a_record_of_another_version_is_named_in_the_message(self):
        old = self.head()
        self.advance_origin()
        self.write(self.pi, "cache/deployed_commit", old + "\n")
        self.git("pull", "-q", "--ff-only", cwd=self.pi)
        result = self.run_update()
        self.assertIn("サービスに反映した版（{0}）".format(old[:7]), result.stderr)
        self.assertIn("いまの版（{0}）".format(self.head()[:7]), result.stderr)
        self.assertEqual(self.setup_calls(), ["setup.sh --no-apt"])

    def test_a_record_that_cannot_be_read_is_treated_as_missing(self):
        os.makedirs(os.path.join(self.pi, "cache", "deployed_commit"))
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setup_calls(), ["setup.sh --no-apt"])

    def test_a_garbage_record_is_treated_as_not_deployed(self):
        self.write(self.pi, "cache/deployed_commit", "not a commit\n")
        self.run_update()
        self.assertEqual(self.setup_calls(), ["setup.sh --no-apt"])

    def test_the_continuation_still_waits_for_the_broadcast_to_end(self):
        """続きの反映も、放送の最中には再起動しない（--wait-idle が失敗すれば、setup.sh は呼ばれない）。"""
        result = self.run_update(WAIT_IDLE_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.setup_calls(), [])
        self.assertIn("サービスはまだ再起動していません", result.stderr)

    def test_the_rollback_is_not_offered_when_the_code_did_not_change(self):
        """コードは変わっていないので、「元の版へ戻す」手順は意味がない（成功しても、失敗しても）。"""
        failed = self.run_update(WAIT_IDLE_EXIT="1")
        self.assertEqual(failed.returncode, 1)
        self.assertNotIn("git reset --hard", failed.stdout + failed.stderr)
        setup_failed = self.run_update(SETUP_EXIT="1")
        self.assertEqual(setup_failed.returncode, 1)
        self.assertNotIn("git reset --hard", setup_failed.stdout + setup_failed.stderr)
        config_unreadable = self.run_update(WAIT_IDLE_EXIT="2")
        self.assertEqual(config_unreadable.returncode, 1)
        self.assertNotIn("git reset --hard", config_unreadable.stdout + config_unreadable.stderr)
        result = self.run_update()
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("git reset --hard", result.stdout + result.stderr)
        self.assertNotIn("元の版", result.stdout + result.stderr)

    def test_the_failure_messages_say_that_a_rerun_continues(self):
        self.advance_origin()
        result = self.run_update(SETUP_EXIT="1")
        self.assertIn("もう一度 bash scripts/update.sh", result.stderr)

    def test_nothing_destructive_happens_on_the_continuation(self):
        self.run_update()
        self.assert_nothing_destructive()


class WaitIdleExitCodeTest(UpdateScriptCase):
    """``--wait-idle`` の終了コードで、案内を変える（2 は ``config.json`` を読めない）。"""

    def test_exit_2_says_the_config_is_unreadable_not_that_the_broadcast_did_not_end(self):
        self.advance_origin()
        result = self.run_update(WAIT_IDLE_EXIT="2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("config.json", result.stderr)
        self.assertIn("読めない", result.stderr)
        self.assertIn("bash scripts/setup.sh --no-apt", result.stderr)
        self.assertIn("サービスはまだ再起動していません", result.stderr)
        self.assertNotIn("放送が終わったことを確認できませんでした", result.stderr)
        self.assertEqual(self.setup_calls(), [])
        self.assert_nothing_destructive()

    def test_exit_2_says_setup_applies_the_update_only_after_the_config_is_fixed(self):
        """設定を読めないままでは、setup.sh を手で実行しても反映されない（再起動を控えて、失敗で終わる）。"""
        self.advance_origin()
        result = self.run_update(WAIT_IDLE_EXIT="2")
        [hurry] = [line for line in result.stderr.splitlines() if "急ぐとき" in line]
        self.assertIn("config.json を直したうえで", hurry)
        self.assertIn("bash scripts/setup.sh --no-apt", hurry)
        self.assertLess(hurry.index("config.json を直したうえで"), hurry.index("bash scripts/setup.sh"))
        self.assertIn("読めないままでは、setup.sh もサービスを再起動しません", hurry)

    def test_exit_2_after_an_update_still_shows_the_rollback(self):
        self.advance_origin()
        old_head = self.head()
        result = self.run_update(WAIT_IDLE_EXIT="2")
        self.assertIn("git reset --hard {0}".format(old_head), result.stderr)

    def test_exit_2_is_continued_by_a_rerun_once_the_config_is_fixed(self):
        self.advance_origin()
        self.run_update(WAIT_IDLE_EXIT="2")
        self.forget_calls()
        again = self.run_update()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.setup_calls(), ["setup.sh --no-apt"])

    def test_exit_1_still_says_the_broadcast_did_not_end(self):
        self.advance_origin()
        result = self.run_update(WAIT_IDLE_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("放送が終わったことを確認できませんでした", result.stderr)
        self.assertNotIn("読めない", result.stderr)

    def test_any_other_failure_is_treated_as_the_broadcast_not_ending(self):
        self.advance_origin()
        result = self.run_update(WAIT_IDLE_EXIT="3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("放送が終わったことを確認できませんでした", result.stderr)
        self.assertEqual(self.setup_calls(), [])


class UpdateTest(UpdateScriptCase):
    EXPECTED_ORDER = [
        "id -u",
        "python3 campus_chime.py --version",
        "git status --porcelain -z --untracked-files=no",
        "git symbolic-ref -q HEAD",
        "git rev-parse HEAD",
        "git pull --ff-only",
        "git rev-parse HEAD",
        "python3 campus_chime.py --wait-idle",
        "setup.sh --no-apt",
        "python3 campus_chime.py --version",
        "python3 campus_chime.py --status",
    ]

    def test_it_pulls_waits_for_the_broadcast_runs_setup_and_shows_the_status_in_order(self):
        self.advance_origin()
        old_head = self.head()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), self.EXPECTED_ORDER)
        self.assertNotEqual(self.head(), old_head)
        self.assert_nothing_destructive()

    def test_it_shows_the_old_and_the_new_version(self):
        self.advance_origin()
        old_short = self.git("rev-parse", "--short", "HEAD", cwd=self.pi)
        result = self.run_update()
        new_short = self.git("rev-parse", "--short", "HEAD", cwd=self.pi)
        self.assertIn("campus-chime 6.0.0 ({0})  →  campus-chime 6.1.0 ({1})".format(old_short, new_short),
                      result.stdout)
        self.assertIn("STATUS-OUTPUT", result.stdout)

    def test_it_prints_how_to_roll_back_but_never_runs_it(self):
        self.advance_origin()
        old_head = self.head()
        result = self.run_update()
        self.assertIn("git reset --hard {0}".format(old_head), result.stdout)
        self.assertIn("bash scripts/setup.sh --no-apt", result.stdout)
        self.assertIn("このスクリプトは実行しません", result.stdout)
        self.assertFalse([call for call in self.calls() if "reset" in call])
        # 戻していない（取り込んだ版のまま）。
        self.assertNotEqual(self.head(), old_head)

    def test_it_runs_from_the_repository_even_when_started_elsewhere(self):
        """``python3`` は更新するリポジトリの中で呼ばれる（偽物の python3 が ``VERSION`` を読む）。"""
        self.advance_origin()
        result = self.run_update()
        self.assertIn("campus-chime 6.0.0 (", result.stdout)
        self.assertIn("campus-chime 6.1.0 (", result.stdout)

    def test_a_status_that_needs_attention_does_not_fail_the_update(self):
        self.advance_origin()
        result = self.run_update(STATUS_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_when_the_broadcast_does_not_end_the_service_is_not_restarted(self):
        self.advance_origin()
        result = self.run_update(WAIT_IDLE_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("サービスはまだ再起動していません", result.stderr)
        self.assertIn("bash scripts/setup.sh --no-apt", result.stderr)
        self.assertIn("git reset --hard", result.stderr)
        calls = self.calls()
        self.assertIn("python3 campus_chime.py --wait-idle", calls)
        self.assertFalse([call for call in calls if call.startswith("setup.sh")])
        self.assert_nothing_destructive()

    def test_when_setup_fails_the_rollback_is_shown_and_the_update_fails(self):
        self.advance_origin()
        result = self.run_update(SETUP_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("導入スクリプトが途中で失敗しました", result.stderr)
        self.assertIn("git reset --hard", result.stderr)
        self.assertNotIn("python3 campus_chime.py --status", self.calls())
        self.assert_nothing_destructive()


class VersionAndRollbackTest(UpdateScriptCase):
    """版の表示と、元の版へ戻す手順の細部。"""

    def stub(self, name, text):
        path = os.path.join(self.tmp, "bin", name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, 0o755)

    def test_a_failing_version_command_stops_with_a_message_before_anything_else(self):
        self.stub("python3", '''#!/bin/sh
echo "python3 $*" >> "$CALLS_LOG"
exit 1
''')
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("中止:", result.stderr)
        self.assertIn("実行できません", result.stderr)
        self.assertIn(self.pi, result.stderr)
        self.assertEqual(self.calls(), ["id -u", "python3 campus_chime.py --version"])

    def test_a_failing_version_after_the_update_is_tolerated(self):
        """更新が済んだあとで版を読めなくても、更新そのものは成功のまま（版の表示だけ代わりの文言）。"""
        self.advance_origin()
        counter = os.path.join(self.tmp, "counter")
        failing_second_time = PYTHON_STUB.replace(
            '*--version*) echo',
            '*--version*) n=$(cat "%s" 2>/dev/null || echo 0); echo $((n+1)) > "%s"; [ "$n" -ge 1 ] && exit 1; echo'
            % (counter, counter))
        self.assertNotEqual(failing_second_time, PYTHON_STUB)
        self.stub("python3", failing_second_time)
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("（版を取得できません）", result.stdout)
        self.assertIn("STATUS-OUTPUT", result.stdout)

    def test_the_rollback_block_is_exactly_the_two_commands_for_the_old_version(self):
        self.advance_origin()
        old = self.head()
        result = self.run_update()
        tail = result.stdout.split("このスクリプトは実行しません）。")[1]
        commands = [line for line in tail.splitlines() if line.strip()]
        self.assertEqual(commands, ["  git reset --hard " + old, "  bash scripts/setup.sh --no-apt"])
        self.assertIn("元の版（{0}）".format(old[:7]), result.stdout)

    def test_the_rollback_in_a_failure_names_the_same_old_version(self):
        self.advance_origin()
        old = self.head()
        result = self.run_update(SETUP_EXIT="1")
        self.assertIn("元の版（{0}）".format(old[:7]), result.stderr)
        self.assertIn("git reset --hard " + old, result.stderr)


class RestoreAdviceTest(UpdateScriptCase):
    """止まったときに表示される『元に戻すコマンド』が、本当に状態をきれいにすること。

    表示されたコマンドをそのまま ``bash`` で動かし、``git status`` が空になるまでを確かめる。
    ``git add`` 済みの変更は、以前の ``git checkout -- <ファイル>`` では戻らなかった。
    """

    RESTORE = "git restore --source=HEAD --staged --worktree -- "

    def printed_commands(self, stderr):
        return [line.strip() for line in stderr.splitlines() if line.startswith("  " + self.RESTORE)]

    def dirty(self):
        return self.git("status", "--porcelain", "--untracked-files=no", cwd=self.pi)

    def run_printed(self, command):
        subprocess.run(["bash", "-c", command], cwd=self.pi, env=self.git_env, check=True,
                       capture_output=True, text=True, encoding="utf-8")

    def reset(self):
        self.git("reset", "-q", "--hard", cwd=self.pi)
        self.git("clean", "-fdq", cwd=self.pi)

    def assert_advice_cleans(self, prepare, expect_commands=1):
        prepare()
        self.assertNotEqual(self.dirty(), "")
        refused = self.run_update()
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        commands = self.printed_commands(refused.stderr)
        self.assertEqual(len(commands), expect_commands, refused.stderr)
        for command in commands:
            self.run_printed(command)
        self.assertEqual(self.dirty(), "")
        # 片付いたので、もう止まらない。
        proceeded = self.run_update()
        self.assertEqual(proceeded.returncode, 0, proceeded.stdout + proceeded.stderr)
        self.assertIn("手元の変更はありません。", proceeded.stdout)

    def test_an_unstaged_edit_is_cleared(self):
        self.assert_advice_cleans(lambda: self.write(self.pi, "VERSION", "edited\n"))

    def test_a_staged_edit_is_cleared(self):
        def prepare():
            self.write(self.pi, "VERSION", "staged\n")
            self.git("add", "VERSION", cwd=self.pi)
        self.assert_advice_cleans(prepare)

    def test_a_staged_edit_with_a_further_unstaged_edit_is_cleared(self):
        def prepare():
            self.write(self.pi, "VERSION", "staged\n")
            self.git("add", "VERSION", cwd=self.pi)
            self.write(self.pi, "VERSION", "staged and edited again\n")
        self.assert_advice_cleans(prepare)

    def test_an_unstaged_deletion_is_cleared(self):
        self.assert_advice_cleans(lambda: os.remove(os.path.join(self.pi, "VERSION")))

    def test_a_staged_deletion_is_cleared(self):
        self.assert_advice_cleans(lambda: self.git("rm", "-q", "VERSION", cwd=self.pi))

    def test_a_newly_added_file_is_cleared(self):
        def prepare():
            self.write(self.pi, "added.txt", "new\n")
            self.git("add", "added.txt", cwd=self.pi)
        self.assert_advice_cleans(prepare)
        self.assertFalse(os.path.exists(os.path.join(self.pi, "added.txt")))

    def test_a_staged_rename_is_cleared_with_one_command_naming_both_paths(self):
        self.assert_advice_cleans(lambda: self.git("mv", "VERSION", "VERSION.renamed", cwd=self.pi))
        self.assertTrue(os.path.exists(os.path.join(self.pi, "VERSION")))
        self.assertFalse(os.path.exists(os.path.join(self.pi, "VERSION.renamed")))

    def test_every_kind_of_awkward_name_is_quoted_so_that_the_printed_command_works(self):
        for staged in (False, True):
            for name in AWKWARD_NAMES:
                with self.subTest(name=name, staged=staged):
                    self.reset()

                    def prepare(name=name, staged=staged):
                        self.write(self.pi, name, "書き換えた\n")
                        if staged:
                            self.git("add", "--", name, cwd=self.pi)
                    self.assert_advice_cleans(prepare)
                    with open(os.path.join(self.pi, name), encoding="utf-8") as handle:
                        self.assertEqual(handle.read(), "元の内容\n")

    def test_a_rename_of_an_awkward_name_is_cleared(self):
        self.assert_advice_cleans(
            lambda: self.git("mv", "--", "docs/a b.txt", "docs/日本語 'q'.txt", cwd=self.pi))

    def test_many_changes_at_once_each_get_their_own_command(self):
        def prepare():
            self.write(self.pi, "VERSION", "x\n")
            self.write(self.pi, "docs/a b.txt", "x\n")
            self.git("add", "VERSION", cwd=self.pi)
            self.git("rm", "-q", "--", "-dash.txt", cwd=self.pi)
            os.remove(os.path.join(self.pi, "campus_chime.py"))
        self.assert_advice_cleans(prepare, expect_commands=4)

    def test_the_listing_shows_each_status_code_and_the_quoted_name(self):
        self.write(self.pi, "VERSION", "x\n")
        self.git("add", "VERSION", cwd=self.pi)
        self.write(self.pi, "docs/a b.txt", "x\n")
        result = self.run_update()
        self.assertIn("  M  VERSION\n", result.stderr)
        self.assertIn("   M docs/a\\ b.txt\n", result.stderr)
        self.assertIn("{0}docs/a\\ b.txt".format(self.RESTORE), result.stderr)

    def test_the_old_checkout_form_is_gone_because_it_leaves_staged_changes(self):
        self.write(self.pi, "VERSION", "x\n")
        self.git("add", "VERSION", cwd=self.pi)
        result = self.run_update()
        self.assertNotIn("git checkout -- ", result.stderr)

    def test_nothing_is_touched_by_the_refusal_itself(self):
        self.write(self.pi, "VERSION", "x\n")
        self.git("add", "VERSION", cwd=self.pi)
        before = self.dirty()
        self.run_update()
        self.assertEqual(self.dirty(), before)
        self.assert_nothing_destructive()


class GitStatusFailureTest(UpdateScriptCase):
    """``git status`` 自体が失敗したとき（持ち主が違う・リポジトリでない）。"""

    def test_a_failing_git_status_stops_with_the_reason_and_exit_1(self):
        message = "fatal: detected dubious ownership in repository at '{0}'".format(self.pi)
        result = self.run_update(GIT_STATUS_ERROR=message)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("中止:", result.stderr)
        self.assertIn("dubious ownership", result.stderr)
        self.assertIn("何も変更していません", result.stderr)
        self.assertNotIn("手元の変更はありません", result.stdout)

    def test_nothing_is_pulled_or_restarted_after_it(self):
        self.advance_origin()
        before = self.head()
        self.run_update(GIT_STATUS_ERROR="fatal: boom")
        self.assertEqual(self.head(), before)
        calls = self.calls()
        self.assertNotIn("git pull --ff-only", calls)
        self.assertEqual(self.setup_calls(), [])
        self.assertFalse([call for call in calls if "--wait-idle" in call])
        self.assert_nothing_destructive()

    def test_the_reason_is_on_one_line_even_when_git_says_several(self):
        result = self.run_update(GIT_STATUS_ERROR="fatal: first line\nhint: second line")
        stopped = [line for line in result.stderr.splitlines() if "中止:" in line]
        self.assertEqual(len(stopped), 1, result.stderr)
        self.assertIn("first line", stopped[0])
        self.assertIn("second line", stopped[0])

    def test_a_folder_that_is_not_a_repository_stops_the_same_way(self):
        bare_dir = os.path.join(self.tmp, "not-a-repo")
        self.write(bare_dir, "VERSION", "6.0.0\n")
        shutil.copy(UPDATE_SH, self.write(bare_dir, "scripts/update.sh", "", 0o755))
        result = subprocess.run(
            ["bash", os.path.join(bare_dir, "scripts", "update.sh")], capture_output=True, text=True,
            encoding="utf-8", cwd=self.elsewhere, env=self.env, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("中止:", result.stderr)
        self.assertIn("not a git repository", result.stderr)

    def test_the_exit_code_is_not_gits_own_128(self):
        result = self.run_update(GIT_STATUS_ERROR="fatal: boom")
        self.assertEqual(result.returncode, 1)

    def test_no_temporary_files_are_left_behind(self):
        tmpdir = os.path.join(self.tmp, "mytmp")
        os.mkdir(tmpdir)
        self.run_update(GIT_STATUS_ERROR="fatal: boom", TMPDIR=tmpdir)
        self.write(self.pi, "VERSION", "x\n")
        self.run_update(TMPDIR=tmpdir)
        self.assertEqual(os.listdir(tmpdir), [])


class PullFailureTest(UpdateScriptCase):
    """``git pull`` の失敗は、git の表示から理由を見分けて、直し方を案内する。"""

    PERMISSION_MESSAGES = (
        "error: insufficient permission for adding an object to repository database .git/objects\n"
        "fatal: failed to write object\nfatal: unpack-objects failed",
        "fatal: Unable to create '/home/pi/campus-chime/.git/index.lock': Permission denied",
        "error: unable to unlink old 'VERSION': Operation not permitted",
        "error: unable to unlink old 'assets/voice/a.wav': Permission denied",
        "fatal: could not open '/home/pi/campus-chime/.git/objects/pack/tmp_pack_x' for writing",
        "error: cannot open .git/FETCH_HEAD: Permission denied",
        "ERROR: INSUFFICIENT PERMISSION FOR ADDING AN OBJECT",
    )

    def assert_stopped_untouched(self, result, before):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("中止:", result.stderr)
        self.assertIn("最新版を取り込めませんでした", result.stderr)
        self.assertIn("放送は今までどおり動いています", result.stderr)
        self.assertEqual(self.head(), before)
        calls = self.calls()
        self.assertFalse([call for call in calls if call.startswith("setup.sh")])
        self.assertFalse([call for call in calls if "--wait-idle" in call])
        self.assert_nothing_destructive()

    # -- 権限 -----------------------------------------------------------------
    def test_a_permission_failure_names_the_ownership_and_the_chown_for_the_invoking_user(self):
        before = self.head()
        for message in self.PERMISSION_MESSAGES:
            with self.subTest(message=message.splitlines()[0]):
                result = self.run_update(GIT_PULL_ERROR=message, FAKE_USER="alice", FAKE_GROUP="staff")
                self.assert_stopped_untouched(result, before)
                self.assertIn("sudo chown -R alice:staff {0}\n".format(self.pi), result.stderr)
                self.assertIn("権限（permission）", result.stderr)
                self.assertIn("ネットワークの問題ではありません", result.stderr)
                self.assertNotIn("ネットワークにつながっていないか", result.stderr)
                self.assertNotIn("食い違っています", result.stderr)

    def test_the_chown_names_the_group_as_well_as_the_user(self):
        result = self.run_update(GIT_PULL_ERROR=self.PERMISSION_MESSAGES[0])
        self.assertIn("sudo chown -R pi:pi {0}\n".format(self.pi), result.stderr)
        calls = self.calls()
        self.assertIn("id -un", calls)
        self.assertIn("id -gn", calls)

    def test_the_chown_covers_only_the_repository_and_quotes_its_path(self):
        spaced = os.path.join(self.tmp, "my pi")
        shutil.move(self.pi, spaced)
        self.pi = spaced
        result = self.run_update(GIT_PULL_ERROR=self.PERMISSION_MESSAGES[1])
        lines = [line.strip() for line in result.stderr.splitlines() if "sudo chown" in line]
        self.assertEqual(len(lines), 1, result.stderr)
        # シェルが 1 つの引数として読める形（空白がエスケープされている）で、リポジトリだけを指す。
        self.assertEqual(shlex.split(lines[0]), ["sudo", "chown", "-R", "pi:pi", spaced])

    def test_git_s_own_message_is_still_shown(self):
        message = self.PERMISSION_MESSAGES[0]
        result = self.run_update(GIT_PULL_ERROR=message)
        self.assertIn(message, result.stderr)
        self.assertLess(result.stderr.index(message), result.stderr.index("sudo chown"))

    def test_the_advice_says_to_rerun_and_that_a_half_done_pull_is_shown_as_local_changes(self):
        result = self.run_update(GIT_PULL_ERROR=self.PERMISSION_MESSAGES[3])
        self.assertIn("もう一度 bash scripts/update.sh を実行してください", result.stderr)
        self.assertIn("手元の変更", result.stderr)

    def test_a_failed_authentication_is_not_mistaken_for_a_file_permission(self):
        """ssh の ``Permission denied (publickey)`` は GitHub への接続の問題で、ファイルの持ち主ではない。"""
        for message in ("git@github.com: Permission denied (publickey).\n"
                        "fatal: Could not read from remote repository.\n\n"
                        "Please make sure you have the correct access rights\nand the repository exists.",
                        "fatal: Could not read from remote repository.",
                        "remote: Invalid username or password.\nfatal: Authentication failed for 'https://x/y.git/'"):
            with self.subTest(message=message.splitlines()[0]):
                result = self.run_update(GIT_PULL_ERROR=message)
                self.assertIn("ネットワークにつながっていないか", result.stderr)
                self.assertNotIn("chown", result.stderr)

    def test_a_permission_failure_after_a_real_pull_error_is_still_found_among_other_lines(self):
        message = "From github.com:x/y\n   abc..def  main -> origin/main\nerror: unable to unlink old 'VERSION': Permission denied\nfatal: Could not reset index file to revision 'HEAD'."
        result = self.run_update(GIT_PULL_ERROR=message)
        self.assertIn("sudo chown -R pi:pi", result.stderr)

    # -- 手元の Git 管理外のファイルが邪魔をする --------------------------------------
    def test_an_untracked_file_in_the_way_is_explained_not_blamed_on_the_network(self):
        self.add_to_origin("NEW.txt")
        self.write(self.pi, "NEW.txt", "この機械だけのメモ\n")
        before = self.head()
        result = self.run_update()
        self.assert_stopped_untouched(result, before)
        self.assertIn("would be overwritten", result.stderr)  # git 自身の表示
        self.assertIn("NEW.txt", result.stderr)
        self.assertIn("Git 管理外のファイル", result.stderr)
        self.assertIn("別の場所へ移し", result.stderr)
        self.assertNotIn("ネットワークにつながっていないか", result.stderr)
        self.assertNotIn("食い違っています", result.stderr)
        self.assertNotIn("chown", result.stderr)
        with open(os.path.join(self.pi, "NEW.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "この機械だけのメモ\n")  # 触らない

    def test_moving_the_file_away_lets_the_next_run_through(self):
        self.add_to_origin("NEW.txt")
        path = self.write(self.pi, "NEW.txt", "メモ\n")
        self.run_update()
        os.remove(path)
        again = self.run_update()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertTrue(os.path.exists(path))

    def test_a_stubbed_would_be_overwritten_message_is_recognised_too(self):
        message = ("error: The following untracked working tree files would be overwritten by merge:\n"
                   "\tNEW.txt\nPlease move or remove them before you merge.\nAborting")
        result = self.run_update(GIT_PULL_ERROR=message)
        self.assertIn("Git 管理外のファイル", result.stderr)
        self.assertNotIn("ネットワークにつながっていないか", result.stderr)

    # -- ディスク（SD カードが一杯・読み取り専用・入出力エラー・割り当て超過） ------------------
    DISK_MESSAGES = (
        "fatal: unable to write loose object file: No space left on device\nfatal: unpack-objects failed",
        "error: copy-fd: write returned: No space left on device\n"
        "fatal: failed to copy file to 'x/.git/objects/42/ab': No space left on device",
        "error: cannot open '.git/FETCH_HEAD': Read-only file system",
        "fatal: unable to create temporary file '.git/objects/pack/tmp_pack_x': Read-only file system",
        "fatal: Unable to create '/home/pi/campus-chime/.git/index.lock': Input/output error",
        "error: unable to create file assets/voice/a.wav: Disk quota exceeded\n"
        "fatal: Could not reset index file to revision 'HEAD'.",
    )

    #: ディスクの語句と、権限の語句（``unable to unlink`` / ``could not open`` / ``Permission denied``）が
    #: 一緒に出る表示。chown では直らないので、ディスクとして案内する。
    DISK_AND_PERMISSION_MESSAGES = (
        "error: unable to unlink old 'VERSION': Read-only file system",
        "fatal: could not open '/home/pi/campus-chime/.git/objects/pack/tmp_pack_x' for writing: "
        "Input/output error",
        "error: unable to unlink old 'assets/voice/a.wav': Input/output error",
        "error: could not open '.git/FETCH_HEAD' for writing: No space left on device",
        "error: unable to unlink old 'VERSION': Disk quota exceeded\nerror: Permission denied",
    )

    def test_a_full_or_failing_disk_names_the_disk_not_the_network(self):
        before = self.head()
        for message in self.DISK_MESSAGES:
            with self.subTest(message=message.splitlines()[0]):
                result = self.run_update(GIT_PULL_ERROR=message)
                self.assert_stopped_untouched(result, before)
                self.assertIn(message, result.stderr)  # git 自身の表示も見える
                self.assertIn("ディスクの問題", result.stderr)
                self.assertIn("ネットワークの問題ではありません", result.stderr)
                self.assertNotIn("ネットワークにつながっていないか", result.stderr)
                self.assertNotIn("食い違っています", result.stderr)
                self.assertNotIn("chown", result.stderr)
                self.assertNotIn("Git 管理外のファイル", result.stderr)

    def test_the_disk_advice_says_how_to_look_at_it_and_what_may_have_happened(self):
        result = self.run_update(GIT_PULL_ERROR=self.DISK_MESSAGES[0])
        self.assertIn("SD カード", result.stderr)
        self.assertIn("いっぱい", result.stderr)
        self.assertIn("読み取り専用", result.stderr)
        self.assertIn("df -h", result.stderr)
        self.assertIn("dmesg", result.stderr)

    def test_the_df_command_names_the_repository_and_quotes_its_path(self):
        spaced = os.path.join(self.tmp, "my pi")
        shutil.move(self.pi, spaced)
        self.pi = spaced
        result = self.run_update(GIT_PULL_ERROR=self.DISK_MESSAGES[0])
        lines = [line.strip() for line in result.stderr.splitlines() if line.strip().startswith("df ")]
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertEqual(shlex.split(lines[0]), ["df", "-h", spaced])

    def test_the_disk_advice_says_to_rerun_and_does_not_claim_that_nothing_was_changed(self):
        """途中まで書けた pull が、手元のファイルを変えているかもしれない（権限の案内と同じ）。"""
        result = self.run_update(GIT_PULL_ERROR=self.DISK_MESSAGES[0])
        self.assertIn("もう一度 bash scripts/update.sh を実行してください", result.stderr)
        self.assertIn("手元の変更", result.stderr)
        self.assertNotIn("何も変更していません", result.stderr)

    def test_a_disk_word_beside_a_permission_word_is_a_disk_problem(self):
        """読み取り専用・容量不足は、持ち主を直しても（chown しても）直らない。"""
        before = self.head()
        for message in self.DISK_AND_PERMISSION_MESSAGES:
            with self.subTest(message=message.splitlines()[0]):
                result = self.run_update(GIT_PULL_ERROR=message)
                self.assert_stopped_untouched(result, before)
                self.assertIn("df -h", result.stderr)
                self.assertNotIn("chown", result.stderr)
                self.assertNotIn("ネットワークにつながっていないか", result.stderr)

    def test_the_disk_phrases_are_found_in_any_case(self):
        for message in ("FATAL: NO SPACE LEFT ON DEVICE", "error: read-only file system",
                        "ERROR: INPUT/OUTPUT ERROR", "error: disk QUOTA exceeded"):
            with self.subTest(message=message):
                result = self.run_update(GIT_PULL_ERROR=message)
                self.assertIn("df -h", result.stderr)
                self.assertNotIn("ネットワークにつながっていないか", result.stderr)

    def test_a_disk_word_in_a_successful_pull_is_not_a_failure(self):
        """判定は失敗したときだけ。成功した pull の表示に語句があっても、止まらない。"""
        self.write(self.pi, os.path.join(".git", "hooks", "post-merge"),
                   "#!/bin/sh\necho 'No space left on device (a harmless note)' >&2\n", 0o755)
        self.advance_origin()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("df -h", result.stderr)

    def test_the_next_run_goes_through_once_the_disk_is_fine(self):
        self.advance_origin()
        before = self.head()
        stopped = self.run_update(GIT_PULL_ERROR=self.DISK_MESSAGES[0])
        self.assertEqual(stopped.returncode, 1)
        self.assertEqual(self.head(), before)
        again = self.run_update()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertNotEqual(self.head(), before)

    def test_no_temporary_files_are_left_behind_after_a_disk_failure(self):
        tmpdir = os.path.join(self.tmp, "mytmp")
        os.mkdir(tmpdir)
        self.run_update(GIT_PULL_ERROR=self.DISK_MESSAGES[0], TMPDIR=tmpdir)
        self.assertEqual(os.listdir(tmpdir), [])

    # -- 理由の見分け（語句を 1 つずつ・大文字小文字・優先順位） ------------------------------
    def assert_network_advice(self, message, label):
        result = self.run_update(GIT_PULL_ERROR=message)
        self.assertEqual(result.returncode, 1, label)
        self.assertIn("ネットワークにつながっていないか", result.stderr, label)
        self.assertNotIn("chown", result.stderr, label)

    def test_each_connection_phrase_alone_keeps_a_permission_word_from_meaning_file_ownership(self):
        """接続・認証の語句（``publickey`` / ``could not read from remote`` / ``authentication failed``）は、
        1 つだけでも、``Permission denied`` をファイルの持ち主の問題と読ませない（大文字でも同じ）。"""
        for label, message in (
                ("publickey だけ", "git@github.com: Permission denied (publickey)."),
                ("could not read だけ", "ssh: connect to host github.com port 22: Permission denied\n"
                                        "fatal: Could not read from remote repository."),
                ("authentication failed だけ", "remote: Permission denied.\n"
                                               "fatal: Authentication failed for 'https://x/y.git/'"),
                ("PUBLICKEY（大文字）", "GIT@GITHUB.COM: PERMISSION DENIED (PUBLICKEY)."),
                ("COULD NOT READ（大文字）",
                 "PERMISSION DENIED\nFATAL: COULD NOT READ FROM REMOTE REPOSITORY."),
                ("AUTHENTICATION FAILED（大文字）",
                 "PERMISSION DENIED\nFATAL: AUTHENTICATION FAILED FOR 'X'")):
            with self.subTest(label):
                self.assert_network_advice(message, label)

    def test_would_be_overwritten_is_found_in_any_case(self):
        result = self.run_update(GIT_PULL_ERROR="ERROR: THE FOLLOWING UNTRACKED WORKING TREE FILES "
                                                "WOULD BE OVERWRITTEN BY MERGE:\n\tx.txt")
        self.assertIn("上書きされてしまう", result.stderr)
        self.assertNotIn("ネットワークにつながっていないか", result.stderr)

    def test_a_message_with_both_a_permission_word_and_would_be_overwritten_is_a_permission_problem(self):
        message = ("error: unable to unlink old 'VERSION': Permission denied\n"
                   "error: Your local changes to the following files would be overwritten by merge:\n\tVERSION")
        result = self.run_update(GIT_PULL_ERROR=message)
        self.assertIn("sudo chown -R", result.stderr)
        self.assertNotIn("上書きされてしまう", result.stderr)

    def test_the_permission_advice_does_not_claim_that_nothing_was_changed(self):
        """途中まで進んだ pull が、手元のファイルを変えているかもしれない。"""
        result = self.run_update(GIT_PULL_ERROR=self.PERMISSION_MESSAGES[3])
        self.assertIn("sudo chown -R", result.stderr)
        self.assertNotIn("何も変更していません", result.stderr)

    def test_without_a_user_or_group_name_the_advice_has_placeholders(self):
        self.write(os.path.join(self.tmp, "bin"), "id", "#!/bin/sh\nexit 1\n", 0o755)
        result = self.run_update(GIT_PULL_ERROR=self.PERMISSION_MESSAGES[3])
        self.assertIn("いまの利用者（自分）", result.stderr)
        self.assertIn("sudo chown -R <ユーザー名>:<グループ名> {0}\n".format(self.pi), result.stderr)

    # -- 今までどおり -------------------------------------------------------------
    def test_a_diverged_history_and_an_unreachable_origin_keep_the_network_and_history_explanation(self):
        self.git("remote", "set-url", "origin", os.path.join(self.tmp, "no-such-origin.git"), cwd=self.pi)
        result = self.run_update()
        self.assertIn("ネットワークにつながっていないか", result.stderr)
        self.assertIn("食い違っています", result.stderr)
        self.assertIn("何も変更していません", result.stderr)
        self.assertNotIn("chown", result.stderr)
        self.assertIn("no-such-origin.git", result.stderr)  # git の表示も見える

    def test_the_stderr_of_a_successful_pull_is_shown_too(self):
        self.advance_origin()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("From ", result.stderr)

    def test_a_successful_pull_with_permission_in_its_output_is_not_a_failure(self):
        """判定は失敗したときだけ。成功した pull の表示に語句があっても、止まらない。"""
        self.write(self.pi, os.path.join(".git", "hooks", "post-merge"),
                   "#!/bin/sh\necho 'Permission denied (a harmless note)' >&2\n", 0o755)
        self.advance_origin()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("chown", result.stderr)

    # -- git の言語 ---------------------------------------------------------------
    def test_the_pull_is_run_in_the_c_locale_so_that_gits_words_can_be_found(self):
        self.run_update(LC_ALL="ja_JP.UTF-8", LANG="ja_JP.UTF-8", LANGUAGE="ja")
        self.assertEqual(self.pull_locale(), "C")

    def test_the_c_locale_is_only_for_the_pull(self):
        """ほかの git（状態の確認など）や python3 の言語設定は、そのまま。"""
        recorded = os.path.join(self.tmp, "locales")
        bin_dir = os.path.join(self.tmp, "bin")
        self.write(bin_dir, "python3", PYTHON_STUB.replace(
            'echo "python3 $*" >> "$CALLS_LOG"',
            'echo "python3 $*" >> "$CALLS_LOG"; echo "python3:${LC_ALL-unset}" >> ' + shlex.quote(recorded)), 0o755)
        self.run_update(LC_ALL="ja_JP.UTF-8")
        with open(recorded, encoding="utf-8") as handle:
            self.assertEqual(set(handle.read().split()), {"python3:ja_JP.UTF-8"})

    # -- 後始末 ---------------------------------------------------------------------
    def test_no_temporary_files_are_left_behind_after_a_failed_pull(self):
        tmpdir = os.path.join(self.tmp, "mytmp")
        os.mkdir(tmpdir)
        self.run_update(GIT_PULL_ERROR=self.PERMISSION_MESSAGES[0], TMPDIR=tmpdir)
        self.assertEqual(os.listdir(tmpdir), [])

    def test_the_failure_changes_no_file_in_the_working_tree(self):
        self.write(self.pi, "config.json", '{"_comment": "現地"}\n')
        self.advance_origin()
        self.run_update(GIT_PULL_ERROR=self.PERMISSION_MESSAGES[0])
        self.assertEqual(self.git("status", "--porcelain", cwd=self.pi), "")
        self.assertTrue(os.path.exists(os.path.join(self.pi, "config.json")))


class RealSetupIntegrationTest(UpdateScriptCase):
    """origin の ``setup.sh`` を本物にして、update.sh との約束（反映済みの記録）を通しで確かめる。

    ``sudo``・``systemctl``・``python3`` などは偽物なので、サービスは本当には再起動しない。
    再起動の呼び出しの回数と、``cache/deployed_commit`` で見る。
    """

    REAL_SETUP = True

    def test_an_update_restarts_once_and_records_the_deployed_commit(self):
        self.advance_origin()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.restarts()), 1)
        self.assertEqual(self.deployed(), self.head())

    def test_the_record_is_written_by_the_invoking_user_not_through_sudo(self):
        self.advance_origin()
        self.run_update()
        path = os.path.join(self.pi, "cache", "deployed_commit")
        self.assertEqual(os.stat(path).st_uid, os.getuid())
        self.assertFalse([call for call in self.calls() if call.startswith("sudo") and "deployed" in call])
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_running_again_does_not_restart(self):
        self.advance_origin()
        self.run_update()
        again = self.run_update()
        self.assertIn("すでに最新版です。", again.stdout)
        self.assertEqual(len(self.restarts()), 1)

    def test_a_rerun_after_the_broadcast_wait_failed_restarts_exactly_once_in_total(self):
        self.advance_origin()
        self.run_update(WAIT_IDLE_EXIT="1")
        self.assertEqual(self.restarts(), [])
        second = self.run_update()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(len(self.restarts()), 1)
        self.assertEqual(self.deployed(), self.head())
        self.run_update()
        self.assertEqual(len(self.restarts()), 1)

    def test_a_restart_withheld_by_an_unreadable_config_fails_the_update_and_a_rerun_continues(self):
        self.advance_origin()
        first = self.run_update(CHECK_EXIT="2")
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertIn("サービスを再起動していません", first.stderr)
        self.assertIn("導入スクリプトが途中で失敗しました", first.stderr)
        self.assertEqual(self.restarts(), [])
        self.assertEqual(self.deployed(), None)

        second = self.run_update()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(len(self.restarts()), 1)
        self.assertEqual(self.deployed(), self.head())

    def test_the_hurry_advice_after_exit_2_applies_the_update_only_once_the_config_is_fixed(self):
        """``--wait-idle`` が 2 のときの『setup.sh を実行しても反映できます』が、設定を直したあとだけ本当であること。"""
        self.advance_origin()
        self.run_update(WAIT_IDLE_EXIT="2")
        by_hand = ["bash", os.path.join(self.pi, "scripts", "setup.sh"), "--no-apt"]

        def run_setup_by_hand(**env):
            return subprocess.run(by_hand, capture_output=True, text=True, encoding="utf-8", cwd=self.pi,
                                  env=dict(self.env, **env), timeout=60)

        broken = run_setup_by_hand(CHECK_EXIT="2")
        self.assertEqual(broken.returncode, 1, broken.stdout + broken.stderr)
        self.assertEqual(self.restarts(), [])
        self.assertEqual(self.deployed(), None)
        fixed = run_setup_by_hand()
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertEqual(len(self.restarts()), 1)
        self.assertEqual(self.deployed(), self.head())

    def test_a_failing_restart_leaves_no_record(self):
        self.advance_origin()
        bin_dir = os.path.join(self.tmp, "bin")
        self.write(bin_dir, "sudo", '''#!/bin/sh
echo "sudo $*" >> "$CALLS_LOG"
case "$*" in *restart*) exit 1 ;; esac
''', 0o755)
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.deployed(), None)

    def test_the_record_is_replaced_when_a_new_version_is_deployed(self):
        self.advance_origin("6.1.0")
        self.run_update()
        first = self.deployed()
        self.advance_origin("6.2.0")
        self.run_update()
        self.assertNotEqual(self.deployed(), first)
        self.assertEqual(self.deployed(), self.head())
        self.assertEqual(len(self.restarts()), 2)


class ScriptHygieneTest(unittest.TestCase):
    def test_the_syntax_is_valid_bash(self):
        for name in ("update.sh", "setup.sh"):
            result = subprocess.run(["bash", "-n", os.path.join(REPO_ROOT, "scripts", name)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_it_is_strict_about_errors(self):
        with open(UPDATE_SH, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("set -euo pipefail", text)
        self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))


if __name__ == "__main__":
    unittest.main()
