"""導入スクリプト（``scripts/setup.sh``）のテスト。

setup.sh は Pi の上で人が手で実行するもので、CI でも他のテストでも走らない。
そのため壊れても気づかれにくく、次の 4 点をここで固定する。

* ``--help`` は、操作者が最初に読む説明。表示するのは先頭のコメントブロックだけで、
  その外の行（``set -euo pipefail``）まで出してはならない。以前は行番号を決め打ちした
  ``sed -n '2,10p'`` で、ヘッダを 1 行変えるたびにずれて、この行まで出ていた。
* 不明なオプションは、たいていタイプミス（``--no-appt`` など）。無視して先へ進むと
  apt の実行や systemd への登録まで走ってしまうため、副作用の前に終了コード 2 で止まる。
* 導入の途中で ``--check`` を走らせ、その終了コードで進み方を変える。設定ファイルを
  読めない（終了コード 2）ときだけ、サービスを再起動しない（読めない設定で再起動すると、
  起動できないまま再起動を繰り返す）。再起動を控えたときは「サービスを再起動していません」と
  目立つ行で言い、「完了」とは言わず、終了コード 1 で終わる（``update.sh`` がそれで失敗と分かる）。
  サービスを触らない指定（``--no-service``）でも、読めない設定のまま「完了」とは言わず、同じく
  終了コード 1 で終わる。
  同じ理由で ``--generate-assets`` が終了コード 2 になったときは、PC で音声を作り直す案内
  （直らない）を出さない。
* サービスの再起動に成功したら、そのときのコミットを ``cache/deployed_commit`` に書く
  （``update.sh`` が「サービスは最新か」を判断する記録）。書くのは実行している利用者で、
  root のときや、再起動しなかった・失敗したときは書かない。
* 新規に作る ``config.json`` の雛形は、既定値を 1 つも写さない「空の上書き」でなければ
  ならない。旧版は ``config.example.json``（既定値の完全なコピー）を複製しており、その時点の
  既定値が凍結されて、読み上げ文言が作り置き音声と食い違った
  （経緯は ``tests/test_config.py`` の ``RedundantKeysTest``）。
  雛形はシェルの here document に埋まっているので、スクリプトから取り出して調べる。

スクリプトは本物を ``bash`` で動かす。ただし、失敗したときに開発機や CI の
``sudo apt-get`` などを実際に走らせてしまわないよう、副作用のあるコマンドは
呼ばれた記録だけ残す偽物に差し替えた ``PATH`` で実行する。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

from tests.support import REPO_ROOT, logs_enabled

from chime.config import DEFAULT_CONFIG, load_config, redundant_keys

SETUP_SH = os.path.join(REPO_ROOT, "scripts", "setup.sh")

#: 引数の解釈より後で呼ばれるはずのコマンド。どれか 1 つでも呼ばれたら、
#: 引数の解釈の前に副作用へ進んでいる。
SIDE_EFFECT_COMMANDS = ("sudo", "apt-get", "systemctl", "timedatectl", "python3")

#: setup.sh が config.json を作る here document。開始行と終了行（``EOF``）をそのまま
#: 錨にしているので、書き方（引用符の有無・区切り語・出力先）を変えたら、見つからずに
#: テストが落ちる。そのときは、ここを新しい書き方に合わせる。
CONFIG_TEMPLATE = re.compile(
    r"""^[ \t]*cat > "\$\{REPO_DIR\}/config\.json" <<'EOF'\n(?P<body>.*?)\nEOF$""",
    re.DOTALL | re.MULTILINE)


def read_setup_script() -> str:
    with open(SETUP_SH, "r", encoding="utf-8") as handle:
        return handle.read()


def header_block() -> str:
    """スクリプト先頭のコメントブロックを返す（``--help`` が表示するはずの範囲）。

    2 行目（1 行目は shebang）から、``#`` で始まらない最初の行の手前まで。
    期待値を行番号で持たず、ファイル自身から決めるので、ヘッダを書き換えても追従する。
    """
    block = []
    for line in read_setup_script().splitlines()[1:]:
        if not line.startswith("#"):
            break
        block.append(line)
    return "".join(line + "\n" for line in block)


class SetupScriptCase(unittest.TestCase):
    """setup.sh を ``bash`` で動かすための共通部分。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # 実行場所はリポジトリの外。--help が相対パスでなく ``$0`` から自分を読むこと、
        # 作業ディレクトリに依存しないことも、ここで確かめられる。
        self.cwd = os.path.join(tmp.name, "work")
        os.mkdir(self.cwd)
        self.calls_log = os.path.join(tmp.name, "calls.log")

        bin_dir = os.path.join(tmp.name, "bin")
        os.mkdir(bin_dir)
        for name in SIDE_EFFECT_COMMANDS:
            path = os.path.join(bin_dir, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\necho {0} >> {1}\n".format(
                    name, shlex.quote(self.calls_log)))
            os.chmod(path, 0o755)
        # bash や sed は元の PATH から引くので、偽物は先頭に足すだけにする。
        self.env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""))

    def run_setup(self, *args):
        # 日本語の出力を、実行環境のロケールに依らず UTF-8 として読む。
        return subprocess.run(
            ["bash", SETUP_SH] + list(args),
            capture_output=True, text=True, encoding="utf-8",
            cwd=self.cwd, env=self.env, timeout=30)

    def assert_no_side_effects(self):
        calls = ""
        if os.path.exists(self.calls_log):
            with open(self.calls_log, "r", encoding="utf-8") as handle:
                calls = handle.read()
        self.assertEqual(calls, "", "引数の解釈の前に副作用のあるコマンドが呼ばれた")


