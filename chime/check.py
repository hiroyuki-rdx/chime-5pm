"""設置状態の点検（``--check``）。

Raspberry Pi の現地で「この機械はちゃんと放送できる状態か」を、鳴らさずに
確かめる。点検するのは次の 4 つ。

設定
    書き方の問題（:func:`chime.configcheck.check_config`）。
作り置きの音声
    放送で読み上げる全文言に、使える作り置き（``assets/voice/``）の声があるか。
    ``tts.engines`` に ``prerecorded`` が無いと、放送は作り置きを引かないので、声が
    そろっていても Pi ではすべて無音になる（NG）。
    Pi には実行時の音声合成が無く、声が無い文言はその文だけ無音になる。空・壊れた
    ファイルは「無い」ものとして数える。ひとことの定義ファイルが読めないときは、
    内蔵の予備（3 件）で動くので警告を出す。
音源
    閉館アナウンス・蛍の光・時報音のファイルが、最後まで読める状態か。WAV は音のデータを
    最後まで読み、MP3 は MPEG のフレームを 1 つずつたどって、最後のフレームがファイルの
    終わりで切れていないか・途中に MP3 でないデータが挟まっていないか・ID3 タグのあとに
    フレームが無い（タグまでしか書けていない）ものでないかを確かめる。
書き込み
    放送が書き込む場所に書けるか。NG にするのは、再生済みの記録（``state.json``）が
    残せなくなるものだけ（残せないと、再起動のたびに同じ回が鳴り直しうる）。放送の
    履歴や音声のキャッシュは、書けなくても放送が止まらないので警告にとどめる。
    ``state.json`` は一時ファイルに書いて置き換えるので、root 所有のファイルが残って
    いても、置き場所のフォルダに書ければ困らない（所有者は NG にしない）。ただし sticky
    ビットのあるフォルダ（``/tmp`` など）は別で、フォルダも既存の ``state.json`` も自分の
    持ち物でないと置き換えを OS に断られるので、その組み合わせは NG にする。

**何も書かない**（ファイルを作らず、権限も変えない）。書き込みの確認は
``os.access`` と読み出しだけで行う。結果は OK / 警告 / NG で、NG が 1 つでもあれば
終了コード 1、警告だけなら 0。設定ファイルを読めないとき（終了コード 2）は、
設定を読み込む側（``chime.cli``）がここへ来る前に返す。

直し方に出す ``chown`` は、設定が指す場所そのものにしか使わない。まだ無い場所には
その 1 つだけを作って渡し（``mkdir -p`` のあとに再帰なしの ``chown``）、実在する親
（``/`` や ``/var/lib`` など、設定の持ち物でないもの）を巻き込まない。
"""

from __future__ import annotations

import os
import shlex
import stat
import sys
import unicodedata
import wave
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple

from . import configcheck
from .config import DEFAULT_CONFIG, Config
from .jsonfile import JsonFileError, read_json
from .phrases import PHRASE_KINDS, Coverage, CoverageTooLarge, coverage
from .quotes import FALLBACK_QUOTES
from .tts import PrerecordedEngine

OK = "ok"
INFO = "info"
WARNING = "warning"
NG = "ng"

#: 行頭の目印。全角 2 文字が半角 4 文字分の幅なので、桁がそろう。
_MARKS = {OK: "OK  ", INFO: "情報", WARNING: "警告", NG: "NG  "}

#: :class:`chime.configcheck.Finding` の level → ここの level。
_FINDING_LEVELS = {configcheck.ERROR: NG, configcheck.WARNING: WARNING, configcheck.INFO: INFO}

#: 文言の種類（:data:`chime.phrases.PHRASE_KINDS`）の表示名。
_KIND_LABELS = {"announce": "時刻アナウンス", "closing": "閉館の追加文言",
                "quote": "ひとこと", "weather": "天気"}

#: 足りない文言を、そのまま挙げる数。
LIST_LIMIT = 10
#: 「既定値と同じ」などの情報を、そのまま挙げる数（残りは件数だけ）。
INFO_LIMIT = 5
#: 項目名の列の最大の幅（半角換算）。これより長い項目名は、列をはみ出してよい。
TITLE_WIDTH = 28

SETUP_COMMAND = "bash scripts/setup.sh --no-apt"

#: MP3 として小さすぎるとみなす大きさ（バイト）。途中で切れたファイルを見つける目安で、
#: 1 秒分の音でも 8 KB 前後になる。
MIN_MP3_BYTES = 4096
#: WAV を最後まで読むときの、1 回に読むフレーム数（大きなファイルでも一度に読み込まない）。
_READ_FRAMES = 65536

#: 書き込み先の目的。
STATE = "state"
HISTORY = "history"
CACHE = "cache"
_ROLE_ORDER = (STATE, HISTORY, CACHE)

#: 目的ごとの、書けないときの ``(level, 困ること)``。NG にするのは、再生済みの記録
#: （``state.json``）が残せなくなるものだけ。履歴・音声のキャッシュは、書けなくても
#: 放送が止まらないので警告にとどめる。
_CONSEQUENCES = {
    STATE: (NG, "再生済みの記録を保存できません。再起動したとき、同じ回が鳴り直すことがあります"),
    HISTORY: (WARNING, "放送の履歴を新しく書けないことがあります。放送は止まりません"),
    CACHE: (WARNING, "合成した音声を保存できません。作り置きだけで放送する Pi には影響しません"),
}

#: 持ち主を変えてはならない OS の場所。設定がここを指していても、``chown`` は案内せず、
#: 設定を書ける場所に変えるよう案内する。
_SYSTEM_DIRECTORIES = frozenset([
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/media", "/mnt", "/opt", "/proc",
    "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var", "/var/lib", "/var/log",
    "/var/tmp"])

