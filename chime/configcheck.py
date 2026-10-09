"""設定の検査。

設定ファイルのタイプミスが、再起動の繰り返し（起動時の例外）・CPU の空回り
（待機時間の秒数を 0 にした）・9 時間のずれ（タイムゾーンの綴りを間違えると
OS のローカル時刻になり、Pi では UTC のことが多い）といった形で表に出る前に、
起動時と ``--check`` で気づけるようにする。

検査は 2 段に分けている。

:func:`walk_overrides`
    設定ファイル（既定値への「差分」）を、書かれたとおりに見る。知らないキー・
    廃止したキー・既定値と同じ値。値そのものの正しさは見ない。
:func:`validate`
    マージ後の設定を見る。ランタイム（``Scheduler`` や ``ChimeApp`` など）が実際に
    その値をどう読むかに合わせて、重大度を決める。

重大度の決め方（方針）
    :func:`sanitized` は、前の版が例外なく動かしていた設定の動作を、変えてはならない。
    そのため **error は「その値でランタイムが実際に壊れる」ものだけ** に付ける。
    起動時や予定を作るとき・再生のときに例外になる、待機ループが CPU を使い続ける、
    OS のローカル時刻で動く（タイムゾーンが解決できない）、ログがすべて失われる
    （書式やログの水準の名前が使えない）。error のキーだけを既定値に戻して続ける
    （異常終了させても systemd の ``Restart=always`` が再起動を繰り返すだけで、時報は
    鳴らないままになるため）。

    ランタイムが許している値は **warning** にする。何がどう動くか（鳴らない時刻・
    読み上げの欠け・既定の文言への切り替えなど）と直し方を示すだけで、値は置き換えない。
    ``"9"`` や ``9.0`` のような「数値の書き方の違い」、0〜23 の範囲外の時刻（鳴らさない
    だけ）、``start_hour`` が ``end_hour`` より後ろ（時報は鳴らない）、曜日の書き間違い、
    ``null`` にした節、緯度経度の範囲外（天気を取れない）、テンプレートの置換名の間違い
    （ランタイムが既定の文言に切り替える）、テンプレートの書式指定の巨大な幅（巨大な文になり、
    作り置きに無いので無音。試しの ``format`` には通さない）、``tts.engines`` に
    ``prerecorded`` が無い（Pi では読み上げがすべて無音）などが当たる。

    リストに壊れた要素が混ざるときは、warning の要素のためにリストを作り直さない。
    error の要素（``int()`` で読めない ``skip_hours`` の要素、ハッシュできない
    ``weekdays`` の要素、コマンドとして読めない ``audio.commands`` の項目）がある場合も、
    **直すのはその要素だけ** で、残りは使う（ランタイムが動くのに必要な分だけ直す）。
    ``audio.commands`` の読めない項目は、その拡張子の既定の項目に置き換える（既定に無い
    拡張子の項目は取り除く）。

    ランタイムが読まない値は、壊れていても warning にする（止めた時報・閉館放送の中身、
    ``tts.engines`` に無い VOICEVOX の設定、再生方式が ``mock`` / ``pygame`` のときの
    ``audio.commands``）。有効にしたとき（``command`` にしたとき）に error になる。

    例外を受け止める所で読む値は、範囲を外れても warning にする（``weather.timeout_seconds`` が
    ソケットの受けられない大きさ・小ささのとき、天気予報の取得は例外になるが、放送を組み立てる
    側が受け止めて、天気予報を飛ばすだけで続ける）。

    error と warning の境目は、ランタイムの読み方そのものに合わせる（``tests/test_configcheck.py``
    が、実際の ``ChimeApp``・``Scheduler``・再生・ログに通して確かめる）。日時の計算が範囲
    （1〜9999 年）を出るかは、今の日付から予定を探す範囲で同じ式を計算して決める。

    個々の検査は互いに独立していて、どれかが例外を出しても起動は止まらない
    （その項目は「検査できませんでした」と warning にするだけ）。

    検査しないもの: 時報音の合成が巨大な値（長さ・短音の数・周波数）で終わらなくなること
    （合成は時報音が無いときだけ行われ、前の版と同じ）。

このモジュールは ``chime.audio`` / ``chime.tts`` を import しない（pygame を
引き込まないため）。再生バックエンドと読み上げエンジンの名前は、ここに写して
持つ（``tests/test_configcheck.py`` が実装との食い違いを検出する）。
"""

from __future__ import annotations

import copy
import difflib
import functools
import json
import logging
import math
import re
import string
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, Mapping, NamedTuple, Optional, Tuple
from zoneinfo import ZoneInfo

from . import timesignal
from .config import DEFAULT_CONFIG, REMOVED_KEYS, Config
from .jsonfile import JsonFileError, read_json
from .scheduler import MAX_LOOKAHEAD_DAYS

ERROR = "error"
WARNING = "warning"
INFO = "info"

#: 重大な順（:func:`check_config` の並び順）。
_LEVEL_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}

#: ``Config.sources`` の先頭に入る、設定ファイルではない出どころ。
_DEFAULTS_SOURCE = "<defaults>"

#: 中身を自由に書ける節。キーの綴りも値も見ない（時刻の読み換え表・外部プレイヤーの
#: コマンド）。
_FREE_FORM = frozenset({"time_signal.hour_readings", "audio.commands"})

#: ``audio.backend`` に書ける値（``chime.audio.create_player`` が受ける名前）。
_AUDIO_BACKENDS = ("auto", "pygame", "command", "mock")

#: ``tts.engines`` に書ける名前（``chime.tts.TTSService`` が組み立てる名前）。
_TTS_ENGINES = ("prerecorded", "voicevox")

#: ``logging.level`` に書ける名前。``chime.logsetup.setup_logging`` は
#: ``getattr(logging, 名前.upper(), logging.INFO)`` で水準を引くので、大文字小文字は問わない。
#: ここに無い名前は、黙って INFO になるか（綴り違い）、数値でない属性を引いて起動時に
#: 例外になる（``BASIC_FORMAT`` など）。
_LOG_LEVELS = ("CRITICAL", "FATAL", "ERROR", "WARN", "WARNING", "INFO", "DEBUG", "NOTSET")

#: 時報の時刻を調べる範囲（``end_hour - start_hour + 1``）の上限。0〜23 時の 24 通りに
#: 収まらない分は鳴らさず飛ばすだけだが、``Scheduler`` は予定を作るたびにその全部を
#: 数える（1000 万件で 1 日あたり約 0.65 秒。予定の探索で 30 日分を数えるので、1 回の
#: 探索に 20 秒かかる）。これを超える範囲は CPU を使い続けて予定が進まないので error にする。
#: 範囲外の時刻を書いただけ（``end_hour: 1600`` など）の現実的な誤りは、これよりずっと小さい。
_MAX_HOUR_SPAN = 10_000_000

#: セグメントの間の無音（``audio.gap_ms``）の上限（ミリ秒）。``time.sleep`` は約 9.2e9 秒
#: （``_PyTime_t`` の上限）を超えると ``OverflowError`` になり、放送が途中で止まる。
_MAX_GAP_MS = 9_223_372_036 * 1000

#: ソケットの待ち時間（``tts.voicevox.probe_timeout_seconds`` と ``timeout_seconds``、
#: ``weather.timeout_seconds``）の絶対値の上限（秒）。``socket.settimeout`` は約 9.2e9 秒
#: （``_PyTime_t`` の上限）を超える値（負の値も）を ``OverflowError`` にする。``urlopen`` は
#: それを通信の失敗として受け止めないので、VOICEVOX ENGINE の疎通確認と合成が毎回例外になる
#: （天気予報の取得も毎回例外になるが、こちらは放送を組み立てる側が受け止める）。9e9 秒
#: （285 年）までは通る。
_MAX_TIMEOUT_SECONDS = 9_223_372_036

#: 気温の読み上げ文（作り置き）の数がこれを超えたら warning にする（既定は 46 件）。
_MAX_TEMP_VALUES = 200

#: テンプレートの書式指定（``{label:>200}`` の 200 や ``{x:.5}`` の 5）に書ける数の上限。
#: これを超える数は、1 つの文を何億文字にもする。試しの ``format`` に通さない（試すだけで
#: 時間とメモリを使う）。``chime.phrases.MAX_FORMAT_SPEC`` と同じ値（このモジュールは
#: ``chime.phrases`` を import しないので写して持つ。``tests/test_configcheck.py`` が一致を確かめる）。
_MAX_FORMAT_SPEC = 1000

#: 曜日の名前（``date.weekday()`` の 0〜6 の順）。
_WEEKDAY_NAMES = ("月", "火", "水", "木", "金", "土", "日")

_MISSING = object()

#: 値を文に埋め込むときの最大文字数（巨大な数や長い文字列でメッセージが埋まらないように）。
_SHOW_LIMIT = 60


@dataclass(frozen=True)
class Finding:
    """検査の結果 1 件。

    ``level`` は ``"error"``（ランタイムが壊れる値。起動時は既定値に置き換える）・
    ``"warning"``（ランタイムは動くが、書いた意図と違う動きになる、または直したほうが
    よい。置き換えない）・``"info"``（動作は変わらない）。
    ``key`` はドット区切りの設定キー（ファイル全体の問題は空文字列）。リストの中の
    要素の問題も、そのリストのキーで報告する（どの要素かは ``message`` に書く）。
    ``source`` は値の出どころの設定ファイル（分からなければ空）。
    """

    level: str
    key: str
    message: str
    hint: str = ""
    source: str = ""

    def describe(self) -> str:
        """1 行の説明（キー・内容・直し方・出どころ）。レベルの表示は呼び出し側で足す。"""
        text = "{0}: {1}".format(self.key, self.message) if self.key else self.message
        if self.hint:
            text += "（{0}）".format(self.hint)
        if self.source:
            text += " [{0}]".format(self.source)
        return text


