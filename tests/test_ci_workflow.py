"""CI の定義（``.github/workflows/ci.yml``）のテスト。

CI は押してみるまで結果が分からず、定義を間違えても「緑のまま何も調べていない」ことが
ある。次の 3 点を、ファイルを読んで固定する（通信も、GitHub の実行も使わない）。

* ``test`` ジョブの Python の版に下限の 3.9（と、実機の 3.11）が入っている。
* ``prerecorded-only`` ジョブに ``--check`` の段がある（合成エンジンの無い環境で、
  設置状態の点検が通ること）。
* ``scripts/*.sh`` の **どれも** ``bash -n`` で 1 本ずつ構文を調べ、``shellcheck`` にかける。
  以前は ``bash -n scripts/setup.sh scripts/update.sh`` と書いていて、``bash -n`` は
  2 つ目以降を「引数」として扱うため、update.sh の構文は調べられていなかった。

文字列を探すだけだと、書き方を変えただけで落ちる。そこで ``bash -n`` と ``shellcheck`` の
段は、書かれたコマンドを **実際に動かして** 確かめる。``scripts/`` の写しを作り、その 1 本を
わざと壊して、段が失敗することを見る（どの 1 本を壊しても）。

YAML の解析器は標準ライブラリに無いので、このファイルの書き方（字下げ 2・6 の
``- name:`` / ``run:`` / ``run: |``）だけを読む最小の解析を使う。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from typing import List, NamedTuple

from tests.support import REPO_ROOT

CI_YML = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

#: ``bash -n`` に構文エラーと見なされる行。
SYNTAX_ERROR = "if then fi (\n"
#: ``shellcheck`` が既定の水準で指摘する行（クォートされていない変数）。
SHELLCHECK_ISSUE = "echo $unquoted_variable\n"


class Step(NamedTuple):
    """ジョブの段 1 つ。"""

    name: str
    run: str
    uses: str
    #: 段の定義の生の行（``continue-on-error`` などを探すのに使う）。
    raw: str


def read_ci() -> str:
    with open(CI_YML, "r", encoding="utf-8") as handle:
        return handle.read()


def job_block(text: str, job: str) -> List[str]:
    """``jobs:`` の下の ``  <job>:`` から、次のジョブの手前までの行。"""
    lines = text.splitlines()
    start = lines.index("  {0}:".format(job))
    block = []
    for line in lines[start + 1:]:
        if re.match(r"^  [A-Za-z0-9_-]+:", line) or re.match(r"^\S", line):
            break
        block.append(line)
    return block


def job_steps(text: str, job: str) -> List[Step]:
    """ジョブの段を、書かれた順に返す（``- name:`` / ``- uses:`` で始まるもの）。"""
    block = job_block(text, job)
    groups: List[List[str]] = []
    for line in block:
        if re.match(r"^      - ", line):
            groups.append([line])
        elif groups:
            groups[-1].append(line)
    return [parse_step(group) for group in groups]


def parse_step(lines: List[str]) -> Step:
    """段の行（先頭は ``      - ``）から、名前・コマンド・``uses`` を取り出す。"""
    fields = {}
    index = 0
    body = ["        " + lines[0][8:]] + lines[1:]   # 先頭の ``- `` を空白にして、キーの字下げをそろえる
    while index < len(body):
        match = re.match(r"^        (name|run|uses): ?(.*)$", body[index])
        index += 1
        if not match:
            continue
        key, value = match.groups()
        if key == "run" and value.strip() in ("|", ">"):
            block = []
            while index < len(body) and (body[index].startswith("          ") or not body[index].strip()):
                block.append(body[index][10:])
                index += 1
            value = "\n".join(block).strip("\n")
        fields[key] = value.strip()
    return Step(fields.get("name", ""), fields.get("run", ""), fields.get("uses", ""),
                "\n".join(lines))


def script_names() -> List[str]:
    return sorted(os.path.basename(path) for path in glob.glob(os.path.join(SCRIPTS_DIR, "*.sh")))


def steps_running(text: str, job: str, command: str) -> List[Step]:
    """``job`` の段のうち、``command``（``bash -n`` など）を含むもの。"""
    return [step for step in job_steps(text, job) if re.search(r"(^|\s){0}(\s|$)".format(re.escape(command)),
                                                               step.run)]


class ParserTest(unittest.TestCase):
    """この解析が、ci.yml の書き方を正しく読めること（以降のテストの土台）。"""

    TEXT = """\
jobs:
  first:
    steps:
      - uses: actions/checkout@v4
      - name: 一行
        run: echo one
      - name: 複数行
        run: |
          for f in a b; do
            echo "$f"
          done

          echo end
  second:
    steps:
      - name: 別のジョブ
        run: echo two