#: 書き込み先の目的 → それを指す設定のキー（OS の場所を指しているとき、設定を変える案内に使う）。
_ROLE_KEYS = {STATE: "state.file", HISTORY: "state.history_file", CACHE: "tts.cache_dir"}

_GENERIC_FIX = "サービスを動かす利用者が書き込めるようにしてください"


@dataclass(frozen=True)
class Result:
    """点検の結果 1 件（画面の 1 項目）。"""

    level: str
    title: str
    detail: str = ""
    #: 直し方（あれば、詳細の下の行に出す）。
    fix: str = ""
    #: 詳細の続き（足りない文言の一覧など。1 行ずつ字下げして出す）。
    lines: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------
def display_width(text: str) -> int:
    """端末での表示幅（全角を 2、半角を 1 と数える）。"""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def pad(text: str, width: int) -> str:
    """表示幅が ``width`` になるまで、右に空白を足す（足りていれば、そのまま）。"""
    return text + " " * max(0, width - display_width(text))


def render_section(heading: str, results: Sequence[Result]) -> List[str]:
    """節の見出しと、項目の行（項目名の桁をそろえる）を返す。"""
    width = min(TITLE_WIDTH, max((display_width(result.title) for result in results), default=0))
    lines = ["== {0} ==".format(heading)]
    for result in results:
        head = "  {0}  {1}  ".format(_MARKS[result.level], pad(result.title, width))
        indent = " " * display_width(head)
        lines.append((head + result.detail).rstrip())
        lines.extend(indent + line for line in result.lines)
        if result.fix:
            lines.append("{0}直し方: {1}".format(indent, result.fix))
    return lines


def summarize(results: Sequence[Result]) -> str:
    """結果の一行まとめ（NG と警告の件数）。"""
    problems = sum(1 for result in results if result.level == NG)
    warnings = sum(1 for result in results if result.level == WARNING)
    if not problems and not warnings:
        return "結果: すべて OK です。"
    parts = []
    if problems:
        parts.append("NG {0} 件".format(problems))
    if warnings:
        parts.append("警告 {0} 件".format(warnings))
    return "結果: {0}。".format("・".join(parts))


def shown_path(config: Config, path: str) -> str:
    """表示用のパス。リポジトリの中なら相対、外なら絶対のまま（リポジトリそのものは ``.`` でなく絶対）。"""
    if not path:
        return ""
    relative = os.path.relpath(path, config.base_dir)
    return path if relative.startswith("..") or relative == os.curdir else relative


# ---------------------------------------------------------------------------
# 1. 設定
# ---------------------------------------------------------------------------
def check_settings(config: Config) -> List[Result]:
    """読み込んだ設定の出どころと、書き方の問題（error → warning → info の順）。"""
    results = [Result(OK, "読み込んだ設定", _sources_text(config))]
    findings = configcheck.check_config(config)
    infos = [finding for finding in findings if finding.level == configcheck.INFO]
    others = [finding for finding in findings if finding.level != configcheck.INFO]
    results.extend(_finding_result(finding) for finding in others)
    results.extend(_finding_result(finding) for finding in infos[:INFO_LIMIT])
    if len(infos) > INFO_LIMIT:
        results.append(Result(INFO, "（ほか）", "情報があと {0} 件あります".format(len(infos) - INFO_LIMIT)))
    if not others:
        results.append(Result(OK, "設定の書き方", "問題は見つかりませんでした"))
    return results


def _sources_text(config: Config) -> str:
    """読み込んだ設定ファイルを、読んだ順に ``→`` でつなぐ。"""
    files = [source for source in config.sources if not source.startswith("<")]
    if not files:
        return "既定値だけ（config.json はありません）"
    return " → ".join(["既定値"] + files)


def _finding_result(finding: configcheck.Finding) -> Result:
    detail = finding.message + ("  [{0}]".format(finding.source) if finding.source else "")
    return Result(_FINDING_LEVELS.get(finding.level, WARNING), finding.key or "設定ファイル",
                  detail, finding.hint)


# ---------------------------------------------------------------------------
# 2. 作り置きの音声
# ---------------------------------------------------------------------------
def prerecorded_coverage(config: Config, broken: Optional[List[str]] = None) -> Coverage:
    """読み上げる全文言のうち、作り置きの声がある数・無い文言を数える。

    引くのは作り置きだけ（``tts.engines`` の設定に関わらず、合成エンジンには
    問い合わせない）。PC で VOICEVOX が動いていても結果は変わらず、Pi と同じ
    答えになる。放送が作り置きを引くかどうか（``tts.engines`` に ``prerecorded`` が
    あるか）は :func:`prerecorded_listed` が答える（無ければ、ここで数えた声は使われない）。

    ファイルがあっても、空・開けない・WAV として壊れている・途中で切れているものは、
    声が「無い」ものとして数える（放送ではその文が無音になる）。``broken`` を渡すと、
    そのような（ファイルはあるのに使えない）文言をそこへ足す。
    """
    engine = PrerecordedEngine({}, config.path("tts.prerecorded_dir"))

    def usable(text: str) -> Optional[str]:
        path = engine.lookup(text)
        if path is None:
            return None
        if _sound_problem(path):
            if broken is not None:
                broken.append(text)
            return None
        return path

    return coverage(config, usable)


