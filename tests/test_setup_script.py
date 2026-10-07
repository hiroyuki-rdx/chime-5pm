"""導入スクリプト（``scripts/setup.sh``）のテスト。

setup.sh は Pi の上で人が手で実行するもので、CI でも他のテストでも走らない。
そのため壊れても気づかれにくく、次の 3 点をここで固定する。

* ``--help`` は、操作者が最初に読む説明。表示するのは先頭のコメントブロックだけで、
  その外の行（``set -euo pipefail``）まで出してはならない。以前は行番号を決め打ちした
  ``sed -n '2,10p'`` で、ヘッダを 1 行変えるたびにずれて、この行まで出ていた。
* 不明なオプションは、たいていタイプミス（``--no-appt`` など）。無視して先へ進むと
  apt の実行や systemd への登録まで走ってしまうため、副作用の前に終了コード 2 で止まる。
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