class HelpTest(SetupScriptCase):
    """``--help`` / ``-h`` は、先頭のコメントブロックだけを表示して正常終了する。"""

    def test_the_expected_block_is_the_documented_header(self):
        # 期待値そのものの確認。ヘッダが空になったり、``set -euo pipefail`` を
        # 含んだりしていたら、下の比較が無意味になる。
        expected = header_block()
        self.assertIn("--no-apt", expected)
        self.assertNotIn("set -euo pipefail", expected)

    def test_help_prints_exactly_the_header_block(self):
        result = self.run_setup("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 行番号の決め打ちで実際に起きた不具合（ヘッダの次の行まで出る）。
        # 全体の比較より先に調べて、落ちたときに原因が分かるようにする。
        self.assertNotIn("set -euo pipefail", result.stdout)
        self.assertEqual(result.stdout, header_block())
        self.assertEqual(result.stderr, "")
        self.assert_no_side_effects()

    def test_help_lists_the_documented_options(self):
        result = self.run_setup("--help")
        self.assertIn("--no-apt", result.stdout)
        self.assertIn("--no-service", result.stdout)

    def test_short_help_prints_the_same_as_long_help(self):
        short = self.run_setup("-h")
        self.assertEqual(short.returncode, 0, short.stderr)
        self.assertEqual(short.stdout, self.run_setup("--help").stdout)
        self.assertEqual(short.stdout, header_block())
        self.assert_no_side_effects()


class UnknownOptionTest(SetupScriptCase):
    """不明なオプションは、副作用の前に終了コード 2 で止まる。"""

    def test_an_unknown_option_exits_with_2_and_names_it(self):
        result = self.run_setup("--bogus")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("不明なオプション: --bogus", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assert_no_side_effects()

    def test_a_valid_option_before_an_unknown_one_does_not_start_the_setup(self):
        # 先に出てきた有効なオプションで、導入が始まってしまわないこと。
        result = self.run_setup("--no-apt", "--bogus")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("不明なオプション: --bogus", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assert_no_side_effects()


#: 偽物の ``python3``。呼ばれた記録を残し、モードごとの終了コードを環境変数で決める。
PYTHON_STUB = """#!/bin/sh
echo "python3 $*" >> "$CALLS_LOG"
case "$*" in
  *--generate-assets*) exit "${GENERATE_EXIT:-0}" ;;
  *--check*) echo "CHECK-OUTPUT"; exit "${CHECK_EXIT:-0}" ;;
esac
"""

#: 呼ばれた記録だけ残す偽物（``sudo`` は何も実行しない）。
LOG_STUB = """#!/bin/sh
echo "{name} $*" >> "$CALLS_LOG"
"""

#: 偽物の ``id``。記録は残さない（呼び出しの一覧を変えない）。``FAKE_UID`` が実行している利用者の uid。
ID_STUB = """#!/bin/sh
echo "${FAKE_UID:-1000}"
"""

#: 再起動の瞬間に、反映済みの記録がまだ無いこと（再起動のあとで書くこと）を記録する偽物の ``sudo``。
SUDO_WATCHING_STUB = """#!/bin/sh
echo "sudo $*" >> "$CALLS_LOG"
case "$*" in
  *restart*)
    if [ -e cache/deployed_commit ]; then echo "record-at-restart: present" >> "$CALLS_LOG"
    else echo "record-at-restart: absent" >> "$CALLS_LOG"; fi
    exit "${RESTART_EXIT:-0}" ;;
esac
"""

#: 偽物の ``timedatectl``。タイムゾーンは東京、時刻は同期済みと答える。
TIMEDATECTL_STUB = """#!/bin/sh
case "$*" in
  *Timezone*) echo "Asia/Tokyo" ;;
  *) echo "yes" ;;
esac
"""


class SetupFlowCase(unittest.TestCase):
    """setup.sh を一時フォルダの「リポジトリ」に置いて、導入の流れを最後まで動かす。

    ``sudo``・``systemctl``・``python3`` は偽物。``sleep`` は待たない。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.join(tmp.name, "repo")
        self.calls_log = os.path.join(tmp.name, "calls.log")
        os.makedirs(os.path.join(self.repo, "scripts"))
        shutil.copy(SETUP_SH, os.path.join(self.repo, "scripts", "setup.sh"))

        bin_dir = os.path.join(tmp.name, "bin")
        os.mkdir(bin_dir)
        stubs = {"python3": PYTHON_STUB, "timedatectl": TIMEDATECTL_STUB, "id": ID_STUB}
        for name in ("sudo", "systemctl", "apt-get", "sleep"):
            stubs[name] = LOG_STUB.format(name=name)
        for name, text in stubs.items():
            path = os.path.join(bin_dir, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(path, 0o755)
        self.env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""),
                        CALLS_LOG=self.calls_log)

    def run_setup(self, *args, **env):
        return subprocess.run(
            ["bash", os.path.join(self.repo, "scripts", "setup.sh")] + list(args),
            capture_output=True, text=True, encoding="utf-8", cwd=self.repo,
            env=dict(self.env, **env), timeout=60)

    def calls(self):
        """呼ばれたコマンドを順に返す（リポジトリの場所は ``$REPO`` に置き換える）。"""
        if not os.path.exists(self.calls_log):
            return []
        with open(self.calls_log, "r", encoding="utf-8") as handle:
            return [line.replace(self.repo, "$REPO") for line in handle.read().splitlines()]


class CheckStepTest(SetupFlowCase):
    """時報音を生成したあとの ``--check`` と、その終了コードによるサービスの扱い。"""

    GENERATE = "python3 $REPO/campus_chime.py --generate-assets"
    CHECK = "python3 $REPO/campus_chime.py --check"
    SCHEDULE = "python3 $REPO/campus_chime.py --schedule 5"
    REGISTER = [
        "sudo cp $REPO/campus_chime.service /etc/systemd/system/campus_chime.service",
        "sudo systemctl daemon-reload",
        "sudo systemctl enable campus_chime.service",
    ]
    RESTART = "sudo systemctl restart campus_chime.service"
    STATUS = "sudo systemctl status campus_chime.service --no-pager"

    def test_the_check_runs_after_the_time_signal_and_the_service_is_restarted_after_it(self):
        result = self.run_setup("--no-apt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call for call in self.calls() if not call.startswith("sleep")],
            [self.GENERATE, self.CHECK, self.SCHEDULE] + self.REGISTER + [self.RESTART, self.STATUS])
        self.assertIn("CHECK-OUTPUT", result.stdout)
        self.assertIn("設置状態の点検", result.stdout)

    def test_a_check_with_ng_still_restarts_the_service_and_says_what_to_do(self):
        result = self.run_setup("--no-apt", CHECK_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.RESTART, self.calls())
        self.assertIn(self.SCHEDULE, self.calls())
        self.assertIn("点検で NG が見つかりました", result.stderr)
        self.assertIn("直し方", result.stderr)

    def test_an_unreadable_config_skips_the_restart_and_says_why(self):
        result = self.run_setup("--no-apt", CHECK_EXIT="2")
        calls = self.calls()
        self.assertNotIn(self.RESTART, calls)
        self.assertFalse([call for call in calls if "restart" in call])
        # 登録（コピー・有効化）までは行う。再起動だけを控える。
        for command in self.REGISTER:
            self.assertIn(command, calls)
        self.assertIn("設定ファイル（config.json）を読めませんでした", result.stderr)
        self.assertIn("サービスを再起動していません", result.stderr)
        self.assertIn("もう一度 bash scripts/setup.sh --no-apt", result.stderr)

    def test_a_withheld_restart_is_a_prominent_line_and_a_failure_not_a_completion(self):
        """再起動を控えたのに、終了コード 0 と「完了」で終わると、update.sh も人も成功と思ってしまう。"""
        result = self.run_setup("--no-apt", CHECK_EXIT="2")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("完了", result.stdout)
        self.assertNotIn("次の手順で実際に音が出るか", result.stdout)
        self.assertIn("導入は途中です", result.stdout)
        # 目立つ行（赤・太字の ``!!``）が、本文の途中と最後の 2 回出る。
        prominent = [line for line in result.stderr.splitlines()
                     if line.startswith("\033[1;31m!! ") and "サービスを再起動していません" in line]
        self.assertEqual(len(prominent), 2, result.stderr)
        self.assertTrue(result.stderr.rstrip().splitlines()[-1].startswith("\033[1;31m!! "))

    def test_the_status_of_the_unit_is_still_shown_when_the_restart_is_withheld(self):
        self.run_setup("--no-apt", CHECK_EXIT="2")
        self.assertIn(self.STATUS, self.calls())

    def test_without_the_service_an_unreadable_config_does_not_claim_a_withheld_restart(self):
        """サービスを触らない指定（``--no-service``）のとき、再起動を「控えた」ことにはならない。"""
        result = self.run_setup("--no-apt", "--no-service", CHECK_EXIT="2")
        self.assertNotIn("サービスを再起動していません", result.stderr)
        self.assertIn("設定ファイル（config.json）を読めませんでした", result.stderr)

    def test_without_the_service_an_unreadable_config_still_fails_and_is_not_a_completion(self):
        """ヘッダー・README・SETUP は「設定ファイルを読めないときは終了コード 1」と、例外なしで言っている。

        ``--no-service`` でも、終了コード 0 と「完了」の案内で終わると、読めない設定を残したまま
        導入が済んだように見える。
        """
        result = self.run_setup("--no-apt", "--no-service", CHECK_EXIT="2")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("完了", result.stdout)
        self.assertNotIn("次の手順で実際に音が出るか", result.stdout)
        self.assertIn("導入は途中です", result.stdout)
        last = result.stderr.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("\033[1;31m!! "), last)
        self.assertIn("config.json を直して", last)
        self.assertIn("bash scripts/setup.sh --no-apt --no-service", last)
        self.assertNotIn("サービスを再起動していません", last)

    def test_without_the_service_an_unreadable_config_registers_nothing_and_skips_the_preview(self):
        result = self.run_setup("--no-apt", "--no-service", CHECK_EXIT="2")
        self.assertEqual([call for call in self.calls() if not call.startswith("sleep")],
                         [self.GENERATE, self.CHECK])
        self.assertIn("設定ファイルを読めないため、省略します。", result.stdout)

    def test_without_the_service_a_check_that_is_clean_or_has_ng_still_completes(self):
        for check_exit in ("0", "1"):
            with self.subTest(check_exit=check_exit):
                result = self.run_setup("--no-apt", "--no-service", CHECK_EXIT=check_exit)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("完了", result.stdout)
                self.assertNotIn("導入は途中です", result.stdout)

    def test_the_header_promises_exit_1_for_an_unreadable_config_without_exceptions(self):
        """ヘッダー（``--help`` の表示）の約束と、上の挙動が食い違わないこと。"""
        header = header_block()
        self.assertIn("設定ファイル（config.json）を読めないときは、サービスを再起動せず、終了コード 1 で終わる", header)
        self.assertNotIn("--no-service のとき", header)

    def test_an_unreadable_config_also_skips_the_schedule_preview(self):
        """同じ理由で失敗する確認を走らせて、導入全体を途中で止めてしまわない。"""
        result = self.run_setup("--no-apt", CHECK_EXIT="2")
        self.assertNotIn(self.SCHEDULE, self.calls())
        self.assertIn("設定ファイルを読めないため、省略します。", result.stdout)

    def test_missing_voices_get_the_voicevox_advice(self):
        result = self.run_setup("--no-apt", GENERATE_EXIT="1")
        self.assertIn("VOICEVOX", result.stderr)
        self.assertIn("generate_voicevox.py", result.stderr)
        self.assertIn("docs/SETUP.md", result.stderr)

    def test_an_unreadable_config_in_generate_assets_gets_no_voicevox_advice(self):
        """終了コード 2 は設定ファイルを読めないとき。PC で作り直しても直らない案内は出さない。"""
        result = self.run_setup("--no-apt", GENERATE_EXIT="2")
        self.assertNotIn("VOICEVOX", result.stderr)
        self.assertNotIn("generate_voicevox", result.stderr)
        self.assertNotIn("作り置き", result.stderr)
        self.assertIn("設定ファイル（config.json）を読めない", result.stderr)

    def test_an_unreadable_config_in_generate_assets_still_runs_the_check_which_says_why(self):
        self.run_setup("--no-apt", GENERATE_EXIT="2")
        self.assertIn(self.CHECK, self.calls())

    def test_the_check_runs_even_when_the_voices_could_not_be_confirmed(self):
        result = self.run_setup("--no-apt", GENERATE_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index(self.GENERATE), calls.index(self.CHECK))
        self.assertIn(self.RESTART, calls)

    def test_without_the_service_the_check_still_runs_and_nothing_is_registered(self):
        result = self.run_setup("--no-apt", "--no-service")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call for call in self.calls() if not call.startswith("sleep")],
            [self.GENERATE, self.CHECK, self.SCHEDULE])
        self.assertIn("--no-service が指定されたため", result.stdout)

    def test_a_nonzero_check_status_does_not_abort_the_setup(self):
        """``set -e`` の下でも、--check が 0 以外を返して導入が途中で終わらない（最後の案内まで進む）。

        NG（1）は直し方を見てもらいつつ、そのまま再起動して完了する。読めない設定（2）は、
        最後まで進んだうえで、再起動を控えたことを言って終了コード 1 で終わる。
        """
        result = self.run_setup("--no-apt", CHECK_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("完了", result.stdout)
        result = self.run_setup("--no-apt", CHECK_EXIT="2")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("導入は途中です", result.stdout)
        self.assertIn(self.STATUS, self.calls())


class SystemctlTest(SetupFlowCase):
    """再起動と状態表示の細部。"""

    def stub(self, name, text):
        bin_dir = self.env["PATH"].split(os.pathsep)[0]
        path = os.path.join(bin_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, 0o755)

    def test_a_failing_systemctl_status_does_not_abort_the_setup(self):
        """``systemctl status`` は、停止中だと 0 以外を返す。それで導入が終わらない（最後まで進む）。"""
        self.stub("sudo", '''#!/bin/sh
echo "sudo $*" >> "$CALLS_LOG"
case "$*" in *status*) exit 3 ;; esac
''')
        result = self.run_setup("--no-apt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("完了", result.stdout)

    def test_it_waits_two_seconds_between_the_restart_and_the_status(self):
        self.run_setup("--no-apt")
        calls = self.calls()
        index = calls.index("sudo systemctl restart campus_chime.service")
        self.assertEqual(calls[index + 1], "sleep 2")
        self.assertEqual(calls[index + 2], "sudo systemctl status campus_chime.service --no-pager")

    def test_a_failing_restart_aborts_the_setup(self):
        self.stub("sudo", SUDO_WATCHING_STUB)
        result = self.run_setup("--no-apt", RESTART_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("完了", result.stdout)


class DeployedCommitTest(SetupFlowCase):
    """再起動に成功したら、そのときのコミットを ``cache/deployed_commit`` に書く。"""

    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git が無い環境では、コミットを記録できない")
        self.git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.git_env.update(HOME=self.repo, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                            GIT_AUTHOR_NAME="tester", GIT_AUTHOR_EMAIL="tester@example.invalid",
                            GIT_COMMITTER_NAME="tester", GIT_COMMITTER_EMAIL="tester@example.invalid")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "初期")
        self.record = os.path.join(self.repo, "cache", "deployed_commit")
        bin_dir = self.env["PATH"].split(os.pathsep)[0]
        with open(os.path.join(bin_dir, "sudo"), "w", encoding="utf-8") as handle:
            handle.write(SUDO_WATCHING_STUB)
        os.chmod(os.path.join(bin_dir, "sudo"), 0o755)
        # 偽物の PATH などはそのままに、git の設定だけ足す（利用者の設定や署名に左右されない）。
        self.env.update({key: value for key, value in self.git_env.items()
                         if key.startswith("GIT_") or key == "HOME"})

    def git(self, *args):
        return subprocess.run(["git"] + list(args), cwd=self.repo, env=self.git_env, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()

    def recorded(self):
        if not os.path.exists(self.record):
            return None
        with open(self.record, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_the_head_commit_is_recorded_after_the_restart(self):
        result = self.run_setup("--no-apt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded(), self.git("rev-parse", "HEAD") + "\n")

    def test_it_is_recorded_after_the_restart_not_before(self):
        self.run_setup("--no-apt")
        self.assertIn("record-at-restart: absent", self.calls())
        self.assertNotIn("record-at-restart: present", self.calls())

    def test_the_record_follows_the_head(self):
        self.run_setup("--no-apt")
        first = self.recorded()
        with open(os.path.join(self.repo, "NOTE"), "w", encoding="utf-8") as handle:
            handle.write("次\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "次")
        self.run_setup("--no-apt")
        self.assertNotEqual(self.recorded(), first)
        self.assertEqual(self.recorded(), self.git("rev-parse", "HEAD") + "\n")

    def test_it_is_written_by_the_invoking_user_and_leaves_no_temporary_file(self):
        self.run_setup("--no-apt")
        self.assertEqual(os.stat(self.record).st_uid, os.getuid())
        self.assertEqual(os.listdir(os.path.dirname(self.record)), ["deployed_commit"])
        self.assertFalse([call for call in self.calls() if call.startswith("sudo") and "deployed" in call])

    def test_nothing_is_recorded_when_the_restart_is_withheld(self):
        self.run_setup("--no-apt", CHECK_EXIT="2")
        self.assertIsNone(self.recorded())

    def test_nothing_is_recorded_when_the_restart_fails(self):
        self.run_setup("--no-apt", RESTART_EXIT="1")
        self.assertIsNone(self.recorded())

    def test_nothing_is_recorded_without_the_service(self):
        self.run_setup("--no-apt", "--no-service")
        self.assertIsNone(self.recorded())

    def test_a_check_with_ng_still_records_because_the_service_was_restarted(self):
        self.run_setup("--no-apt", CHECK_EXIT="1")
        self.assertEqual(self.recorded(), self.git("rev-parse", "HEAD") + "\n")

    def test_it_is_not_written_when_run_as_root(self):
        """root が作ると ``cache/`` が root 所有になり、サービス（pi）が状態を書けなくなる。"""
        result = self.run_setup("--no-apt", FAKE_UID="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.recorded())
        self.assertFalse(os.path.exists(os.path.join(self.repo, "cache")))
        self.assertIn("root で実行しているため", result.stderr)

    def test_outside_a_repository_it_warns_and_still_completes(self):
        shutil.rmtree(os.path.join(self.repo, ".git"))
        result = self.run_setup("--no-apt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("完了", result.stdout)
        self.assertIsNone(self.recorded())
        self.assertIn("コミットを調べられない", result.stderr)

    def test_a_cache_folder_that_cannot_be_made_only_warns(self):
        with open(os.path.join(self.repo, "cache"), "w", encoding="utf-8") as handle:
            handle.write("ファイル\n")
        result = self.run_setup("--no-apt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("完了", result.stdout)
        self.assertIn("反映した版の記録（cache/deployed_commit）を書けませんでした", result.stderr)

    def test_an_existing_record_is_replaced_even_if_it_cannot_be_overwritten_in_place(self):
        os.makedirs(os.path.dirname(self.record))
        with open(self.record, "w", encoding="utf-8") as handle:
            handle.write("古い\n")
        os.chmod(self.record, 0o444)
        self.run_setup("--no-apt")
        self.assertEqual(self.recorded(), self.git("rev-parse", "HEAD") + "\n")


class ConfigTemplateTest(unittest.TestCase):
    """setup.sh が作る config.json の雛形は、既定値を写さない空の上書きである。"""

    def template_text(self) -> str:
        matches = CONFIG_TEMPLATE.findall(read_setup_script())
        self.assertEqual(
            len(matches), 1,
            "scripts/setup.sh に config.json を作る here document が 1 つだけ見つからない"
            "（書き方を変えたなら、このテストの CONFIG_TEMPLATE を合わせる）")
        return matches[0]

    def template(self) -> dict:
        data = json.loads(self.template_text())
        self.assertIsInstance(data, dict)
        return data

    def test_the_template_is_valid_json(self):
        # 壊れた JSON を書くと、利用者の次の起動が設定の読み込みで失敗する。
        self.template()

    def test_the_template_holds_only_comment_keys(self):
        # 設定項目を 1 つも持たない（"_" で始まる説明用のキーだけ）。項目を書くと、
        # その値は以後の既定値の変更を上書きし続ける。
        template = self.template()
        self.assertIn("_comment", template)
        self.assertEqual([key for key in template if not key.startswith("_")], [])

    def test_the_template_repeats_no_default(self):
        self.assertEqual(redundant_keys(self.template()), [])

    def test_loading_the_template_neither_warns_nor_changes_the_defaults(self):
        # 起動時の「既定値の丸ごとコピー」警告（``_warn_if_defaults_were_copied``）は
        # 非公開なので、公開の ``load_config`` を通して、警告が出ないことを確かめる。
        # 警告は既定値と同じ項目が 10 を超えて初めて出る（``redundant_keys`` の確認の方が
        # 厳しい）が、利用者が実際に目にするのはこちらの経路。
        # ``assertLogs`` は 1 件も出ないと失敗するので、判定用のダミーを先に出す
        # （``tests/test_config.py`` の ``_load_capturing_warnings`` と同じ作り）。
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as handle:
                handle.write(self.template_text())
            with logs_enabled(), self.assertLogs("chime.config", level="WARNING") as captured:
                logging.getLogger("chime.config").warning("dummy")
                config = load_config(base_dir=tmp)
        warnings = [line for line in captured.output if not line.endswith("dummy")]
        self.assertEqual(warnings, [])
        effective = {key: value for key, value in config.data.items()
                     if not key.startswith("_")}
        self.assertEqual(effective, DEFAULT_CONFIG)


if __name__ == "__main__":
    unittest.main()