def prerecorded_listed(config: Config) -> bool:
    """放送が作り置きの声を引くか（``tts.engines`` に ``prerecorded`` があるか）。

    :class:`chime.tts.TTSService` と同じ読み方をする（``engines`` を 1 つずつ文字列にして
    ``"prerecorded"`` と比べる）。したがって、リストでなく文字列を書いたときは 1 文字ずつの
    名前になって、``False``。反復できない値も ``False``（サービスは起動で例外になる）。
    ``False`` の Pi では、声がディスクにそろっていても、読み上げがすべて無音になる。
    """
    engines = config.section("tts").get("engines", [])
    try:
        return any(str(name) == "prerecorded" for name in engines)
    except (TypeError, ValueError):  # 反復できない値、または文字列にできない（桁数が多すぎる）整数
        return False


def check_voices(config: Config) -> List[Result]:
    """作り置きの声が、読み上げる全文言にそろっているか。ひとこと定義の読み込みも見る。"""
    return _voice_results(config) + check_quotes(config)


def _voice_results(config: Config) -> List[Result]:
    if not prerecorded_listed(config):
        return [Result(
            NG, "作り置き",
            "tts.engines に \"prerecorded\" がないため、作り置きの声を使いません。"
            "Pi では読み上げがすべて無音になります",
            "tts.engines に \"prerecorded\" を加えてください（既定は [\"prerecorded\", \"voicevox\"]）")]
    directory = config.path("tts.prerecorded_dir")
    if not os.path.isdir(directory):
        return [Result(
            NG, "作り置き",
            "フォルダ {0} が見つかりません。読み上げがすべて無音になります".format(
                shown_path(config, directory)),
            "git pull が届いているか、設置場所を確認してください")]
    broken: List[str] = []
    try:
        found = prerecorded_coverage(config, broken)
    except Exception as exc:  # 文言の数え上げは設定の値に左右される。点検は止めない
        # CoverageTooLarge は、何をどう直すかまで日本語で述べてある。型名は見せない（--status と同じ）
        reason = str(exc) if isinstance(exc, CoverageTooLarge) else "{0}: {1}".format(type(exc).__name__, exc)
        return [Result(NG, "作り置き", "読み上げる文言を数えられません: {0}".format(reason),
                       "time_signal・closing・weather の文言の設定を確認してください")]
    return [_coverage_result(found, len(broken), shown_path(config, directory))]


def coverage_breakdown(found: Coverage) -> str:
    """種類ごとの ``声がある件数/全件数``（件数が 0 の種類は挙げない）。"""
    return "・".join(
        "{0} {1}/{2}".format(_KIND_LABELS[kind], *found.by_kind[kind])
        for kind in PHRASE_KINDS if found.by_kind[kind][1])


def _coverage_result(found: Coverage, broken: int = 0, directory: str = "assets/voice") -> Result:
    breakdown = coverage_breakdown(found)
    if found.ok:
        return Result(OK, "作り置き",
                      "{0} 件すべてそろっています（{1}）".format(found.total, breakdown))
    lines = ["「{0}」".format(text) for text in found.missing[:LIST_LIMIT]]
    if len(found.missing) > LIST_LIMIT:
        lines.append("ほか {0} 件".format(len(found.missing) - LIST_LIMIT))
    detail = "{0} 件中 {1} 件の声がありません（{2}）。その文は無音になります".format(
        found.total, len(found.missing), breakdown)
    fix = "PC で作り直して commit し、この Pi で git pull する（docs/SETUP.md 8 章）"
    if broken:
        detail += "。うち {0} 件はファイルがあるのに使えません（空・壊れている・読めない）".format(broken)
        fix = "使えないファイルは git checkout -- {0} で戻す。声そのものが無いときは、{1}".format(
            shlex.quote(directory), fix)
    return Result(NG, "作り置き", detail, fix, tuple(lines))


def check_quotes(config: Config) -> List[Result]:
    """ひとこと（``quotes.file``）を読めるか。読めないときだけ警告を出す（読めれば何も出さない）。

    読めなくても放送は止まらない。内蔵の予備（:data:`chime.quotes.FALLBACK_QUOTES`）の
    ひとことで動くので、NG にはしない。
    """
    path = config.path("quotes.file")
    if not path:
        return []
    shown = shown_path(config, path)
    fallback = "内蔵の予備 {0} 件だけを使います".format(len(FALLBACK_QUOTES))
    try:
        data = read_json(path)
    except JsonFileError as exc:
        reason = "{0} が見つかりません".format(shown) if exc.kind == "missing" else str(exc)
        return [Result(WARNING, "ひとこと", "{0}（{1}）".format(reason, fallback),
                       _restore_hint("quotes.file", shown))]
    if not _quote_count(data):
        return [Result(WARNING, "ひとこと", "{0} に使えるひとことがありません（{1}）".format(shown, fallback),
                       _restore_hint("quotes.file", shown))]
    return []


def _quote_count(data: Any) -> int:
    """ひとこと定義にある文の数（``chime.quotes`` が受け付ける形：配列、または general・by_hour）。"""
    if isinstance(data, list):
        data = {"general": data}
    if not isinstance(data, Mapping):
        return 0
    by_hour = data.get("by_hour") or {}
    groups = [data.get("general", [])]
    if isinstance(by_hour, Mapping):
        groups.extend(by_hour.values())
    return sum(1 for group in groups if isinstance(group, list) for item in group if str(item))


# ---------------------------------------------------------------------------
# 3. 音源
# ---------------------------------------------------------------------------
def check_sounds(config: Config, euid: Optional[int] = None,
                 account: Optional[str] = None) -> List[Result]:
    """閉館アナウンス・蛍の光・時報音のファイルが、最後まで読める状態か。

    ``euid`` と ``account`` は :func:`check_writable` と同じ（時報音を作る場所に書けない
    ときの直し方に使う）。
    """
    euid, account = _identity(euid, account)
    return [
        _sound_result(config, "閉館アナウンス", "closing.announce_file"),
        _sound_result(config, "蛍の光", "closing.music_file"),
        _time_signal_result(config, euid, account),
    ]