"""

    def test_steps_are_split_per_job_and_in_order(self):
        steps = job_steps(self.TEXT, "first")
        self.assertEqual([step.name for step in steps], ["", "一行", "複数行"])
        self.assertEqual(steps[0].uses, "actions/checkout@v4")
        self.assertEqual([step.name for step in job_steps(self.TEXT, "second")], ["別のジョブ"])

    def test_a_single_line_run_and_a_block_run_are_read(self):
        steps = job_steps(self.TEXT, "first")
        self.assertEqual(steps[1].run, "echo one")
        self.assertEqual(steps[2].run, 'for f in a b; do\n  echo "$f"\ndone\n\necho end')

    def test_the_real_workflow_has_the_jobs_the_tests_below_rely_on(self):
        text = read_ci()
        for job in ("test", "lint", "shell", "prerecorded-only"):
            self.assertTrue(job_steps(text, job), job)


class PythonMatrixTest(unittest.TestCase):
    def versions(self):
        block = "\n".join(job_block(read_ci(), "test"))
        match = re.search(r"python-version:\s*\[([^\]]*)\]", block)
        self.assertTrue(match, "test ジョブに python-version の一覧がありません")
        return [item.strip().strip("\"'") for item in match.group(1).split(",")]

    def test_the_oldest_supported_python_is_in_the_matrix(self):
        self.assertIn("3.9", self.versions())

    def test_the_python_of_the_raspberry_pi_is_in_the_matrix(self):
        """実機（Raspberry Pi OS Bookworm）の Python は 3.11。"""
        self.assertIn("3.11", self.versions())

    def test_the_matrix_has_a_newer_python_too(self):
        newest = max(tuple(int(part) for part in version.split(".")) for version in self.versions())
        self.assertGreaterEqual(newest, (3, 13))

    def test_every_version_is_run_even_when_one_fails(self):
        block = "\n".join(job_block(read_ci(), "test"))
        self.assertIn("fail-fast: false", block)

    def test_the_unit_tests_are_run_on_every_version(self):
        runs = " ".join(step.run for step in job_steps(read_ci(), "test"))
        self.assertIn("unittest discover -s tests -t .", runs)


class CheckStepTest(unittest.TestCase):
    """合成エンジンの無い環境（``prerecorded-only``）で、設置状態の点検が通ること。"""

    def test_the_prerecorded_only_job_runs_check(self):
        steps = steps_running(read_ci(), "prerecorded-only", "campus_chime.py --check")
        self.assertEqual(len(steps), 1, [step.run for step in job_steps(read_ci(), "prerecorded-only")])
        self.assertEqual(steps[0].run, "python campus_chime.py --check")

    def test_the_check_runs_after_the_phrases_were_confirmed(self):
        steps = job_steps(read_ci(), "prerecorded-only")
        names = [step.run for step in steps]
        say = next(index for index, run in enumerate(names) if "--say" in run)
        check = next(index for index, run in enumerate(names) if "campus_chime.py --check" in run)
        self.assertLess(say, check)

    def test_a_failing_check_fails_the_job(self):
        [step] = steps_running(read_ci(), "prerecorded-only", "campus_chime.py --check")
        self.assertNotIn("||", step.run)
        self.assertNotIn("continue-on-error", step.raw)
        self.assertNotIn("if:", step.raw)


class ShellJobTest(unittest.TestCase):
    """``scripts/*.sh`` の全部が、``bash -n`` と ``shellcheck`` にかかること。"""

    def setUp(self):
        self.text = read_ci()
        self.names = script_names()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = tmp.name

    def test_there_are_scripts_to_check(self):
        self.assertIn("setup.sh", self.names)
        self.assertIn("update.sh", self.names)

    # -- 書き方 -----------------------------------------------------------
    def test_bash_n_never_gets_two_files_in_one_command(self):
        """``bash -n a b`` は a しか調べない（b は引数）。1 回の呼び出しに 1 本だけ渡す。"""
        for step in steps_running(self.text, "shell", "bash -n"):
            for line in step.run.splitlines():
                match = re.match(r"^\s*bash -n\s+(.*?)\s*$", line)
                if match:
                    self.assertEqual(len(match.group(1).split()), 1, line)

    def test_there_is_a_bash_n_step_and_a_shellcheck_step(self):
        self.assertTrue(steps_running(self.text, "shell", "bash -n"))
        self.assertTrue(steps_running(self.text, "shell", "shellcheck"))

    # -- 動かして確かめる ---------------------------------------------------
    def place_scripts(self, broken=None, line=SYNTAX_ERROR):
        """作業フォルダに ``scripts/`` の写しを作る。``broken`` の 1 本だけ、末尾に ``line`` を足す。"""
        scripts = os.path.join(self.work, "scripts")
        shutil.rmtree(scripts, ignore_errors=True)
        os.makedirs(scripts)
        for name in self.names:
            shutil.copy(os.path.join(SCRIPTS_DIR, name), os.path.join(scripts, name))
        if broken:
            with open(os.path.join(scripts, broken), "a", encoding="utf-8") as handle:
                handle.write("\n" + line)

    def run_steps(self, command):
        """``command`` を含む段のコマンドを、GitHub Actions と同じ ``bash -e`` で動かす。"""
        steps = steps_running(self.text, "shell", command)
        self.assertTrue(steps, command)
        outputs = []
        for step in steps:
            result = subprocess.run(["bash", "-e", "-c", step.run], cwd=self.work, capture_output=True,
                                    text=True, encoding="utf-8", timeout=120)
            outputs.append(result)
            if result.returncode != 0:
                return result
        return outputs[-1]

    def test_bash_n_passes_on_the_real_scripts(self):
        self.place_scripts()
        result = self.run_steps("bash -n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bash_n_fails_whichever_single_script_has_a_syntax_error(self):
        """どの 1 本が壊れても段が失敗する（先頭でも、2 本目以降でも）。"""
        for name in self.names:
            with self.subTest(script=name):
                self.place_scripts(broken=name)
                result = self.run_steps("bash -n")
                self.assertNotEqual(result.returncode, 0, "{0} の構文エラーを見逃した".format(name))

    def test_each_script_is_syntax_checked_by_its_own_invocation(self):
        """実行された ``bash -n`` の引数を数える（ラッパーに差し替えて、呼び出しを記録する）。"""
        self.place_scripts()
        bin_dir = os.path.join(self.work, "bin")
        os.makedirs(bin_dir)
        log = os.path.join(self.work, "bash.log")
        real_bash = shutil.which("bash")
        wrapper = os.path.join(bin_dir, "bash")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\necho "$#:$*" >> "{0}"\nexec "{1}" "$@"\n'.format(log, real_bash))
        os.chmod(wrapper, 0o755)
        steps = steps_running(self.text, "shell", "bash -n")
        for step in steps:
            subprocess.run([real_bash, "-e", "-c", step.run], cwd=self.work, capture_output=True,
                           env=dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"]),
                           timeout=120, check=True)
        with open(log, encoding="utf-8") as handle:
            calls = [line.strip() for line in handle if line.strip()]
        self.assertEqual(sorted(calls), sorted("2:-n scripts/{0}".format(name) for name in self.names))

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck が無い環境では、段を動かせない")
    def test_shellcheck_passes_on_the_real_scripts(self):
        self.place_scripts()
        result = self.run_steps("shellcheck")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck が無い環境では、段を動かせない")
    def test_shellcheck_fails_whichever_single_script_has_an_issue(self):
        for name in self.names:
            with self.subTest(script=name):
                self.place_scripts(broken=name, line=SHELLCHECK_ISSUE)
                result = self.run_steps("shellcheck")
                self.assertNotEqual(result.returncode, 0, "{0} の指摘を見逃した".format(name))

    def test_shellcheck_is_given_every_script(self):
        """shellcheck を動かす代わりに、渡すファイルを数える（shellcheck が無い環境でも確かめられる）。"""
        self.place_scripts()
        bin_dir = os.path.join(self.work, "bin")
        os.makedirs(bin_dir)
        log = os.path.join(self.work, "shellcheck.log")
        wrapper = os.path.join(bin_dir, "shellcheck")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nfor a in "$@"; do echo "$a" >> "{0}"; done\n'.format(log))
        os.chmod(wrapper, 0o755)
        for step in steps_running(self.text, "shell", "shellcheck"):
            subprocess.run(["bash", "-e", "-c", step.run], cwd=self.work, capture_output=True,
                           env=dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"]),
                           timeout=60, check=True)
        with open(log, encoding="utf-8") as handle:
            given = sorted(line.strip() for line in handle if line.strip())
        self.assertEqual(given, sorted("scripts/{0}".format(name) for name in self.names))


class DocumentedClaimsTest(unittest.TestCase):
    """ci.yml のコメントが言っていることが、定義と合っていること。"""

    def test_the_comment_about_bash_n_matches_the_steps(self):
        text = read_ci()
        self.assertIn("1 本ずつ", text)
        self.assertNotRegex(text, r"bash -n scripts/\S+\.sh scripts/\S+\.sh")


if __name__ == "__main__":
    unittest.main()