# ---------------------------------------------------------------------------
# 共通の小道具
# ---------------------------------------------------------------------------
def _is_int(value: Any) -> bool:
    """整数か（``True`` / ``False`` は整数に数えない）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    """有限の数値か（``bool``・``NaN``・無限大は数値に数えない）。

    巨大な整数（``10**400``）は ``float`` に直せないので、``math.isfinite`` に渡さない
    （``OverflowError`` になる）。整数は常に有限。
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _show(value: Any, limit: Optional[int] = _SHOW_LIMIT) -> str:
    """値を、設定ファイルに書く形（JSON）で見せる。長いときは ``limit`` 文字で切る。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except Exception:  # 循環参照・桁数が多すぎる整数など。見せられなくても検査は続ける
        text = "<{0}>".format(type(value).__name__)
    if limit is not None and len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


def _convert(value: Any, integer: bool) -> Optional[Any]:
    """ランタイムが読むのと同じ ``int()`` / ``float()`` で読んだ値（読めなければ ``None``）。

    ``"9"``・``9.0``・``True`` などは読める。``None``・リスト・``"abc"``・``NaN`` の
    ``int()``・無限大の ``int()``・巨大な整数の ``float()`` は読めない。
    """
    try:
        return int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _is_plain(value: Any, integer: bool) -> bool:
    """数値の書き方として素直か（整数のキーは整数、そのほかは有限の数値）。"""
    return _is_int(value) if integer else _is_number(value)


def _hashable(item: Any) -> bool:
    try:
        hash(item)
    except TypeError:
        return False
    return True


def _default_paths(default: Mapping[str, Any] = DEFAULT_CONFIG,
                   prefix: str = "") -> Iterator[Tuple[str, Any]]:
    """既定設定の全キーを ``(ドット区切りのパス, 既定値)`` で列挙する。

    自由形式の節（:data:`_FREE_FORM`）の中身は含めない。
    """
    for key, value in default.items():
        path = prefix + key
        yield path, value
        if isinstance(value, Mapping) and path not in _FREE_FORM:
            yield from _default_paths(value, path + ".")


def _default_at(key: str) -> Any:
    """``DEFAULT_CONFIG`` の ``key`` の値（無ければ ``KeyError``）。"""
    node: Any = DEFAULT_CONFIG
    for part in key.split("."):
        node = node[part]
    return node


def _value(config: Config, key: str) -> Any:
    """設定値。キーが無い（親が辞書でない場合も）なら :data:`_MISSING`。"""
    return config.get(key, _MISSING)


def _error(key: str, message: str, how_to_fix: str) -> Finding:
    """error を作る。直し方に、直るまで使う既定値を添える。"""
    hint = "{0}。直るまでは既定値 {1} で動かします".format(
        how_to_fix, _show(_default_at(key), None))
    return Finding(ERROR, key, message, hint)


def _warning(key: str, message: str, how_to_fix: str) -> Finding:
    """warning を作る。置き換えないので、直し方だけを添える。"""
    return Finding(WARNING, key, message, how_to_fix)


def _leveled(level: str, key: str, message: str, how_to_fix: str) -> Finding:
    return (_error if level == ERROR else _warning)(key, message, how_to_fix)


def _is_active(config: Config, section: str) -> bool:
    """節（時報・閉館放送）が有効か。``Scheduler`` と同じ見方（``enabled`` の真偽。無ければ有効）。"""
    value = _value(config, section + ".enabled")
    return True if value is _MISSING else bool(value)


#: 無効にした節の中身は ``Scheduler`` が読まない。値が壊れていても、有効にするまでは動く。
_UNUSED_NOTE = "（今は enabled が false なので使われませんが、有効にしたときに例外になります）"


# ---------------------------------------------------------------------------
# 設定ファイル（差分）を見る
# ---------------------------------------------------------------------------
def walk_overrides(override: Mapping[str, Any], source: str = "") -> List[Finding]:
    """設定ファイルの中身を既定値と突き合わせ、書き方の問題を返す。

    - 知らないキー → warning（近い綴りがあれば「もしかして …?」）
    - 廃止したキー（``config.REMOVED_KEYS``）→ warning
    - 既定値と同じ値 → info（書かなくても同じ動作。将来の既定値の変更が届かなくなる）

    ``_`` で始まるキー（``_comment`` など）・``time_signal.hour_readings`` と
    ``audio.commands`` の中身・リストの要素は見ない。値の正しさは :func:`validate` の役目。
    """
    findings: List[Finding] = []
    if isinstance(override, Mapping):
        _walk(override, DEFAULT_CONFIG, "", source, findings)
    return findings


def _walk(override: Mapping[str, Any], default: Mapping[str, Any], prefix: str,
          source: str, findings: List[Finding]) -> None:
    for raw_key, value in override.items():
        key = str(raw_key)
        if key.startswith("_"):
            continue
        path = prefix + key
        if path in REMOVED_KEYS:
            findings.append(_removed(path, source))
        elif key not in default:
            findings.append(_unknown(path, key, default, prefix, source))
        else:
            _walk_known(path, value, default[key], source, findings)


def _walk_known(path: str, value: Any, base: Any, source: str,
                findings: List[Finding]) -> None:
    """既定設定にあるキーを見る。節なら中へ入り、値なら既定値と同じかを見る。"""
    if isinstance(value, Mapping) and isinstance(base, Mapping) and path not in _FREE_FORM:
        _walk(value, base, path + ".", source, findings)
    elif _same(value, base):
        findings.append(Finding(
            INFO, path, "{0} は既定値（{1}）と同じ値です".format(path, _show(base, None)),
            "書かなくても同じ動作です。残すと、将来その既定値が変わっても古い値のままになります",
            source))


def _same(value: Any, base: Any) -> bool:
    """既定値と同じ値か。``1`` と ``True`` は同じに数えない。"""
    return isinstance(value, bool) == isinstance(base, bool) and value == base


def _removed(path: str, source: str) -> Finding:
    return Finding(
        WARNING, path,
        "{0} は v6.0.0 で廃止しました（{1}）。この行は無視されます".format(path, REMOVED_KEYS[path]),
        "この行を消してください", source)


def _unknown(path: str, key: str, default: Mapping[str, Any], prefix: str,
             source: str) -> Finding:
    return Finding(
        WARNING, path, "{0} という設定キーはありません。この行は無視されます".format(path),
        _suggestion(key, default, prefix), source)


def _suggestion(key: str, default: Mapping[str, Any], prefix: str) -> str:
    """知らないキーに近い、使えるキーの案内。

    同じ階層の綴りの近いキーを先に探し、無ければ、別の階層に同じ名前のキー
    （書く場所の間違い）を探す。
    """
    near = [prefix + name for name in difflib.get_close_matches(key, list(default), n=1)]
    if not near:
        near = [path for path, _ in _default_paths() if path.rsplit(".", 1)[-1] == key][:3]
    if near:
        return "もしかして {0}?".format(" か ".join(near))
    return "使えるキーは config.example.json を参照してください"


# ---------------------------------------------------------------------------
# マージ後の設定を見る
# ---------------------------------------------------------------------------
def validate(config: Config) -> List[Finding]:
    """マージ後の設定の値を検査し、見つけた問題（error と warning）を返す。

    既定の設定では何も返さない。キーが無い項目は見ない（既定値が使われるため）。
    検査は 1 項目ずつ独立して動かす。どれかが例外を出しても、ほかの項目の検査と
    起動は止めず、その項目を「検査できませんでした」という warning にする。
    """
    findings: List[Finding] = []
    for key, check in _checks():
        try:
            findings.extend(check(config))
        except Exception as exc:  # 検査の不具合で、起動まで止めない
            findings.append(_unchecked(key, exc))
    return findings


def _unchecked(key: str, exc: Exception) -> Finding:
    where = "{0} の".format(key) if key else ""
    return Finding(
        WARNING, key,
        "{0}検査中に内部エラーが起きたため、この項目は検査できませんでした（{1}）".format(
            where, type(exc).__name__),
        "値の書き方を確かめてください（config.example.json が見本です）。動作は変えません")


# -- 節の形 -----------------------------------------------------------------
class _Section(NamedTuple):
    """節（``{ … }`` でまとめる項目）が ``{ … }`` 以外の値になったときの、ランタイムの動き。"""

    #: 例外にならないとき（warning）の、実際の動き。
    effect: str
    #: 例外になるときの、実際の動き（``breaks`` が真のとき）。
    crash: str = ""
    #: その値でランタイムが例外になるか。``None`` なら、どんな値でも例外にならない。
    breaks: Optional[Callable[[Any], bool]] = None
    #: 直し方。
    how: str = "{ … } の形で書き直してください（config.example.json が見本です）"
    #: この節をランタイムが読むか（設定から分かるとき）。読まないなら、``breaks`` でも warning。
    when: Optional[Callable[[Config], bool]] = None
    #: ``breaks`` だが ``when`` が偽（読まれない）ときの、実際の動き。
    unused: str = ""


#: ``tts.engines`` に ``voicevox`` が無い間、VOICEVOX の設定は読まれない。
_VOICEVOX_UNUSED = "今は tts.engines に voicevox が無いので使われませんが、入れたときに例外になります"


#: ``audio.backend`` が ``mock`` / ``pygame`` の間、外部コマンドの表は読まれない。
_COMMANDS_UNUSED = ("今は audio.backend が mock か pygame なので使われませんが、command にしたとき"
                    "（pygame が無いときの auto を含む）に放送が鳴らなくなります")


def _command_backend_possible(config: Config) -> bool:
    """外部コマンドで再生する方式（``CommandPlayer``）が選ばれうるか（表を読むか）。

    ``create_player`` は、``command`` のとき、そして ``auto``（空・``null``・知らない名前を含む）で
    pygame が無いときに、外部コマンドの再生を作る。表を読まないのは、``mock`` と ``pygame``
    （大文字小文字は問わない）を名指ししたときだけ。文字列でない値は別の error が既定値
    （``auto``）に戻すので、読まれるものとして数える。
    """
    backend = _value(config, "audio.backend")
    if isinstance(backend, str):
        return backend.lower() not in ("mock", "pygame")
    return True


def _voicevox_listed(config: Config) -> bool:
    """``tts.engines`` に ``voicevox`` があるか（``TTSService`` は、あるときだけ ``tts.voicevox`` を読む）。

    ``tts`` が節でない（空の節として読まれ、エンジンが 1 つも無い）ときは偽。反復できない値は
    別の error が既定値に戻すので、真として数える。
    """
    engines = _value(config, "tts.engines")
    if engines is _MISSING:
        return False
    try:
        return any(str(name) == "voicevox" for name in engines)
    except TypeError:
        return True


def _is_truthy(value: Any) -> bool:
    """``Scheduler`` は ``節 or {}`` で読む。偽の値は空の節になり、真の値は ``.get`` で例外になる。"""
    return bool(value)


def _not_dict_like(value: Any) -> bool:
    """``dict(値)`` が例外になるか（ランタイムは節をそのまま ``dict()`` に渡すことがある）。"""
    try:
        dict(value)
    except Exception:
        return True
    return False


_DISABLE_WITH_ENABLED = "止めたいときは null にせず、中の enabled を false にします"

#: 節の値が辞書でなくなったとき（``null``・数値・文字列・リストに置き換えたとき）の扱い。
#: ランタイムの読み方（``Config.section`` は辞書でなければ空の辞書を返す。``Scheduler`` や
#: ``PygamePlayer`` などは節の中の節を直接読む）に合わせて、error か warning かを決める。
#: 既定設定の節は、ここにすべて載せる（``tests/test_configcheck.py`` が確かめる）。
_SECTIONS: Dict[str, _Section] = {
    "logging": _Section("ログの水準と書式は組み込みの値になります"),
    "schedule": _Section("時報も閉館放送も鳴りません", how=_DISABLE_WITH_ENABLED),
    "schedule.hourly": _Section(
        "時報（毎正時）は鳴りません",
        "時報の予定を作る処理が例外になり、起動できません", _is_truthy, _DISABLE_WITH_ENABLED),
    "schedule.closing": _Section(
        "閉館放送は鳴りません",
        "閉館放送の予定を作る処理が例外になり、起動できません", _is_truthy, _DISABLE_WITH_ENABLED),
    "audio": _Section("再生の設定は組み込みの値になります（セグメントの間の無音は 0 秒、外部コマンドは"
                      "未設定になります）"),
    "audio.mixer": _Section(
        "mixer の設定は組み込みの値になります",
        "再生の準備（mixer の設定を読む処理）が例外になり、放送が鳴りません", _not_dict_like),
    "audio.commands": _Section(
        "外部コマンドは未設定になります（外部コマンドで再生する方式は使えません）",
        "外部コマンドで再生する方式（command）の準備が例外になり、放送が鳴りません", _not_dict_like,
        when=_command_backend_possible, unused=_COMMANDS_UNUSED),
    "time_signal": _Section("時報音の保存先が無くなり、時報音（ポ・ポ・ポ・ポーン）は鳴りません。"
                            "時刻アナウンスは組み込みの文言になります"),
    "time_signal.short_pip": _Section(
        "時報音を作れず、時報音が鳴りません（前に作った時報音があればそれを使います）"),
    "time_signal.long_pip": _Section(
        "時報音を作れず、時報音が鳴りません（前に作った時報音があればそれを使います）"),
    "time_signal.hour_readings": _Section(
        "読み換えの表として使えないので、組み込みの読み換え（0・4・7・9 時）を使います"),
    "extra_segment": _Section(
        "おまけの設定は組み込みの値になります（天気は流れず、ひとことだけ流れます）",
        how=_DISABLE_WITH_ENABLED),
    "quotes": _Section("ひとことのファイルが指定されず、内蔵の予備のひとことだけになります"),
    "weather": _Section("地点が読めず、天気予報は流れません", how=_DISABLE_WITH_ENABLED),
    "weather.open_meteo": _Section("地点が読めず、天気予報は流れません"),
    "weather.prerecord": _Section("作り置きの範囲が読めず、天気の文を作れないので天気予報は流れません"),
    "tts": _Section("読み上げのエンジンが無く、読み上げはすべて無音になります"),
    "tts.voicevox": _Section(
        "VOICEVOX の設定は空として扱われます",
        "VOICEVOX ENGINE の設定を読む処理が例外になり、起動できません", _not_dict_like,
        when=_voicevox_listed, unused=_VOICEVOX_UNUSED),
    "closing": _Section("閉館放送の音源が指定されず、閉館放送は無音になります"),
    "state": _Section("再生済みの記録を保存できず、再起動すると同じ放送を鳴らし直すことがあります"),
}


def _check_sections(config: Config) -> List[Finding]:
    """節が、数値や ``null`` などに置き換わっていないか。

    ``Scheduler`` が節の中の節を直接読む（時報・閉館放送）、再生や読み上げのエンジンが
    節を ``dict()`` に渡す（mixer・外部コマンド・VOICEVOX）ものは、置き換えると例外に
    なる（error）。それ以外は、ランタイムが空の節として扱うので動く（warning）。
    """
    findings: List[Finding] = []
    for path, base in _default_paths():
        value = _value(config, path)
        if not isinstance(base, Mapping) or value is _MISSING or isinstance(value, Mapping):
            continue
        rule = _SECTIONS.get(path) or _Section("この節の設定は組み込みの値になります")
        breaks = rule.breaks is not None and rule.breaks(value)
        read = rule.when is None or rule.when(config)
        effect = rule.crash if breaks and read else rule.unused if breaks else rule.effect
        message = "{0} は {{ … }} の形でまとめて書く項目ですが、{1} になっています。{2}".format(
            path, _show(value), effect)
        findings.append(_leveled(ERROR if breaks and read else WARNING, path, message, rule.how))
    return findings


def _check_commands(config: Config) -> List[Finding]:
    """外部コマンドの表の各項目が、コマンド（引数の文字列のリスト）として読めるか。

    節の中身は自由に書けるが、``CommandPlayer`` は項目ごとに ``list(値)`` で読み、再生のときに
    引数ごとに ``format`` する。したがって、反復できない値（数値・``null``）は外部コマンドで
    再生する方式を作る処理が例外になり、**文字列**（``"aplay -q {path}"``。リストと取り違えやすい）は
    1 文字ずつの引数に分かれて、そのコマンドの再生が毎回失敗し（``{`` だけの引数は
    ``format`` が例外、波括弧が無ければ ``a`` という名前のコマンドを探す）、リストの中に
    文字列でない要素があれば、再生のたびに ``format`` が例外になる。

    表を読むのは外部コマンドの再生だけ（:func:`_command_backend_possible`）。``audio.backend`` が
    ``mock`` / ``pygame`` の間は、壊れていてもランタイムは動くので、置き換えない warning にする。
    """
    key = "audio.commands"
    value = _value(config, key)
    if not isinstance(value, Mapping):
        return []  # 節が辞書でないことは、節の検査が見る
    bad = [name for name, command in value.items() if _command_breaks(command)]
    if not bad:
        return []
    texts = [name for name in bad if isinstance(value[name], str)]
    note = ""
    if texts:
        note = ("。文字列で書くと、1 文字ずつの引数に分かれて読まれます（{0} は {1}）".format(
            _show(texts[0]), _show(list(value[texts[0]][:12]), 30)))
    read = _command_backend_possible(config)
    effect = ("外部コマンドで再生する方式（command）が例外になり（または、そのコマンドの"
              "再生がすべて失敗し）、放送が鳴りません") if read else _COMMANDS_UNUSED
    message = (
        "{0} の {1} の値はコマンド（[\"aplay\", \"-q\", \"{{path}}\"] のような、文字列の引数のリスト）では"
        "ありません{2}。{3}".format(key, _show(bad), note, effect))
    how = 'コマンドは ["aplay", "-q", "{path}"] のように引数のリストで書いてください'
    if not read:
        return [_warning(key, message, how)]
    hint = (how + "。直るまでは、読めない項目だけを"
            "その拡張子の既定の項目に置き換え（既定に無い拡張子の項目は取り除き）、残りの項目は"
            "そのまま使って動かします（表そのものでないなど、直せないときは、既定値 {0} で動かします）".format(
                _show(_default_at(key), None)))
    return [Finding(ERROR, key, message, hint)]


def _command_breaks(command: Any) -> bool:
    """外部コマンドの項目が、再生を壊す書き方か。

    反復できない値、空でない文字列、文字列でない要素を含む ``list`` / ``tuple``。空の文字列は
    ``[]`` と同じ「未設定」として読まれる。辞書など、ほかの反復できる値はキーを並べた
    コマンドとして読まれ、例外にはならない。
    """
    if isinstance(command, str):
        return command != ""
    if not _iterable(command):
        return True
    if isinstance(command, (list, tuple)):
        return any(not isinstance(part, str) for part in command)
    return False


def _iterable(value: Any) -> bool:
    try:
        iter(value)
    except TypeError:
        return False
    return True


#: 真偽値で書くキー → (真のときの動き, 偽のときの動き)。ランタイムは ``bool(値)`` で読むので、
#: 文字列の ``"false"`` は真（有効のまま）、``0`` や ``null`` は偽（無効）になる。
_FLAGS: Dict[str, Tuple[str, str]] = {
    "schedule.hourly.enabled": ("時報が鳴ります", "時報は鳴りません"),
    "schedule.closing.enabled": ("閉館放送が鳴ります", "閉館放送は鳴りません"),
    "extra_segment.enabled": ("おまけ（天気・ひとこと）が流れます", "おまけ（天気・ひとこと）は流れません"),
    "weather.enabled": ("天気予報が流れます", "天気予報は流れません"),
    "time_signal.use_noon_template": ("正午は専用の文言（noon_template）で読み上げます",
                                      "正午も announce_template の文言になり（「午後12時を…」は作り置きに無いので）無音になります"),
}


def _check_flag(config: Config, key: str) -> List[Finding]:
    """真偽値のキーが ``true`` / ``false`` で書かれているか。

    ランタイムは値の真偽（``bool(値)``）で読むので動くが、``"false"`` や ``"no"`` は真になり
    （止めたつもりで有効のまま）、``null`` や ``0`` は偽になる（既定のつもりで無効になる）。
    ``time_signal.use_noon_template`` の ``null`` だけは、既定の値（``true``）で補われる。
    """
    value = _value(config, key)
    if value is _MISSING or isinstance(value, bool):
        return []
    if value is None and key == "time_signal.use_noon_template":
        return []
    on, off = _FLAGS[key]
    return [_warning(
        key, "{0} の値 {1} は true / false ではありません（{2}として扱われ、{3}）".format(
            key, _show(value), "真" if value else "偽", on if value else off),
        "true か false で書いてください（引用符で囲まない）")]


# -- 時・分 -------------------------------------------------------------------
_START_HOUR = "schedule.hourly.start_hour"
_END_HOUR = "schedule.hourly.end_hour"
_HOURLY = "schedule.hourly"
_CLOSING = "schedule.closing"

_HOUR_RANGE = "0〜23 の整数"
_HOUR_HOW = _HOUR_RANGE + "に直してください（24 時間制で書きます。午後 4 時なら 16）"


class _Field(NamedTuple):
    """閉館放送の時と、時報・閉館放送の分。範囲外だと日時を作る処理（``datetime()``）が例外になる。"""

    key: str
    section: str
    low: int
    high: int

    def text(self) -> str:
        return "{0}〜{1} の整数".format(self.low, self.high)

    def how_to_fix(self) -> str:
        tip = "（24 時間制で書きます。午後 4 時なら 16）" if self.high == 23 else ""
        return self.text() + "に直してください" + tip


_FIELDS = (
    _Field("schedule.closing.hour", _CLOSING, 0, 23),
    _Field("schedule.hourly.minute", _HOURLY, 0, 59),
    _Field("schedule.closing.minute", _CLOSING, 0, 59),
)


def _reading_note(value: Any, number: Any, integer: bool) -> str:
    """素直でない書き方の値を、実行時にどう読むかの説明にする。"""
    if isinstance(number, float) and not math.isfinite(number):
        return "有限の数値ではありません"
    if integer and isinstance(value, float) and value != number:
        return "小数部は切り捨てて {0} として読みます".format(number)
    return "{0} として読みます".format(_show(number))


def _style_warning(key: str, value: Any, number: Any, integer: bool, what: str) -> Finding:
    """数値の書き方が素直でない（文字列・小数・真偽値）という warning。動作は変わらない。"""
    return _warning(
        key, "{0} の値 {1} は{2}の書き方ではありません（{3}）".format(
            key, _show(value), "整数" if integer else "数値", _reading_note(value, number, integer)),
        "{0}をそのまま（引用符や小数点を付けずに）書いてください".format(what))


def _check_field(config: Config, field: _Field) -> List[Finding]:
    """閉館放送の時・時報と閉館放送の分が、整数として読めて範囲内か。

    ランタイムは ``int()`` で読み、``datetime()`` に渡す。読めない値と範囲外の値は例外
    （error）。ただし節が無効（``enabled`` が偽）なら ``Scheduler`` は読まないので warning。
    """
    key = field.key
    value = _value(config, key)
    if value is _MISSING:
        return []
    active = _is_active(config, field.section)
    unused = "" if active else _UNUSED_NOTE
    level = ERROR if active else WARNING
    number = _convert(value, True)
    if number is None:
        return [_leveled(
            level, key, "{0} の値 {1} は整数として読めません。予定を作る処理が例外になります{2}".format(
                key, _show(value), unused),
            field.how_to_fix())]
    if not field.low <= number <= field.high:
        return [_leveled(
            level, key, "{0} の値 {1} は使えません（{2}で指定します）。日時を作る処理が例外になります{3}".format(
                key, _show(value), field.text(), unused),
            field.how_to_fix())]
    if not _is_int(value):
        return [_style_warning(key, value, number, True, field.text())]
    return []


def _check_hour_bounds(config: Config) -> List[Finding]:
    """時報の ``start_hour`` / ``end_hour``。

    範囲外の時刻・``start_hour`` が ``end_hour`` より後ろ・数値の書き方の違いは warning
    （``Scheduler`` は 0〜23 の外の時刻を飛ばし、順序が逆なら時報を作らないだけで、動く）。
    整数として読めないものと、調べる範囲が広すぎて CPU を使い続けるものは error
    （どちらも、読み上げ文言の列挙が ``enabled`` に関わらず読むので、無効でも error）。
    """
    findings: List[Finding] = []
    numbers: Dict[str, int] = {}
    for key in (_START_HOUR, _END_HOUR):
        value = _value(config, key)
        if value is _MISSING:
            continue
        number = _convert(value, True)
        if number is None:
            findings.append(_error(
                key, "{0} の値 {1} は整数として読めません。時報の予定を作る処理が例外になります".format(
                    key, _show(value)), _HOUR_HOW))
        else:
            numbers[key] = number
    blamed = _span_blame(numbers)
    reversed_order = (not blamed and len(numbers) == 2 and numbers[_START_HOUR] > numbers[_END_HOUR]
                      and _is_active(config, _HOURLY))
    for key, number in numbers.items():
        value = _value(config, key)
        if key in blamed:
            findings.append(_error(
                key, "{0} の値 {1} では、時報の時刻を {2} 通り調べることになり、予定を作るたびに CPU を"
                     "使い続けます（時報の時刻は 0〜23 時の 24 通りです）".format(
                         key, _show(value), _count_text(_span_length(numbers))), _HOUR_HOW))
        elif not 0 <= number <= 23 and not reversed_order:
            findings.append(_warning(
                key, "{0} の値 {1} は {2}の範囲外で、その時刻の時報は鳴りません".format(
                    key, _show(value), _HOUR_RANGE), _HOUR_HOW))
        elif not _is_int(value):
            findings.append(_style_warning(key, value, number, True, _HOUR_RANGE))
    if reversed_order:
        findings.append(_warning(
            _START_HOUR,
            "{0} の {1} が end_hour の {2} より後ろなので、時報は 1 回も鳴りません".format(
                _START_HOUR, _show(numbers[_START_HOUR]), _show(numbers[_END_HOUR])),
            "start_hour は end_hour 以下にしてください（時報を止めたいときは、enabled を false にします）"))
    return findings


def _span_length(numbers: Mapping[str, int]) -> int:
    return numbers[_END_HOUR] - numbers[_START_HOUR] + 1


def _span_blame(numbers: Mapping[str, int]) -> List[str]:
    """調べる範囲が広すぎるとき、0〜23 の外にあるほうの時（どちらも外なら両方）を返す。"""
    if len(numbers) < 2 or _span_length(numbers) <= _MAX_HOUR_SPAN:
        return []
    outside = [key for key, number in numbers.items() if not 0 <= number <= 23]
    return outside or list(numbers)


def _count_text(count: int) -> str:
    """件数を文に入れる形に。巨大な数は桁を並べない（桁数が多すぎる整数は文字列にもできないので、ビット数から数える）。"""
    if count <= 10 ** 12:
        return "{0:,}".format(count)
    return "天文学的な数（約 {0} 桁）".format(int(count.bit_length() * math.log10(2)) + 1)


# -- 曜日・休みの時刻・天気の時刻（リスト） ---------------------------------------------
def _weekday_of(item: Any) -> Optional[int]:
    """曜日の要素が一致する曜日（0〜6）。どの曜日にも一致しなければ ``None``。

    ``Scheduler`` は ``day.weekday() in set(weekdays)`` で調べる。``1.0`` や ``True`` は
    ``1`` と等しく同じ値としてハッシュされるので火曜に一致し、``"1"``・``None``・7 は、
    どの曜日にも一致しない。
    """
    if isinstance(item, (int, float)):  # bool も int の仲間
        try:
            matched = item in range(7)
        except (TypeError, ValueError, OverflowError):
            return None
        return int(item) if matched else None
    return None


def _intended_day(item: Any) -> Optional[str]:
    """一致しない要素が、本当は何曜日のつもりか（``"0"`` なら「月曜」）。分からなければ ``None``。"""
    if isinstance(item, str) and len(item.strip()) == 1 and item.strip() in "0123456":
        return _WEEKDAY_NAMES[int(item.strip())] + "曜"
    return None


def _listed(texts: List[str], limit: int = 5) -> str:
    """要素の説明を「・」でつなぐ。多いときは先頭だけ。"""
    shown = "・".join(texts[:limit])
    return shown + "・ほか {0} 件".format(len(texts) - limit) if len(texts) > limit else shown


def _days_text(days: Any) -> str:
    return "・".join(_WEEKDAY_NAMES[day] for day in sorted(days)) or "なし"


def _check_weekdays(config: Config, key: str, section: str, label: str) -> List[Finding]:
    """曜日のリスト。``Scheduler`` は ``day.weekday() in set(値)`` で調べる。

    ``set()`` にできない値（数値・``null``・リストの中のリストや辞書）は例外（error）。
    文字列の曜日（``"0"``）・範囲外の数・``null`` の要素は、どの曜日にも一致しないだけで、
    その日は鳴らない（warning）。
    """
    value = _value(config, key)
    if value is _MISSING:
        return []
    try:
        set(value)
    except TypeError:
        level = ERROR if _is_active(config, section) else WARNING
        unused = "" if level == ERROR else _UNUSED_NOTE
        if isinstance(value, list):
            bad = [item for item in value if not _hashable(item)]
            return [_element_leveled(
                level, key, "{0} に曜日として読めない要素があります: {1}。{2}の予定を作る処理が"
                            "例外になります{3}".format(key, _show(bad), label, unused),
                "0〜6 の整数だけを並べてください（0=月曜 … 6=日曜）")]
        return [_leveled(
            level, key, "{0} は 0〜6 の整数のリストで指定します（今は {1}）。{2}の予定を作る処理が"
                        "例外になります{3}".format(key, _show(value), label, unused),
            "[ ] の中に 0〜6 の整数を並べてください（0=月曜 … 6=日曜）")]
    return _weekday_warnings(key, value, label)


def _element_leveled(level: str, key: str, message: str, how_to_fix: str,
                     shape: str = "リスト") -> Finding:
    """リスト（や表）の要素の問題。error なら、読めない要素だけを取り除き、残りの要素は使って動かす。"""
    if level != ERROR:
        return _warning(key, message, how_to_fix)
    hint = ("{0}。読めない要素だけを取り除き、残りの要素はそのまま使って動かします"
            "（{1}でないなど、取り除けないときは、直るまでは既定値 {2} で動かします）".format(
                how_to_fix, shape, _show(_default_at(key), None)))
    return Finding(ERROR, key, message, hint)


def _weekday_warnings(key: str, value: Any, label: str) -> List[Finding]:
    """``set()`` にできる曜日の値の、動作は変わらない問題（warning）。"""
    items = list(value)
    firing = {day for day in (_weekday_of(item) for item in items) if day is not None}
    lost = [item for item in items if _weekday_of(item) is None]
    untidy = [item for item in items if _weekday_of(item) is not None and not _is_int(item)]
    how = "[0, 1, 2, 3, 4] のように、0〜6 の整数（0=月曜 … 6=日曜）だけを並べてください"
    if firing:
        effect = "{0}が鳴る曜日は {1}、鳴らない曜日は {2} です".format(
            label, _days_text(firing), _days_text(set(range(7)) - firing))
    else:
        effect = "どの曜日にも{0}は鳴りません".format(label)
    findings: List[Finding] = []
    if isinstance(value, (str, Mapping)):
        findings.append(_warning(
            key, "{0} がリストではなく {1} です（1 文字・1 キーずつの要素として読まれ、曜日に一致しません）。"
                 "{2}".format(key, _show(value), effect), how))
    elif lost:
        shown = _listed(["{0}{1}".format(_show(item), "（{0}のつもり）".format(_intended_day(item))
                                       if _intended_day(item) else "") for item in lost])
        findings.append(_warning(
            key, "{0} の {1} は曜日に一致しません（曜日は 0〜6 の整数で書きます。文字列・範囲外の数・"
                 "null は一致しません）。{2}".format(key, shown, effect), how))
    if untidy:
        findings.append(_warning(
            key, "{0} の {1} は整数ではありません（整数と等しいので、同じ曜日として読みます）".format(
                key, _show(untidy)), how))
    return findings


def _check_skip_hours(config: Config) -> List[Finding]:
    """休みにする時刻のリスト。``Scheduler`` は ``{int(h) for h in 値}`` で読む。

    ``int()`` で読めない要素・反復できない値は例外（error）。範囲外（0〜23 の外）の要素は
    どの時刻にも一致しないだけ（warning）。
    """
    key = "schedule.hourly.skip_hours"
    value = _value(config, key)
    if value is _MISSING or not value:
        return []
    level = ERROR if _is_active(config, _HOURLY) else WARNING
    unused = "" if level == ERROR else _UNUSED_NOTE
    try:
        items = list(value)
    except TypeError:
        return [_leveled(
            level, key, "{0} は 0〜23 の整数のリストで指定します（今は {1}）。時報の予定を作る処理が"
                        "例外になります{2}".format(key, _show(value), unused),
            "[ ] の中に 0〜23 の整数を並べてください")]
    findings: List[Finding] = []
    broken = [item for item in items if _convert(item, True) is None]
    if broken:
        findings.append(_element_leveled(
            level, key, "{0} に整数として読めない要素があります: {1}。時報の予定を作る処理が例外になります{2}".format(
                key, _show(broken), unused),
            "0〜23 の整数だけを並べてください"))
    readable = [(item, _convert(item, True)) for item in items if _convert(item, True) is not None]
    findings.extend(_hour_list_warnings(key, value, readable, "休みにする時刻", "その時刻は休みになりません"))
    return findings


def _hour_list_warnings(key: str, value: Any, readable: List[Tuple[Any, int]], what: str,
                        outside_effect: str) -> List[Finding]:
    """時刻のリストの、動作は変わらない問題（範囲外の要素・整数でない書き方・リストでない値）。"""
    findings: List[Finding] = []
    how = "[12] のように、0〜23 の整数だけを並べてください"
    outside = [item for item, number in readable if not 0 <= number <= 23]
    if outside:
        findings.append(_warning(
            key, "{0} の {1} は 0〜23 の範囲外で、どの時刻にも一致しません（{2}）".format(
                key, _show(outside), outside_effect), how))
    if isinstance(value, (str, Mapping)):
        findings.append(_warning(
            key, "{0} がリストではなく {1} です（1 文字・1 キーずつの要素として読まれ、{2}は {3} になります）".format(
                key, _show(value), what, _show([number for _, number in readable])), how))
    else:
        untidy = [item for item, _ in readable if not _is_int(item)]
        if untidy:
            findings.append(_warning(
                key, "{0} の {1} は整数ではありません（{2} として読みます）".format(
                    key, _show(untidy), _show([int(item) for item in untidy])), how))
    return findings


def _check_weather_hours(config: Config) -> List[Finding]:
    """天気を流す時刻のリスト。どんな値でも例外にはならない（読めない要素は無視され、天気が流れないだけ）。"""
    key = "extra_segment.weather_hours"
    value = _value(config, key)
    if value is _MISSING or not value:
        return []
    try:
        items = list(value)
    except TypeError:
        return [_warning(
            key, "{0} は 0〜23 の整数のリストで指定します（今は {1}）。このままでは天気予報は流れません".format(
                key, _show(value)), "[ ] の中に 0〜23 の整数を並べてください")]
    findings: List[Finding] = []
    broken = [item for item in items if _convert(item, True) is None]
    if broken:
        findings.append(_warning(
            key, "{0} に整数として読めない要素があります: {1}。その要素は無視されます".format(
                key, _show(broken)), "[12] のように、0〜23 の整数だけを並べてください"))
    readable = [(item, _convert(item, True)) for item in items if _convert(item, True) is not None]
    findings.extend(_hour_list_warnings(key, value, readable, "天気を流す時刻", "その時刻に天気は流れません"))
    return findings


# -- 数値 -------------------------------------------------------------------
class _Number(NamedTuple):
    """数値で指定するキー。

    ``integer`` はランタイムが ``int()`` で読むか（そうでなければ ``float()``）。
    ``fatal`` は、読めない値のときに起動・再生が例外で止まるか（真なら error）、それとも
    その部品だけが失敗して放送は続くか（偽なら warning。ランタイムが例外を受け止める）。
    ``minimum`` / ``maximum`` は、外れると実害が出る範囲（``range_fatal`` が真なら error）。
    ``range_fatal`` が偽なのは、範囲を外れると読む部品が例外になるが、ランタイムがそれを受け止めて
    その部品だけを飛ばし、放送は続くもの（warning。置き換えない）。``shifts`` は、日時の
    計算（今からその秒数だけずらす）に使うか。
    """

    key: str
    integer: bool = False
    fatal: bool = True
    nullable: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    range_fatal: bool = True
    shifts: bool = False
    #: ``NaN`` で例外になるか（``time.sleep(nan)`` など）。``min()`` に渡るだけの ``NaN`` は無害。
    nan_breaks: bool = False
    #: この値をランタイムが読むか（設定から分かるとき）。読まないなら、読めない値でも warning。
    when: Optional[Callable[[Config], bool]] = None
    #: ``when`` が偽（読まれない）ときの、実際の動き。
    unused: str = ""
    #: 読めないときの、実際の動き。
    effect: str = ""
    #: ``minimum`` を下回る、または ``maximum`` を上回るときの、実際の動き。
    reason: str = ""


_STARTUP = "起動時に例外になり、systemd が再起動を繰り返します"
_PLAYER = "再生の準備が例外になり、放送が鳴りません"
_SIGNAL = "時報音を作れず、時報音が鳴りません（前に作った時報音があればそれを使います）"

#: 数値で読むキー。``fatal`` と ``effect`` は、ランタイムがその値をどこで読むかに合わせる
#: （起動時や再生のときなら error。``_append_time_signal`` などの ``_guard`` が受け止める所なら
#: warning）。
_NUMBERS = (
    _Number("schedule.max_sleep_seconds", effect=_STARTUP, minimum=1,
            reason="1 秒未満だと、待機ループが CPU を使い続けます"),
    _Number("schedule.pip_lead_seconds", nullable=True, shifts=True, effect=_STARTUP),
    _Number("schedule.prepare_lead_seconds", shifts=True, effect=_STARTUP),
    _Number("schedule.catchup_grace_seconds", shifts=True, effect=_STARTUP),
    _Number("audio.mixer.frequency", integer=True, effect=_PLAYER),
    _Number("audio.mixer.size", integer=True, effect=_PLAYER),
    _Number("audio.mixer.channels", integer=True, effect=_PLAYER),
    _Number("audio.mixer.buffer", integer=True, effect=_PLAYER),
    _Number("audio.gap_ms", integer=True, minimum=0, maximum=_MAX_GAP_MS, effect=_STARTUP,
            reason="セグメントの間の無音を待つ処理が例外になり、放送が途中で止まります"
                   "（負の値、または待てないほど大きい値）"),
    _Number("audio.fade_in_ms", integer=True, fatal=False,
            effect="蛍の光のフェードインが省かれます（音楽は鳴ります）"),
    _Number("audio.mock_max_seconds", minimum=0, nan_breaks=True,
            effect="mock バックエンドの再生が例外になります",
            reason="負の値や NaN だと、mock バックエンドの再生が例外になります"),
    _Number("time_signal.short_pip.frequency", fatal=False, effect=_SIGNAL),
    _Number("time_signal.short_pip.duration_ms", fatal=False, effect=_SIGNAL),
    _Number("time_signal.long_pip.frequency", fatal=False, effect=_SIGNAL),
    _Number("time_signal.long_pip.duration_ms", fatal=False, effect=_SIGNAL),
    _Number("time_signal.short_pip_count", integer=True, effect=_STARTUP),
    _Number("time_signal.pip_interval_ms", effect=_STARTUP),
    _Number("time_signal.volume", fatal=False, effect=_SIGNAL),
    _Number("time_signal.envelope_ms", fatal=False, effect=_SIGNAL),
    _Number("quotes.avoid_recent", integer=True, effect=_STARTUP),
    _Number("weather.timeout_seconds", effect=_STARTUP,
            minimum=-_MAX_TIMEOUT_SECONDS, maximum=_MAX_TIMEOUT_SECONDS, range_fatal=False,
            reason="通信の待ち時間として設定できる範囲（±約 92 億秒）を出ています。天気予報の取得が毎回"
                   "例外になり、天気予報が流れません（時報の読み上げとひとことは鳴ります）"),
    _Number("weather.cache_minutes", effect=_STARTUP),
    _Number("tts.voicevox.speaker", integer=True, when=_voicevox_listed, unused=_VOICEVOX_UNUSED,
            effect=_STARTUP),
    _Number("tts.voicevox.timeout_seconds", when=_voicevox_listed, unused=_VOICEVOX_UNUSED,
            effect=_STARTUP, minimum=-_MAX_TIMEOUT_SECONDS, maximum=_MAX_TIMEOUT_SECONDS,
            reason="通信の待ち時間として設定できる範囲（±約 92 億秒）を出ています。VOICEVOX ENGINE への"
                   "合成の依頼が毎回例外になり、VOICEVOX ENGINE で合成できません"),
    _Number("tts.voicevox.probe_timeout_seconds", when=_voicevox_listed, unused=_VOICEVOX_UNUSED,
            effect=_STARTUP, minimum=-_MAX_TIMEOUT_SECONDS, maximum=_MAX_TIMEOUT_SECONDS,
            reason="通信の待ち時間として設定できる範囲（±約 92 億秒）を出ています。VOICEVOX ENGINE の"
                   "疎通確認が毎回例外になり、VOICEVOX ENGINE を使えません"),
)


def _reference_moments() -> Tuple[datetime, datetime]:
    """日時の計算を試す、最も早い日付と最も遅い日付（今から、予定を探す範囲の前後）。"""
    now = datetime.now()
    return now - timedelta(days=1), now + timedelta(days=MAX_LOOKAHEAD_DAYS + 1)


def _shift_overflows(seconds: Any) -> bool:
    """今の前後の日付から ``seconds`` 秒ずらすと、日時の範囲（1〜9999 年）を出るか。

    ``Scheduler`` が ``moment - timedelta(seconds=…)`` で計算するのと同じ式で確かめる。
    ``NaN``・無限大・``timedelta`` の上限を超える値・巨大な整数は、どれも例外になる。
    """
    earliest, latest = _reference_moments()
    try:
        earliest - timedelta(seconds=seconds)
        latest - timedelta(seconds=seconds)
    except (OverflowError, ValueError, TypeError):
        return True
    return False


def _check_number(config: Config, rule: _Number) -> List[Finding]:
    """数値で書くキーが数値として読めるか、範囲を満たすか、日時の計算に耐えるか。

    ``"9"`` や ``9.0`` のように、ランタイムが ``int()`` / ``float()`` で読める書き方の違いは
    warning。読めない値は、起動や再生が例外で止まるもの（``fatal``）だけ error。
    """
    key = rule.key
    value = _value(config, key)
    if value is _MISSING or (value is None and rule.nullable):
        return []
    kind = "整数" if rule.integer else "数値"
    number = _convert(value, rule.integer)
    read = rule.when is None or rule.when(config)
    unused = "" if read else "（{0}）".format(rule.unused)
    if number is None:
        return [_leveled(
            ERROR if rule.fatal and read else WARNING, key,
            "{0} の値 {1} は{2}として読めません。{3}{4}".format(key, _show(value), kind, rule.effect, unused),
            kind + "で書いてください（引用符で囲まない）")]
    level = ERROR if read and rule.range_fatal else WARNING
    if (rule.minimum is not None and number < rule.minimum) or (rule.nan_breaks and number != number):
        return [_leveled(
            level, key, "{0} の値 {1} は{2}。{3}{4}".format(
                key, _show(value), "小さすぎます" if number == number else "数値として使えません",
                rule.reason, unused),
            "{0} 以上の{1}にしてください".format(rule.minimum, kind))]
    if rule.maximum is not None and number > rule.maximum:
        return [_leveled(
            level, key, "{0} の値 {1} は大きすぎます。{2}{3}".format(
                key, _show(value), rule.reason, unused),
            "{0} 以下の{1}にしてください".format(rule.maximum, kind))]
    if rule.shifts and _shift_overflows(number):
        return [_error(
            key, "{0} の値 {1} は大きすぎて、日時の計算が例外になります（1〜9999 年の範囲を出ます）。{2}".format(
                key, _show(value), _STARTUP),
            "秒数は現実的な値（数十秒〜数分）にしてください")]
    if not _is_plain(value, rule.integer):
        return [_style_warning(key, value, number, rule.integer, kind)]
    return []


_PIP_COUNT = "time_signal.short_pip_count"
_PIP_INTERVAL = "time_signal.pip_interval_ms"
_PIP_LEAD = "schedule.pip_lead_seconds"
_PREPARE_LEAD = "schedule.prepare_lead_seconds"


def _lead(count: Any, interval_ms: Any) -> Optional[float]:
    """時報の前倒しの秒数（短音の数 × 間隔）。``timesignal.lead_seconds`` と同じ計算。計算できなければ ``None``。"""
    try:
        return timesignal.lead_seconds({"short_pip_count": count, "pip_interval_ms": interval_ms})
    except (OverflowError, ValueError, TypeError):
        return None


def _culprits(count: int, interval: float, fails: Callable[[Optional[float]], bool]) -> List[str]:
    """短音の数と間隔のうち、それぞれ単独で（もう一方を既定値にして）``fails`` になるもの。

    どちらでもなければ（掛け合わせて初めて失敗する組み合わせなら）短音の数を原因とする。
    """
    found = [key for key, own in (
        (_PIP_COUNT, _lead(count, _default_at(_PIP_INTERVAL))),
        (_PIP_INTERVAL, _lead(_default_at(_PIP_COUNT), interval))) if fails(own)]
    return found or [_PIP_COUNT]


def _pip_overflow(config: Config, key: str, effect: str) -> Finding:
    return _error(
        key, "{0} の値 {1} は大きすぎます。{2}".format(key, _show(_value(config, key)), effect),
        "短音の数は 3 前後、間隔は 1000（ミリ秒）前後にしてください")


def _check_pip_timing(config: Config) -> List[Finding]:
    """短音の数 × 間隔（時報の前倒しの秒数）が、起動時と予定の計算に耐えるか。

    ``timesignal.lead_seconds`` は ``schedule.pip_lead_seconds`` の指定に関わらず、起動時に
    計算される（巨大な数どうしの掛け算は例外）。``pip_lead_seconds`` が ``null`` なら、その
    結果が予定の計算（今から何秒ずらすか）にも使われる。原因は、もう一方を既定値にすると
    直るほうのキー。
    """
    count = _convert(_value(config, _PIP_COUNT), True)
    interval = _convert(_value(config, _PIP_INTERVAL), False)
    if count is None or interval is None:
        return []  # 読めない値は、数値の検査が error にする
    lead = _lead(count, interval)
    if lead is None:
        effect = "起動時の計算（時報の前倒しの秒数）が例外になり、systemd が再起動を繰り返します"
        return [_pip_overflow(config, key, effect)
                for key in _culprits(count, interval, lambda own: own is None)]
    if _pip_lead_is_explicit(config) or not _shift_overflows(lead):
        return []
    effect = ("予定の日時の計算が例外になります（前倒しの秒数 {0} は 1〜9999 年の範囲を出ます）。"
              "{1}".format(_show(lead), _STARTUP))
    return [_pip_overflow(config, key, effect)
            for key in _culprits(count, interval, lambda own: own is None or _shift_overflows(own))]


def _pip_lead_is_explicit(config: Config) -> bool:
    """``schedule.pip_lead_seconds`` が、置き換えられた後も数値として残るか（残れば短音の計算は予定に使われない）。"""
    value = _value(config, _PIP_LEAD)
    if value is _MISSING or value is None:
        return False
    number = _convert(value, False)
    return number is not None and not _shift_overflows(number)


def _effective_pip_lead(config: Config) -> Optional[float]:
    """時報の前倒しの秒数（error のキーを既定値に戻した後の設定での値）。"""
    if _pip_lead_is_explicit(config):
        return _convert(_value(config, _PIP_LEAD), False)
    count = _convert(_value(config, _PIP_COUNT), True)
    interval = _convert(_value(config, _PIP_INTERVAL), False)
    lead = None if count is None or interval is None else _lead(count, interval)
    if lead is None or _shift_overflows(lead):
        lead = _lead(_default_at(_PIP_COUNT), _default_at(_PIP_INTERVAL))
    return lead


def _effective_prepare_lead(config: Config) -> Optional[float]:
    """準備の秒数（error のキーを既定値に戻した後の設定での値）。"""
    value = _value(config, _PREPARE_LEAD)
    number = None if value is _MISSING else _convert(value, False)
    if number is None or _shift_overflows(number):
        number = _convert(_default_at(_PREPARE_LEAD), False)
    return number


def _check_lead_sum(config: Config) -> List[Finding]:
    """時報の前倒しと準備の秒数を足したとき、日時の範囲を出ないか（それぞれは範囲内でも、足すと出る組み合わせ）。"""
    pip = _effective_pip_lead(config)
    prepare = _effective_prepare_lead(config)
    if pip is None or prepare is None or not _shift_overflows(pip + prepare):
        return []
    return [_error(
        _PREPARE_LEAD,
        "{0} の値 {1} は、時報の前倒し（{2} 秒）と足すと、予定の日時の計算が例外になります。{3}".format(
            _PREPARE_LEAD, _show(_value(config, _PREPARE_LEAD)), _show(pip), _STARTUP),
        "秒数は現実的な値（数十秒〜数分）にしてください")]


# -- 文字列で選ぶもの ---------------------------------------------------------
def _check_timezone(config: Config) -> List[Finding]:
    """``timezone`` が解決できるか。解決できないと OS のローカル時刻で動く（9 時間のずれ）。"""
    value = _value(config, "timezone")
    if value is _MISSING:
        return []
    try:
        ZoneInfo(str(value))  # ChimeApp も str() を通して引く
    except Exception as exc:  # 未知の名前・文字列でない値・tzdata 欠落のどれでも
        return [_error(
            "timezone",
            "timezone の値 {0} を解決できません（{1}）。このままだと OS のローカル時刻で動き、"
            "Pi では UTC のことが多く 9 時間ずれます".format(_show(value), type(exc).__name__),
            "IANA のタイムゾーン名（例: Asia/Tokyo）に直してください")]
    return []


def _check_log_level(config: Config) -> List[Finding]:
    """``logging.level`` がログの水準の名前か。

    綴りを間違えると、黙って INFO になる（意図した水準で動かない）か、数値でない属性を
    引いて起動時に例外になる。``null`` は「指定なし」（既定の水準）。
    """
    key = "logging.level"
    value = _value(config, key)
    if value is _MISSING or value is None:
        return []
    if isinstance(value, str) and value.upper() in _LOG_LEVELS:
        return []
    return [_error(
        key, "{0} の値 {1} はログの水準の名前ではありません。このままだと INFO で動くか、起動時に"
             "例外になります".format(key, _show(value)),
        "DEBUG・INFO・WARNING・ERROR・CRITICAL のどれかに直してください")]


def _check_log_format(config: Config) -> List[Finding]:
    """``logging.format`` がログの書式として使えるか。

    書式の組み立てだけでなく、実際に 1 件のログを整形してみる（``%(foo)s``・
    ``%(message)d``・余分な ``%s`` は、組み立てられても整形の段階で例外になり、すべての
    ログが失われる）。
    """
    key = "logging.format"
    value = _value(config, key)
    if value is _MISSING or value is None:
        return []
    record = logging.LogRecord("chime", logging.INFO, "", 0, "メッセージ", None, None)
    try:
        logging.Formatter(value).format(record)
    except Exception as exc:  # ValueError・TypeError・KeyError…どれでも、ログは使えない
        return [_error(
            key, "{0} の値 {1} はログの書式として使えません（{2}: {3}）。このままだと、すべてのログが"
                 "失われます".format(key, _show(value), type(exc).__name__, _show(str(exc))),
            "%(asctime)s や %(message)s のような書き方にしてください")]
    return []


def _check_audio_backend(config: Config) -> List[Finding]:
    """``audio.backend`` の名前。

    知らない名前は ``create_player`` が警告して ``auto`` として扱う（warning）。文字列
    でない値は ``.lower()`` が例外になる（error）。空・``null`` は ``auto``。
    """
    key = "audio.backend"
    value = _value(config, key)
    if value is _MISSING or not value:
        return []
    names = "・".join(_AUDIO_BACKENDS)
    if not isinstance(value, str):
        return [_error(
            key, "{0} の値 {1} は文字列ではありません。再生方式を選ぶ処理が例外になります".format(
                key, _show(value)),
            "{0} のどれかに直してください".format(names))]
    if value.lower() in _AUDIO_BACKENDS:
        return []
    return [_warning(
        key, "{0} の値 {1} は使えない名前です。自動選択（auto）として動きます".format(key, _show(value)),
        "{0} のどれかに直してください".format(names))]


def _check_tts_engines(config: Config) -> List[Finding]:
    """``tts.engines`` の名前。

    知らない名前は ``TTSService`` が警告して無視する（warning）。リストでなく文字列を書くと
    1 文字ずつの名前として読まれ、どれも使えず読み上げがすべて無音になる（warning）。
    反復できない値は例外（error）。

    ``prerecorded`` が無いとき（``[]``・``""``・``{}``・``["voicevox"]`` を含む）は、
    ``TTSService.prerecorded_lookup`` が何も引けず、Pi（VOICEVOX ENGINE が動かない）では
    読み上げがすべて無音になる。ランタイムは例外にならず、そのまま動くので warning
    （置き換えない。PC で VOICEVOX ENGINE だけを使う開発のために、あえて外すこともある）。
    1 つのキーに 1 件の指摘にまとめる。
    """
    key = "tts.engines"
    value = _value(config, key)
    if value is _MISSING:
        return []
    names = "・".join(_TTS_ENGINES)
    try:
        listed = [str(name) for name in value]
    except (TypeError, ValueError):  # 反復できない値、または文字列にできない（桁数が多すぎる）整数
        return [_error(key, "{0} はリストで指定します（今は {1}）。読み上げのエンジンを作る処理が"
                            "例外になります".format(key, _show(value)),
                       "[ ] の中に {0} を並べてください".format(names))]
    unknown = [name for name in listed if name not in _TTS_ENGINES]
    silent = "prerecorded" not in listed
    if not unknown and not silent:
        return []
    is_text = isinstance(value, (str, Mapping))
    problems = []
    if is_text:
        problems.append("{0} がリストではなく {1} です（1 文字・1 キーずつのエンジン名として読まれます）".format(
            key, _show(value)))
    elif unknown:
        problems.append("{0} に知らないエンジン名があります: {1}。その名前は無視されます".format(
            key, _show(unknown)))
    else:
        problems.append("{0} に prerecorded がありません（今は {1}）".format(key, _show(value)))
    if silent:
        problems.append(_silent_engines(listed, bool(unknown)))
    if is_text:
        how = "[ ] の中に {0} を並べてください".format(names)
    elif silent:
        how = ("tts.engines に \"prerecorded\" を入れてください（既定は {0}。Pi の声はすべて作り置きです）".format(
            _show(_default_at(key), None)))
        if unknown:
            how += "。使えるのは {0} だけです".format(names)
    else:
        how = "使えるのは {0} だけです".format(names)
    return [_warning(key, "。".join(problems), how)]


def _silent_engines(listed: List[str], has_unknown: bool) -> str:
    """``prerecorded`` が無いときの、読み上げへの影響の文。"""
    if not any(name in _TTS_ENGINES for name in listed):
        return "使えるエンジンが 1 つも{0}ので、Pi では読み上げがすべて無音になります（PC でも同じです）".format(
            "残らない" if has_unknown else "ない")
    return ("作り置き（prerecorded）を使わないので、Pi では読み上げがすべて無音になります"
            "（VOICEVOX ENGINE が動く PC では声が出ます）")


# -- 天気の地点 ---------------------------------------------------------------
#: 緯度・経度の取りうる範囲（絶対値の上限）。
_COORDINATES = (("latitude", "緯度", 90), ("longitude", "経度", 180))


def _check_locations(config: Config) -> List[Finding]:
    """天気の地点の緯度（-90〜90）・経度（-180〜180）。

    範囲外・数値でない座標は、天気 API が断るので、その地点の天気を取れないだけ（warning）。
    """
    key = "weather.open_meteo.locations"
    value = _value(config, key)
    if value is _MISSING or not value:
        return []
    if not isinstance(value, list):
        return [_warning(
            key, "{0} はリストで指定します（今は {1}）。このままでは天気予報は流れません".format(
                key, _show(value)),
            "[ ] の中に {\"label\": …, \"latitude\": …, \"longitude\": …} を並べてください")]
    findings: List[Finding] = []
    for number, location in enumerate(value, start=1):
        for problem in _location_problems(location):
            findings.append(_warning(
                key, "{0} の {1} 番目: {2}。その地点の天気予報は流れません".format(key, number, problem),
                "緯度は -90〜90、経度は -180〜180 です。緯度と経度を取り違えていませんか"))
    return findings


def _location_problems(location: Any) -> List[str]:
    """1 地点の問題を、文にして返す（無ければ空）。"""
    if not isinstance(location, Mapping):
        return ["地点が {{ … }} の形ではありません（{0}）".format(_show(location))]
    problems: List[str] = []
    for field, name, limit in _COORDINATES:
        number = location.get(field)
        if not _is_number(number):
            problems.append("{0} が数値ではありません（{1}）".format(field, _show(number)))
        elif abs(number) > limit:
            problems.append("{0}（{1}）{2} は -{3}〜{3} の範囲外です".format(
                field, name, _show(number), limit))
    return problems


# -- 読み上げ文のテンプレート -------------------------------------------------
#: ``format`` の置換に使える名前を、テンプレートごとに持つ。時刻アナウンスは
#: ``timesignal.hour_parts`` が渡す名前、天気の文は ``chime.weather`` の
#: ``_format_*_sentence`` が渡す名前。
_HOUR_FIELDS = tuple(timesignal.hour_parts(0, DEFAULT_CONFIG["time_signal"]))

_TEMPLATE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "time_signal.announce_template": _HOUR_FIELDS,
    "time_signal.noon_template": _HOUR_FIELDS,
    "weather.sentence_weather": ("when", "label", "weather"),
    "weather.sentence_temp": ("temp",),
    "weather.sentence_temp_max": ("temp_max",),
    "weather.sentence_pop": ("pop",),
}

#: テンプレートを実際に ``format`` してみるときの値（置換名の検査を通ったあとの、
#: ``{hour.foo}`` や ``{hour_reading:02d}`` のような使い方の誤りを見つける）。
_TEMPLATE_SAMPLES: Dict[str, Dict[str, Any]] = {
    "time_signal.announce_template": timesignal.hour_parts(10, DEFAULT_CONFIG["time_signal"]),
    "time_signal.noon_template": timesignal.hour_parts(12, DEFAULT_CONFIG["time_signal"]),
    "weather.sentence_weather": {"when": "今日", "label": "大津", "weather": "晴れ"},
    "weather.sentence_temp": {"temp": 20},
    "weather.sentence_temp_max": {"temp_max": 25},
    "weather.sentence_pop": {"pop": 50},
}

#: 実際に ``format`` してみるテンプレートの最大の長さ。
_MAX_TRIAL_TEMPLATE = 1000

#: 時刻アナウンスのテンプレートで、``SequenceBuilder`` が既定の文言に切り替える例外
#: （``_append_announce`` が受けるもの）。ほかの例外は、時刻アナウンスの部品が飛ばされる。
_FALLBACK_ERRORS = (KeyError, IndexError, ValueError)


def _placeholder_names(template: str) -> List[str]:
    """テンプレートが使う置換の名前（``{period}`` なら ``period``）を返す。

    ``{hour.real}`` や ``{names[0]}`` は先頭の名前だけ、``{x:{width}}`` の中の置換も
    数える。番号だけ・空の置換（``{0}``・``{}``）は名前が空または数字になる。
    波括弧の使い方が壊れていれば ``ValueError``。
    """
    names: List[str] = []
    for _literal, field, spec, _conversion in string.Formatter().parse(template):
        if field is None:
            continue
        names.append(re.split(r"[.\[]", field, maxsplit=1)[0])
        if spec:
            names.extend(_placeholder_names(spec))
    return names


class _TemplateProblem(NamedTuple):
    """テンプレートの問題 1 件。"""

    text: str
    #: ``SequenceBuilder`` が既定の文言に切り替える種類の失敗か（``KeyError`` / ``IndexError`` / ``ValueError``）。
    fallback: bool
    #: 書式指定の幅・桁数が上限を超えているか。
    width: bool = False
    #: そのうえで、例外にならず巨大な文になるか（9.2e18 以上は ``format`` が ``ValueError`` にする）。
    oversized: bool = False


def _template_problem(key: str, template: str) -> Optional[_TemplateProblem]:
    """テンプレートの問題を返す（無ければ ``None``）。

    置換名が使えないもの・波括弧が壊れているもの・番号だけの置換は、名前から分かる。
    それを通ったものは、実際に ``format`` してみて（``{hour.foo}`` や ``{hour_reading:02d}`` など）
    失敗するかを見る。ただし、書式指定の幅・桁数が :data:`_MAX_FORMAT_SPEC` を超えるものは、
    試さない（``{label:>200000000}`` は、試すだけで 200 MB の文字列を作る）。

    巨大な幅は、ランタイムを壊さない（前の版も、天気を止めていれば文を作らず、使っても
    巨大な文を作るだけで、作れなければ ``_guard`` がその部品だけ飛ばす）ので、error にはしない。
    ただし 9.2e18 以上の幅は ``format`` が ``ValueError`` にする（既定の文言への切り替えなど、
    ほかの書き間違いと同じ扱い）。
    """
    allowed = _TEMPLATE_FIELDS[key]
    try:
        names = _placeholder_names(template)
    except ValueError as exc:
        return _TemplateProblem("波括弧の使い方が正しくありません（{0}）".format(exc), True)
    unknown = [name for name in dict.fromkeys(names) if name not in allowed]
    if unknown:
        return _TemplateProblem(
            "使えない置換があります: {0}".format("・".join("{" + name + "}" for name in unknown)), True)
    if len(template) > _MAX_TRIAL_TEMPLATE:
        return None  # 長すぎるものは試さない（幅の指定が巨大だと、試すだけで時間とメモリを使う）
    widest = _widest_spec(template)
    if widest > _MAX_FORMAT_SPEC:
        rejected = widest > sys.maxsize  # ``format`` が桁数の多さで ``ValueError`` にする大きさ
        return _TemplateProblem(
            "書式指定の幅または桁数 {0} が、上限の {1} を超えています".format(
                _show(widest) if widest < 10 ** 18 else "巨大な数", _MAX_FORMAT_SPEC),
            rejected, True, not rejected)
    try:
        template.format(**_TEMPLATE_SAMPLES[key])
    except Exception as exc:
        return _TemplateProblem(
            "この書き方は使えません（{0}: {1}）".format(type(exc).__name__, _show(str(exc))),
            isinstance(exc, _FALLBACK_ERRORS))
    return None


def _spec_number(spec: str) -> int:
    """書式指定（``>200`` ``.5f`` ``02d`` など）に書かれた数の最大（幅・桁数）。無ければ 0。

    ``format`` が読むのは半角の数字だけ。桁数の多すぎる数（Python 3.11 以降は 4300 桁を超える
    文字列を整数にできない）は、巨大な値として扱う。
    """
    largest = 0
    for digits in re.findall(r"[0-9]+", spec):
        largest = max(largest, int(digits) if len(digits) < 4000 else 10 ** 4000)
    return largest


def _widest_spec(template: str) -> int:
    """テンプレートの書式指定（``:`` のあと。入れ子の中も）に書かれた数の最大。波括弧が壊れていれば ``ValueError``。"""
    return max((_spec_number(spec or "")
                for _literal, field, spec, _conversion in string.Formatter().parse(template)
                if field is not None), default=0)


def _template_effect(key: str, fallback: bool, oversized: bool = False) -> str:
    """壊れたテンプレートのとき、ランタイムが実際にどうするか。"""
    if oversized:
        if not key.startswith("time_signal."):
            return ("巨大な文になり、作り置きに無いので、その地点の天気予報は読み上げられません"
                    "（時報とひとことは鳴ります）")
        return "巨大な文になり、作り置きに無いので、時刻アナウンスは鳴りません（時報音とひとことは鳴ります）"
    if not key.startswith("time_signal."):
        return "この文を作れないので、その地点の天気予報は読み上げられません（時報とひとことは鳴ります）"
    if fallback:
        return "時報では既定の文言で読み上げます"
    return "時刻アナウンスの部品が飛ばされ、時報音とひとことだけが鳴ります"


def _check_template(config: Config, key: str) -> List[Finding]:
    """読み上げ文のテンプレートが、使える置換名だけを使っているか。

    作り置きの声は文言の完全一致で引く。時刻アナウンスのテンプレートの置換名の間違いは
    ``SequenceBuilder`` が既定の文言に切り替え、天気の文のテンプレートの間違いは、その
    地点の天気だけが流れなくなる。どちらも動くので warning。
    """
    value = _value(config, key)
    if value is _MISSING:
        return []
    names = "・".join("{" + name + "}" for name in _TEMPLATE_FIELDS[key])
    how = "使える置換は {0} です。波括弧そのものを書くときは {{{{ }}}} と二重にします".format(names)
    if not isinstance(value, str):
        if key.startswith("time_signal."):
            if value is None:
                return []  # null は既定の文言になる（timesignal が既定設定で補う）
            effect = _template_effect(key, False)
        else:
            # 天気の文は str() を通して使われる（null は "None" という文になる）
            effect = "{0} という文として扱われ、作り置きに無いので無音になります".format(_show(str(value)))
        return [_warning(
            key, "{0} は文字列ではありません（{1}）。{2}".format(key, _show(value), effect),
            "文字列で書いてください（読み上げない文は空文字列 \"\" にします）")]
    problem = _template_problem(key, value)
    if problem is None:
        return []
    if problem.width:
        how = "書式指定の幅や桁数（: のあとの数字。{{label:>20}} の 20 など）は {0} 以下にしてください".format(
            _MAX_FORMAT_SPEC)
    return [_warning(key, "{0} のテンプレートは使えません（{1}）。{2}".format(
        key, problem.text, _template_effect(key, problem.fallback, problem.oversized)), how)]


# -- 天気の作り置きの範囲 ---------------------------------------------------------
_PRERECORD = "weather.prerecord"


def _check_prerecord(config: Config) -> List[Finding]:
    """作り置きする気温・降水確率の範囲が、現実的か。

    実行時の放送では、これらは作り置きの声が引けるかどうかを決めるだけ（範囲の外の値は
    その文だけ無音になる）で、例外にはならない。ただし範囲が広いと、作り置きの文言の
    数え上げが巨大になり、起動時の確認や音声の生成が終わらなくなる（warning）。
    """
    findings: List[Finding] = []
    low_key, high_key = _PRERECORD + ".temp_min", _PRERECORD + ".temp_max"
    raw_low, raw_high = _value(config, low_key), _value(config, high_key)
    if raw_low is not _MISSING and raw_high is not _MISSING:
        low, high = _convert(raw_low, True), _convert(raw_high, True)
        for key, raw, number in ((low_key, raw_low, low), (high_key, raw_high, high)):
            if number is None:
                findings.append(_warning(
                    key, "{0} の値 {1} は整数として読めません。気温の作り置きは 0 件として扱われ、気温の文は"
                         "無音になります".format(key, _show(raw)),
                    "整数（例: -5 や 40）で書いてください"))
        if low is not None and high is not None:
            findings.extend(_temp_range_findings(low, high, low_key, high_key, raw_low, raw_high))
    key = _PRERECORD + ".pop_step"
    raw_step = _value(config, key)
    if raw_step is not _MISSING:
        step = _convert(raw_step, True)
        if step is None or step <= 0:
            findings.append(_warning(
                key, "{0} の値 {1} は 1 以上の整数ではありません。降水確率は丸められず、降水確率の"
                     "作り置きも 0 件になります（sentence_pop を使うと無音になります）".format(
                         key, _show(raw_step)),
                "1 以上の整数（例: 10）で書いてください"))
    return findings


def _temp_range_findings(low: int, high: int, low_key: str, high_key: str,
                         raw_low: Any, raw_high: Any) -> List[Finding]:
    count = high - low + 1
    if count <= 0:
        return [_warning(
            low_key, "{0} の {1} が temp_max の {2} より大きいので、気温の作り置きは 0 件になり、気温の文は"
                     "無音になります".format(low_key, _show(raw_low), _show(raw_high)),
            "temp_min は temp_max 以下にしてください")]
    if count > _MAX_TEMP_VALUES:
        # 既定値から遠いほうを原因とする
        far_low = abs(low - _default_at(low_key)) > abs(high - _default_at(high_key))
        return [_warning(
            low_key if far_low else high_key,
            "{0} と {1}（{2}〜{3}）では、気温の読み上げ文が {4} 件になります（既定は 46 件）。作り置きの"
            "文言の数え上げと音声の生成が、巨大になって終わらなくなります".format(
                low_key, high_key, _show(raw_low), _show(raw_high), _count_text(count)),
            "気温の範囲は、実際に出る値（例: -5〜40）に絞ってください")]
    return []


# ---------------------------------------------------------------------------
# 検査の一覧
# ---------------------------------------------------------------------------
Check = Callable[[Config], List[Finding]]


def _checks() -> List[Tuple[str, Check]]:
    """``(キー, 検査)`` の一覧。検査が例外を出したとき、そのキーの項目を「検査できませんでした」にする。

    検査は 1 キー（または 1 組のキー）ごとに分けてある。呼ぶたびに作るので、モジュールの
    検査関数を差し替えるテストにも効く。
    """
    checks: List[Tuple[str, Check]] = [("", _check_sections), ("audio.commands", _check_commands)]
    checks.extend((key, functools.partial(_check_flag, key=key)) for key in _FLAGS)
    checks.extend((field.key, functools.partial(_check_field, field=field)) for field in _FIELDS)
    checks.append((_START_HOUR, _check_hour_bounds))
    for key, section, label in (("schedule.hourly.weekdays", _HOURLY, "時報"),
                                ("schedule.closing.weekdays", _CLOSING, "閉館放送")):
        checks.append((key, functools.partial(_check_weekdays, key=key, section=section, label=label)))
    checks.append(("schedule.hourly.skip_hours", _check_skip_hours))
    checks.append(("extra_segment.weather_hours", _check_weather_hours))
    checks.extend((rule.key, functools.partial(_check_number, rule=rule)) for rule in _NUMBERS)
    checks.extend([
        (_PIP_COUNT, _check_pip_timing),
        (_PREPARE_LEAD, _check_lead_sum),
        ("timezone", _check_timezone),
        ("logging.level", _check_log_level),
        ("logging.format", _check_log_format),
        ("audio.backend", _check_audio_backend),
        ("tts.engines", _check_tts_engines),
        ("weather.open_meteo.locations", _check_locations),
    ])
    checks.extend((key, functools.partial(_check_template, key=key)) for key in _TEMPLATE_FIELDS)
    checks.append((_PRERECORD, _check_prerecord))
    return checks


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------
def check_config(config: Config) -> List[Finding]:
    """読み込んだ設定の全問題を、重大な順（error → warning → info）に返す。

    ``config.sources`` の設定ファイルを読み直して書き方を見て
    （:func:`walk_overrides`）、マージ後の値を検査する（:func:`validate`）。
    既定の設定では何も返さない。値の出どころのファイルが分かる問題には
    ``source`` を付ける。
    """
    findings: List[Finding] = []
    overrides: List[Tuple[str, Mapping[str, Any]]] = []
    for source in config.sources:
        if source == _DEFAULTS_SOURCE:
            continue
        override = _reread(source, findings)
        if override is not None:
            overrides.append((source, override))
            try:
                findings.extend(walk_overrides(override, source))
            except Exception as exc:  # 書き方の検査の不具合で、--check まで止めない
                findings.append(replace(_unchecked("", exc), source=source))
    findings.extend(_with_sources(validate(config), overrides))
    return sorted(findings, key=lambda finding: _LEVEL_ORDER.get(finding.level, len(_LEVEL_ORDER)))


def _reread(source: str, findings: List[Finding]) -> Optional[Mapping[str, Any]]:
    """設定ファイルを読み直す。読めなければ warning を足して ``None``。"""
    try:
        loaded = read_json(source)
    except JsonFileError as exc:
        findings.append(Finding(
            WARNING, "", "検査のために設定ファイルを読み直せませんでした: {0}".format(exc),
            "", source))
        return None
    if not isinstance(loaded, Mapping):
        findings.append(Finding(
            WARNING, "", "設定ファイルのトップレベルがオブジェクトではありません", "", source))
        return None
    return loaded


def _with_sources(findings: List[Finding],
                  overrides: List[Tuple[str, Mapping[str, Any]]]) -> List[Finding]:
    """出どころの無い問題に、そのキーを最後に書いた設定ファイルを添える。"""
    result: List[Finding] = []
    for finding in findings:
        source = finding.source or _defining_source(finding.key, overrides)
        result.append(replace(finding, source=source))
    return result


def _defining_source(key: str, overrides: List[Tuple[str, Mapping[str, Any]]]) -> str:
    """``key`` を書いている最後の設定ファイル（どれにも無ければ空）。"""
    for source, override in reversed(overrides):
        node: Any = override
        for part in key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                break
            node = node[part]
        else:
            return source
    return ""


# ---------------------------------------------------------------------------
# 置き換え
# ---------------------------------------------------------------------------
def sanitized(config: Config) -> Tuple[Config, List[Finding]]:
    """error の出たキーだけを直した設定と、その error の一覧を返す。

    直し方は、そのキーを既定値に戻すこと。ただしリストは、ランタイムが動くのに必要な
    分（``int()`` で読めない ``skip_hours`` の要素・ハッシュできない ``weekdays`` の要素・
    コマンドとして読めない ``audio.commands`` の項目）だけを取り除き、残りは使う
    （:data:`_REPAIRS`）。warning と info の項目は
    変えない（前の版が動かしていた設定の動作を変えないため）。

    元の ``config`` は変えない。返す設定の ``base_dir`` と ``sources`` は元のまま。
    error が無ければ、元と等しい設定を返す。
    """
    errors = [finding for finding in validate(config) if finding.level == ERROR]
    data = copy.deepcopy(config.data)
    for key in dict.fromkeys(finding.key for finding in errors):
        _repair(data, key)
    return Config(data, base_dir=config.base_dir, sources=config.sources), errors


def _repair_skip_hours(value: Any) -> Any:
    """``int()`` で読めない要素だけを取り除く（リストでなければ既定値に戻す）。"""
    if not isinstance(value, list):
        return _MISSING
    return [item for item in value if _convert(item, True) is not None]


def _repair_weekdays(value: Any) -> Any:
    """ハッシュできない要素だけを取り除く（リストでなければ既定値に戻す）。"""
    if not isinstance(value, list):
        return _MISSING
    return [item for item in value if _hashable(item)]


def _repair_commands(value: Any) -> Any:
    """コマンドとして読めない項目だけを直す（表でなければ既定値に戻す）。

    読めない項目は、その拡張子の既定の項目（``.wav`` なら ``aplay``、``.mp3`` なら ``mpg123``。
    拡張子は ``CommandPlayer`` と同じく小文字にして引く）に置き換える。既定に無い拡張子の
    項目は取り除く（外部コマンドで再生する方式は、その拡張子を鳴らせないが、もともと鳴らせて
    いなかった）。読める項目は、書かれたとおりに残す。
    """
    if not isinstance(value, dict):
        return _MISSING
    defaults = _default_at("audio.commands")
    fixed = {}
    for name, command in value.items():
        if not _command_breaks(command):
            fixed[name] = command
        elif str(name).lower() in defaults:
            fixed[name] = copy.deepcopy(defaults[str(name).lower()])
    return fixed


#: 既定値に戻さず、要素だけを直すキー。値を受け取り、直した値（または、既定値に戻すべき
#: なら :data:`_MISSING`）を返す。
_REPAIRS: Dict[str, Callable[[Any], Any]] = {
    "audio.commands": _repair_commands,
    "schedule.hourly.skip_hours": _repair_skip_hours,
    "schedule.hourly.weekdays": _repair_weekdays,
    "schedule.closing.weekdays": _repair_weekdays,
}


def _repair(data: Dict[str, Any], key: str) -> None:
    """``data`` の ``key`` を直す（要素だけを直せるものは要素だけ、ほかは既定値に戻す）。"""
    repair = _REPAIRS.get(key)
    if repair is not None:
        try:
            node: Any = data
            for part in key.split("."):
                node = node[part]
            fixed = repair(node)
        except Exception:  # 直せなければ、既定値に戻す
            fixed = _MISSING
        if fixed is not _MISSING:
            _assign(data, key, fixed)
            return
    _set_default(data, key)


def _assign(data: Dict[str, Any], key: str, value: Any) -> None:
    """``data`` の ``key`` に ``value`` を入れる（途中の節が辞書でなければ作り直す）。"""
    *parents, leaf = key.split(".")
    node = data
    for part in parents:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[leaf] = value


def _set_default(data: Dict[str, Any], key: str) -> None:
    """``data`` の ``key`` を既定値にする（途中の節が辞書でなければ作り直す）。"""
    _assign(data, key, copy.deepcopy(_default_at(key)))