def _sound_result(config: Config, title: str, key: str) -> Result:
    """同梱の音源 1 つ。無い・壊れているのは NG（放送でその部品が欠ける）。"""
    path = config.path(key)
    if not path:
        return Result(OK, title, "指定なし（鳴らしません）")
    shown = shown_path(config, path)
    if not os.path.exists(path):
        return Result(NG, title, "{0} が見つかりません".format(shown), _restore_hint(key, shown))
    problem = _sound_problem(path)
    if problem:
        return Result(NG, title, "{0}: {1}".format(shown, problem), _restore_hint(key, shown))
    return Result(OK, title, shown)


def _time_signal_result(config: Config, euid: Optional[int], account: str) -> Result:
    """時報音。無いのは警告（放送のときに自動で作る）。あるのに開けないのは NG。

    無くて、作る場所にも書けないときは、自動で作れず時報音が鳴らないので NG。
    """
    title = "時報音"
    path = config.path("time_signal.output_file")
    if not path:
        return Result(OK, title, "指定なし（鳴らしません）")
    shown = shown_path(config, path)
    if not os.path.exists(path):
        directory = os.path.dirname(path)
        probe, problem = _creation_problem(directory)
        if problem == "file":
            return Result(NG, title, "{0} がまだ無く、{1} がフォルダではないので作れません（時報音が鳴りません）".format(
                shown, shown_path(config, probe)), "そのファイルを移すか消してください")
        if problem == "unwritable":
            return Result(NG, title, "{0} がまだ無く、{1} に書けないので作れません（時報音が鳴りません）".format(
                shown, shown_path(config, probe)),
                _directory_fix(directory, probe, ("time_signal.output_file",), account, euid))
        return Result(WARNING, title, "{0} がまだありません（放送のときに自動で作ります）".format(shown),
                      SETUP_COMMAND)
    problem_text = _sound_problem(path)
    if problem_text:
        return Result(NG, title, "{0}: {1}".format(shown, problem_text), SETUP_COMMAND)
    return Result(OK, title, shown)


def _restore_hint(key: str, shown: str) -> str:
    """同梱のファイルを戻す方法。既定の場所なら git で戻せる。別の場所なら設定を確かめる。"""
    section, _, name = key.partition(".")
    if shown == DEFAULT_CONFIG[section][name]:
        return "git checkout -- {0}".format(shown)
    return "設定の {0} のパスを確認してください".format(key)


def _sound_problem(path: str) -> Optional[str]:
    """音源として使えない理由。使えるなら ``None``。拡張子に応じて中身を確かめる。"""
    suffix = os.path.splitext(path)[1].lower()
    try:
        size = os.path.getsize(path)
        if size == 0:
            return "空のファイルです"
        if suffix == ".wav":
            return _wav_problem(path)
        if suffix == ".mp3":
            return _mp3_problem(path, size)
    except OSError as exc:
        return "読めません（{0}）".format(exc)
    return None


def _wav_problem(path: str) -> Optional[str]:
    """WAV として開けない・音が無い・宣言どおりの長さが最後まで入っていない、のいずれか。

    ヘッダーだけ見ると、電源断などで途中までしか書けなかったファイルが通ってしまう
    （``wave`` はヘッダーの宣言どおりに開く）。そのため音のデータを最後まで読んで、
    宣言したフレーム数とバイト数が合うかを確かめる。
    """
    try:
        with wave.open(path, "rb") as handle:
            frames = handle.getnframes()
            if frames == 0:
                return "音のデータがありません（0 フレーム）"
            frame_size = handle.getsampwidth() * handle.getnchannels()
            actual = 0
            while True:
                chunk = handle.readframes(_READ_FRAMES)
                if not chunk:
                    break
                actual += len(chunk)
    except OSError:
        raise  # 読めない（権限・フォルダだった等）は、呼び出し側が「読めません」と答える
    except Exception as exc:  # wave は壊れ方により wave.Error・EOFError・struct.error などを出す
        return "WAV として開けません（{0}）".format(str(exc) or type(exc).__name__)
    if actual < frames * frame_size:
        return "音のデータが途中で切れています（宣言 {0} フレームのうち {1} フレームしかありません）".format(
            frames, actual // frame_size)
    return None


def _mp3_problem(path: str, size: int) -> Optional[str]:
    """MP3 として読めない・小さすぎる・途中で切れているか壊れている、のいずれか。

    先頭が ID3 タグか MPEG のフレーム同期（0xFF の次の 3 ビットが 1）でなければ、MP3 ではない。
    先頭が正しくても :data:`MIN_MP3_BYTES` に満たなければ、途中で切れたものとみなす。
    そのうえで MPEG のフレームをファイルの終わりまで 1 つずつたどる
    （:func:`_mp3_frame_problem`）。
    """
    with open(path, "rb") as handle:
        head = handle.read(3)
        if not (head == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)):
            return "MP3 として読めません（先頭が ID3 でも MPEG のフレームでもありません）"
        if size < MIN_MP3_BYTES:
            return "小さすぎます（{0} バイト。途中で切れている可能性があります）".format(size)
        return _mp3_frame_problem(handle, size)


#: MPEG オーディオのビットレート表（kbps。添字 = ヘッダーのビットレート指数 - 1）。
#: キーは ``(MPEG の版: 1 なら MPEG-1、2 なら MPEG-2 と 2.5, レイヤー)``。
_MPEG_BITRATES = {
    (1, 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
#: サンプリング周波数（Hz。添字 = ヘッダーの指数）。キーはヘッダーの版ビット（3: MPEG-1、2: MPEG-2、0: MPEG-2.5）。
_MPEG_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}

#: フレームをたどるとき、1 回に読むバイト数（ヘッダーの 4 バイトだけを使い、各バイトは 1 度しか読まない）。
_MP3_CHUNK = 65536
#: ファイルの終わりに許す 0 埋めの最大バイト数。電源断で OS が長さだけ伸ばして中身を 0 で
#: 埋めたファイル（ブロック単位、通常 4096 バイト）を見逃さないよう、小さく取る。
_MP3_MAX_PADDING = 1024
#: ID3v1 タグの大きさ（ファイルの最後の 128 バイト。先頭は ``TAG``）。
_ID3V1_BYTES = 128
#: APEv2 タグのヘッダー・フッターの大きさと目印。
_APE_BYTES = 32
_APE_MAGIC = b"APETAGEX"
#: 先頭に重なっている ID3v2 タグを、読み飛ばす最大の数。
_ID3V2_MAX_TAGS = 4
#: ID3v2 タグの直後にフレームが無いとき、フレームを探す範囲（タグの直後からのバイト数）。タグの大きさに
#: 数えられていない余分なデータ（0 埋めなど）が挟まる MP3 があるので、直後でなくてもよい。この範囲に
#: 続けて 2 つのフレームのヘッダーがなければ、タグまでしか書けていない（フレームが無い）とみなす。
_MP3_FRAME_SEARCH_BYTES = 65536


def _mpeg_frame_length(second: int, third: int) -> int:
    """MPEG オーディオのフレームヘッダーの 2・3 バイト目から、フレームの長さ（バイト）を返す。

    同期（先頭の 0xFF と、2 バイト目の上位 3 ビット）・版・レイヤー・ビットレート・サンプリング
    周波数が正しくなければ 0（フレームではない）。ビットレート指数が 0（フリーフォーマット）の
    フレームは長さを決められないので、これも 0。
    """
    if second & 0xE0 != 0xE0:
        return 0
    version = (second >> 3) & 3  # 3: MPEG-1、2: MPEG-2、0: MPEG-2.5、1: 予約
    layer = 4 - ((second >> 1) & 3)  # 1〜3。0 ビットなら 4（予約）
    bitrate_index = third >> 4
    rate_index = (third >> 2) & 3
    if version == 1 or layer == 4 or bitrate_index in (0, 15) or rate_index == 3:
        return 0
    bitrate = _MPEG_BITRATES[(1 if version == 3 else 2, layer)][bitrate_index - 1] * 1000
    rate = _MPEG_SAMPLE_RATES[version][rate_index]
    padding = (third >> 1) & 1
    if layer == 1:
        return (12 * bitrate // rate + padding) * 4
    if layer == 3 and version != 3:
        return 72 * bitrate // rate + padding
    return 144 * bitrate // rate + padding


def _id3v2_length(head: bytes) -> Optional[int]:
    """ID3v2 タグ 1 つの長さ（ヘッダー 10 バイトと、あればフッターを含む）。``head``（そのタグの先頭の
    10 バイト）がタグでなければ ``None``。

    長さは**そのタグの先頭からの**バイト数。ファイルの先頭からの位置にするには、前のタグまでの
    長さに足していく。
    """
    if len(head) < 10 or head[:3] != b"ID3":
        return None
    sizes = head[6:10]
    if any(byte & 0x80 for byte in sizes):  # syncsafe 整数は各バイトの最上位ビットが 0
        return None
    body = (sizes[0] << 21) | (sizes[1] << 14) | (sizes[2] << 7) | sizes[3]
    footer = 10 if head[3] >= 4 and head[5] & 0x10 else 0  # フッターがあるのは ID3v2.4 だけ
    return 10 + body + footer


def _frames_follow(handle: Any, pos: int) -> bool:
    """``pos`` から :data:`_MP3_FRAME_SEARCH_BYTES` バイトの範囲に、フレームのヘッダーが続けて 2 つあるか。

    1 つ目のヘッダーが示す長さの先に、次のヘッダーがあることを確かめる（1 つだけでは、音のデータや
    でたらめなバイトが偶然ヘッダーの形になっていることがある）。2 つのヘッダーは、どちらも範囲に
    収まっていなければならない。
    """
    handle.seek(pos)
    window = handle.read(_MP3_FRAME_SEARCH_BYTES)
    start = window.find(b"\xff")
    while 0 <= start <= len(window) - 4:
        length = _mpeg_frame_length(window[start + 1], window[start + 2])
        following = start + length
        if (length and following + 4 <= len(window) and window[following] == 0xFF
                and _mpeg_frame_length(window[following + 1], window[following + 2])):
            return True
        start = window.find(b"\xff", start + 1)
    return False


def _mp3_frame_problem(handle: Any, size: int) -> Optional[str]:
    """MPEG のフレームをファイルの終わりまで 1 つずつたどり、切れ・壊れの理由を返す。問題なければ ``None``。

    ID3v2 タグを（syncsafe の大きさで）読み飛ばし、フレームのヘッダー（版・レイヤー・
    ビットレート・サンプリング周波数・パディング）から長さを求めて次のフレームへ進む。
    Xing / Info のフレームも普通のフレームとして数える。

    * 最後のフレームがファイルの終わりを越える、または ID3v2 タグが終わりを越える: 途中で切れている。
    * フレームの途中でないところに、フレームでないデータがある: 壊れている。
    * ID3v2 タグのあとに、フレームが無い（ファイルの終わりまで 0 や関係のないデータばかり）:
      タグまでしか書けていない。タグの直後でなくても、:data:`_MP3_FRAME_SEARCH_BYTES` バイトの
      範囲に続けて 2 つのフレームのヘッダーがあれば、フレームはある（タグの大きさに数えられて
      いない余分なデータが挟まる MP3 もあるので、たどらず、何も言わない）。
    * ファイルの終わりにある ID3v1・APEv2 タグと、小さな 0 埋めは正常。

    先頭が ID3 タグでなく、フレームにも見えないとき（フリーフォーマットなど）や、タグの大きさを
    読めないときは、MP3 の形が分からない（フレームをたどれない）ので、先頭の確かめ
    （:func:`_mp3_problem`）の結果のまま、何も言わない。
    """
    pos = 0
    for _ in range(_ID3V2_MAX_TAGS):
        handle.seek(pos)
        length = _id3v2_length(handle.read(10))
        if length is None:
            break
        if pos + length > size:  # 2 つ目以降のタグの長さは、そのタグの先頭から数えたもの
            return "途中で切れているか壊れています（先頭の ID3 タグは {0} バイトあるはずですが、ファイルは {1} バイトです）".format(
                pos + length, size)
        pos += length
    handle.seek(pos)
    first = handle.read(4)
    starts_with_frame = len(first) == 4 and first[0] == 0xFF and _mpeg_frame_length(first[1], first[2]) > 0
    if not starts_with_frame and pos:
        if _frames_follow(handle, pos):
            return None
        return "途中で切れているか壊れています（ID3 タグのあとに MP3 のフレームがありません）"
    if len(first) == 4 and not starts_with_frame:
        return None  # タグが無く、先頭がフレームに見えない。形が分からない（4 バイトに満たないときは、そこで切れているので、たどって確かめる）
    chunk, base = b"", 0
    lengths: Dict[int, int] = {}
    while pos < size:
        offset = pos - base
        if offset < 0 or offset + 4 > len(chunk):
            handle.seek(pos)
            chunk, base, offset = handle.read(_MP3_CHUNK), pos, 0
        if len(chunk) - offset < 4 or chunk[offset] != 0xFF:
            break
        key = (chunk[offset + 1] << 8) | chunk[offset + 2]
        length = lengths.get(key)
        if length is None:
            length = lengths[key] = _mpeg_frame_length(chunk[offset + 1], chunk[offset + 2])
        if not length:
            break
        if pos + length > size:
            return "途中で切れているか壊れています（{0} バイト目のフレームが、ファイルの終わりを越えています）".format(pos)
        pos += length
    if pos >= size or _mp3_trailer_is_ok(handle, pos, size):
        return None
    return "途中で切れているか壊れています（{0} バイト目に、MP3 のフレームでないデータがあります）".format(pos)


def _mp3_trailer_is_ok(handle: Any, pos: int, size: int) -> bool:
    """フレームの後（``pos`` からファイルの終わりまで）が、MP3 に付いていてよいものだけか。

    よいのは、小さな 0 埋め（:data:`_MP3_MAX_PADDING` バイトまで）または APEv2 タグ、
    そのあとに最後の 128 バイトの ID3v1 タグ、のいずれか（どちらも無くてもよい）。
    """
    end = size
    if size - pos >= _ID3V1_BYTES:
        handle.seek(size - _ID3V1_BYTES)
        if handle.read(3) == b"TAG":
            end -= _ID3V1_BYTES
    if end == pos:
        return True
    if end - pos <= _MP3_MAX_PADDING:
        handle.seek(pos)
        rest = handle.read(end - pos)
        if len(rest) == end - pos and not any(rest):
            return True
    return _ape_tag_spans(handle, pos, end)


def _ape_tag_spans(handle: Any, start: int, end: int) -> bool:
    """``start`` から ``end`` までが、APEv2 タグか。

    ヘッダー（``APETAGEX`` で始まる 32 バイト）で始まっていれば、中身は問わない（タグの先は
    メタデータだけで、音のフレームはここまでで終わっている）。ヘッダーの無いタグ（APEv1 など）は、
    最後の 32 バイトのフッターが示す大きさが、ちょうど ``start`` に届く場合だけ認める。
    """
    if end - start < _APE_BYTES:
        return False
    handle.seek(start)
    if handle.read(len(_APE_MAGIC)) == _APE_MAGIC:
        return True
    handle.seek(end - _APE_BYTES)
    footer = handle.read(_APE_BYTES)
    if len(footer) < _APE_BYTES or footer[:len(_APE_MAGIC)] != _APE_MAGIC:
        return False
    return end - int.from_bytes(footer[12:16], "little") == start  # 大きさはフッターを含み、ヘッダーを含まない


# ---------------------------------------------------------------------------
# 4. 書き込み
# ---------------------------------------------------------------------------
def _can_write(path: str) -> bool:
    """このプロセスの利用者が書けるか（ディレクトリは中にファイルを作れるか）。書き込みはしない。"""
    mode = os.W_OK | os.X_OK if os.path.isdir(path) else os.W_OK
    return os.access(path, mode)


def _uid_of(path: str) -> Optional[int]:
    """所有者の uid（リンクはたどらない）。調べられなければ ``None``。"""
    try:
        return os.lstat(path).st_uid
    except OSError:
        return None


def _effective_uid() -> Optional[int]:
    """実行している利用者の uid。持たない OS（Windows）では ``None``。"""
    geteuid = getattr(os, "geteuid", None)
    return geteuid() if geteuid else None


def _account_name(uid: Optional[int]) -> str:
    """``chown`` に渡す ``利用者:グループ``。調べられなければ空文字列。"""
    if uid is None:
        return ""
    try:
        import grp
        import pwd
        user = pwd.getpwuid(uid)
        return "{0}:{1}".format(user.pw_name, grp.getgrgid(user.pw_gid).gr_name)
    except (ImportError, KeyError):
        return "{0}:{0}".format(uid)


def _identity(euid: Optional[int], account: Optional[str]) -> Tuple[Optional[int], str]:
    """``(実行している利用者の uid, chown に渡す 利用者:グループ)``。省略されたものは実際の値で補う。"""
    if euid is None:
        euid = _effective_uid()
    if account is None:
        account = _account_name(euid)
    return euid, account


def write_roles(config: Config) -> Dict[str, Tuple[str, ...]]:
    """放送が書き込むフォルダ → その目的（:data:`STATE`・:data:`HISTORY`・:data:`CACHE`）。

    同じフォルダは 1 つにまとめる（既定では ``cache/`` が状態と履歴を兼ねる）。
    """
    candidates = ((STATE, os.path.dirname(config.path("state.file"))),
                  (HISTORY, os.path.dirname(config.path("state.history_file"))),
                  (CACHE, config.path("tts.cache_dir")))
    roles: Dict[str, List[str]] = {}
    for role, directory in candidates:
        if directory:
            roles.setdefault(directory, []).append(role)
    return {directory: tuple(found) for directory, found in roles.items()}


def write_targets(config: Config) -> List[str]:
    """放送が書き込む場所（状態・履歴のフォルダと、音声のキャッシュ）。重なりは除く。"""
    return list(write_roles(config))


def check_writable(config: Config, euid: Optional[int] = None,
                   account: Optional[str] = None) -> List[Result]:
    """書き込み先に書けるか。書けないとき、その結果（NG か警告か）は目的で決まる。

    ``euid`` は実行している利用者（省略時は実際の値）、``account`` は ``chown`` に
    渡す ``利用者:グループ``（省略時は ``euid`` から調べる）。root で実行している
    ときは、何でも書けてしまい権限を調べても意味がないので、その旨を情報で出す。
    """
    euid, account = _identity(euid, account)
    results = [_directory_result(config, directory, roles, account, euid)
               for directory, roles in write_roles(config).items()]
    results.extend(_file_results(config, account, euid))
    if euid == 0:
        results.append(Result(
            INFO, "所有者",
            "root で実行しているため確認しません（書き込めるか・所有者は、サービスの利用者で実行すると確認できます）"))
    return results


def _nearest_existing(path: str) -> str:
    """``path`` かその親のうち、実在する一番近いもの。"""
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _creation_problem(directory: str) -> Tuple[str, Optional[str]]:
    """``directory`` を（まだ無ければ作って）使えるか。``(最寄りの実在するもの, 問題)``。

    問題は、無ければ ``None``、実在する祖先がフォルダでなければ ``"file"``、書けなければ
    ``"unwritable"``。
    """
    probe = _nearest_existing(directory)
    if not os.path.isdir(probe):
        return probe, "file"
    if not _can_write(probe):
        return probe, "unwritable"
    return probe, None


def _directory_fix(directory: str, probe: str, keys: Sequence[str], account: str,
                   euid: Optional[int]) -> str:
    """``directory`` に書けるようにする直し方（``probe`` は ``directory`` かその最寄りの実在する祖先）。

    ``keys`` は、このフォルダを指している設定のキー。

    * まだ無い: **その 1 つだけ**を作って渡す（``mkdir -p`` のあとに再帰なしの ``chown``）。
      実在する祖先（``/`` や ``/var/lib`` など、設定の持ち物でないもの）には触れない。
    * あるのに書けない、OS の場所（:data:`_SYSTEM_DIRECTORIES`）: 持ち主を変えると OS が壊れる。
      設定を書ける場所に変える案内にする。
    * あるのに書けない、自分の所有: 権限（モード）の問題。``chmod`` で直す。
    * あるのに書けない、他人（root など）の所有: そのフォルダと中身を ``chown -R`` する。
    """
    quoted = shlex.quote(directory)
    if probe != directory:
        if not account:
            return "sudo mkdir -p {0} のうえ、サービスを動かす利用者が書き込めるようにしてください".format(quoted)
        return "sudo mkdir -p {0} && sudo chown {1} {0}".format(quoted, account)
    if os.path.normpath(directory) in _SYSTEM_DIRECTORIES:
        return "OS の場所 {0} の持ち主は変えられません。設定の {1} を、サービスの利用者が書ける場所に変えてください".format(
            quoted, "・".join(keys))
    if euid is not None and _uid_of(directory) == euid:
        return "chmod u+rwx {0}".format(quoted)
    if not account:
        return _GENERIC_FIX
    return "sudo chown -R {0} {1}".format(account, quoted)


def _file_fix(path: str, account: str, euid: Optional[int]) -> str:
    """既にあるファイルに書けるようにする直し方（1 つのファイルだけ。再帰しない）。"""
    quoted = shlex.quote(path)
    if euid is not None and _uid_of(path) == euid:
        return "chmod u+w {0}".format(quoted)
    if not account:
        return _GENERIC_FIX
    return "sudo chown {0} {1}".format(account, quoted)


def _directory_result(config: Config, directory: str, roles: Sequence[str], account: str,
                      euid: Optional[int]) -> Result:
    """書き込み先のフォルダ。まだ無ければ、作れる（実在する親に書ける）かを見る。

    書けなかったときの結果（NG か警告か）と、それで困ること（詳細に添える）は、そこへ
    何を書くか（``roles``）で決まる。複数あるときは、いちばん重いものに合わせる。
    """
    title = shown_path(config, directory).rstrip("/") + "/"
    level, consequence = _CONSEQUENCES[min(roles, key=_ROLE_ORDER.index)]
    probe, problem = _creation_problem(directory)
    if problem == "file":
        return Result(level, title, "{0} がフォルダではありません（{1}）".format(
            shown_path(config, probe), consequence), "そのファイルを移すか消してください")
    if problem == "unwritable":
        reason = ("書き込めません" if probe == directory else
                  "まだ無く、{0} に書けないので作れません".format(shown_path(config, probe)))
        return Result(level, title, "{0}（{1}）".format(reason, consequence),
                      _directory_fix(directory, probe, [_ROLE_KEYS[role] for role in roles], account, euid))
    if probe != directory:
        when = "初回の放送で作ります" if set(roles) & {STATE, HISTORY} else "合成した音声を保存するときに作ります"
        return Result(OK, title, "まだありません（{0}）".format(when))
    return Result(OK, title, "書き込めます")


def _sticky_problem(config: Config, state: str, account: str, euid: Optional[int]) -> Optional[Result]:
    """sticky ビットのフォルダにある、他人の ``state.json`` を置き換えられない組み合わせ。

    sticky ビット（``/tmp`` など）のフォルダでは、ファイルを消す・置き換えられるのは、そのファイルか
    フォルダの持ち主だけ。``state.json`` は一時ファイルから置き換えて保存するので、フォルダにも
    既存の ``state.json`` にも自分の持ち物でないと、保存が断られる（まだ ``state.json`` が無ければ
    新しく作れる）。root（と uid を持たない OS）には当てはまらない。フォルダに書けないときは
    :func:`_directory_result` が言うので、ここでは重ねない。
    """
    if not euid:
        return None
    directory = os.path.dirname(state)
    try:
        sticky = bool(os.stat(directory).st_mode & stat.S_ISVTX)
    except OSError:
        return None
    file_owner = _uid_of(state)
    if not sticky or file_owner is None or not _can_write(directory):
        return None
    if euid in (file_owner, _uid_of(os.path.realpath(directory))):
        return None
    return Result(
        NG, shown_path(config, state),
        "{0}/ は sticky ビット付きのフォルダで、フォルダも既存のファイルも別の利用者の持ち物なので、"
        "置き換えて保存できません（{1}）".format(shown_path(config, directory).rstrip("/"), _CONSEQUENCES[STATE][1]),
        "{0}（または設定の state.file を、サービスの利用者のフォルダの中に変える）".format(
            _file_fix(state, account, euid)))


def _file_results(config: Config, account: str, euid: Optional[int]) -> List[Result]:
    """既にある状態・履歴のファイルのうち、使えないものだけを挙げる（問題が無ければ何も出さない）。

    * 状態ファイルがフォルダ: 再生済みの記録を保存できないので NG。
    * 状態ファイルが sticky ビットのフォルダにあり、他人の持ち物: 置き換えられないので NG
      （:func:`_sticky_problem`）。
    * 履歴ファイルがフォルダ、または追記できない: 履歴が残らないだけなので警告。

    状態ファイルが root 所有でも、置き場所のフォルダに書ければ、アプリは一時ファイルから
    置き換えて保存できるので、挙げない（フォルダに書けない場合は :func:`_directory_result`
    が NG にする。sticky ビットのフォルダは上のとおり別）。履歴は追記なので、ファイルそのものに書けることが要る。
    """
    results = []
    state = config.path("state.file")
    if state and os.path.isdir(state):
        results.append(Result(
            NG, shown_path(config, state),
            "ファイルではなくフォルダです（{0}）".format(_CONSEQUENCES[STATE][1]),
            "そのフォルダを移すか消してください（中身を確かめてから）"))
    elif state:
        sticky = _sticky_problem(config, state, account, euid)
        if sticky:
            results.append(sticky)
    history = config.path("state.history_file")
    if history and os.path.isdir(history):
        results.append(Result(
            WARNING, shown_path(config, history),
            "ファイルではなくフォルダです（放送の履歴を残せません。放送は止まりません）",
            "そのフォルダを移すか消してください（中身を確かめてから）"))
    elif history and os.path.isfile(history) and not _can_write(history):
        results.append(Result(
            WARNING, shown_path(config, history),
            "追記できません（放送の履歴を残せません。放送は止まりません）",
            _file_fix(history, account, euid)))
    return results


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------
def collect(config: Config, euid: Optional[int] = None,
            account: Optional[str] = None) -> List[Tuple[str, List[Result]]]:
    """節の見出しと結果の一覧を、表示する順に返す。

    設定の節は書かれたとおりの設定（``config``）を調べ、ほかの節は実際に動く設定
    （error の出たキーを既定値に戻したもの。サービスはこちらで動く）を調べる。
    """
    effective, _ = configcheck.sanitized(config)
    return [
        ("設定", check_settings(config)),
        ("作り置きの音声", check_voices(effective)),
        ("音源", check_sounds(effective, euid, account)),
        ("書き込み", check_writable(effective, euid, account)),
    ]


def run_check(config: Config, out: Optional[TextIO] = None, euid: Optional[int] = None,
              account: Optional[str] = None) -> int:
    """設置状態を点検して結果を表示し、終了コードを返す（0: 問題なし、1: NG あり）。

    何も書かない。``out`` は出力先（省略時は標準出力）。``euid`` と ``account`` は
    :func:`check_writable` と同じ。
    """
    stream = sys.stdout if out is None else out
    sections = collect(config, euid, account)
    for heading, results in sections:
        print("\n".join(render_section(heading, results)), file=stream)
        print(file=stream)
    everything = [result for _, results in sections for result in results]
    print(summarize(everything), file=stream)
    return 1 if any(result.level == NG for result in everything) else 0
