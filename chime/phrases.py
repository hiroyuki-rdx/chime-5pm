"""チャイムが読み上げうる全文言の列挙。

ここで数えた文言が、そのまま「作り置きしておかなければならない文言」になる。
Pi には実行時の音声合成が無く、声は文言の**完全一致**で作り置き
（``assets/voice/manifest.json``）から引くため、列挙から漏れた文言はその文だけ
無音になる。列挙の結果は ``scripts/generate_voicevox.py``（WAV の生成と
``--prune``）・テスト・CI の ``prerecorded-only`` ジョブが使う。作り置きの声が
揃っているかどうかの集計は :func:`coverage`（起動のたびに呼ばれるので、設定の
値で文言が増えすぎる・長すぎるときは数えずに :class:`CoverageTooLarge`）。

このモジュールは ``chime.audio`` / ``chime.sequence`` / ``chime.app`` を
import しない（pygame を引き込まないため）。生成スクリプトは PC 側で動くので、
再生系の依存が無くても使えるようにしておく。
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from . import scheduler, timesignal, weather
from .config import BASE_DIR, DEFAULT_CONFIG, Config
from .quotes import load_quotes
from .tts import normalize_phrase

# 文言は、実行時の照合（``TTSService``）と同じ整え方（``normalize_phrase``。前後の
# 空白を落とす）にそろえて列挙する。そろえないと、前後に空白のある
# ``closing.extra_text`` やひとことは空白つきのキーで作られてしまい、実行時には
# 引けずに、その文だけ永久に無音になる。整えて空になる文言は数えない。

#: 文言の種類。:class:`Coverage` の ``by_kind`` のキーで、列挙の順。
PHRASE_KINDS = ("announce", "closing", "quote", "weather")

#: :func:`coverage` が数える文言の数の上限（既定）。設定の値で文言の数はいくらでも
#: 増やせる（気温の幅など）。起動のたびに重い列挙をして、常駐が始まらなくなる
#: ことのないよう、これを超える設定は数えずに :class:`CoverageTooLarge` にする。
#: 既定の設定は 138 件。
MAX_COVERAGE_PHRASES = 2000

#: :func:`coverage` が数える文言の**文字数の合計**の上限（既定）。件数の上限だけでは、
#: 1 件が巨大な設定（地点の ``label`` が長い、テンプレートの書式指定の幅が大きい）で、
#: 起動のたびに数秒と数百 MB〜数 GB を使ってしまう（Pi 3B は 1 GB）。既定の設定は
#: 約 4,000 文字。文言を作る前の見積もりと、作ったあとの合計の両方で確かめる。
MAX_COVERAGE_CHARS = 2_000_000

#: テンプレートの書式指定（``{label:>200}`` の 200 や ``{x:.5}`` の 5）に書ける数の上限。
#: これを超える数（``{label:>200000000}``）は、1 つの文を何億文字にもする。
#: ``chime.configcheck`` の検査と同じ値（``tests/test_configcheck.py`` が一致を確かめる）。
MAX_FORMAT_SPEC = 1000

#: 気温の作り置き（``weather.prerecord.temp_min`` 〜 ``temp_max``）の幅の上限（度）。
#: 既定は 46 度分（-5〜40）。
MAX_TEMP_SPAN = 200


class CoverageTooLarge(ValueError):
    """設定の文言が多すぎる・長すぎて、作り置きの数え上げを省くとき（:func:`coverage` が出す）。

    メッセージは、何が多すぎるか（長すぎるか）と、どの設定のキーを直すかを日本語で述べる。
    そのままログや画面に出せる（利用者に例外の型名は見せない）。
    """


def _unique(phrases: Iterable[str]) -> List[str]:
    """文言を整え、空でないものを最初に現れた順のまま重複なしで返す。"""
    normalized = (normalize_phrase(phrase) for phrase in phrases)
    return list(dict.fromkeys(phrase for phrase in normalized if phrase))


def announcement_phrases(config: Config) -> Iterator[str]:
    """時報の読み上げ文言を、``start_hour``〜``end_hour`` の時刻ごとに返す。

    重複は除かず、``skip_hours`` も見ない（休みにしている時刻の文言も作り置き
    しておく）。ジェネレーターなので、テンプレートが壊れていても例外が出るのは
    その時刻まで進んだときになる（呼び出し側が 1 件ずつ処理できる）。
    """
    settings = config.section("time_signal")
    for hour in scheduler.hourly_hours(config.section("schedule.hourly")):
        yield timesignal.announce_text(hour, settings)


def closing_extra_text(config: Config) -> str:
    """閉館放送の最後に足す文言（``closing.extra_text``）。無ければ空文字列。

    実行時の組み立てと作り置きの列挙が同じ読み方をするよう、読み出しを
    ここに一本化している。
    """
    return str(config.get("closing.extra_text", "") or "")


def closing_phrases(config: Config) -> List[str]:
    """閉館放送に足す文言（設定されていれば 1 件、無ければ空）。"""
    text = closing_extra_text(config)
    return [text] if text else []


def quote_phrases(config: Config) -> List[str]:
    """ひとことの全文言を、``general``、``by_hour``（ファイルの順）の順に返す。"""
    quotes = load_quotes(config.path("quotes.file"))
    phrases = list(quotes.get("general", []))
    for values in quotes.get("by_hour", {}).values():
        phrases.extend(values)
    return phrases


def _span(values: range) -> int:
    """範囲の長さ。``len()`` と違い、``sys.maxsize`` を超える範囲でも例外にならない。"""
    return max(0, values.stop - values.start)


def _estimate_work(config: Config) -> int:
    """文言を作る前に、作る手間（繰り返しの回数）を見積もる。

    設定の値で手間が増えるのは、時報の時刻の幅・天気の「地点 × いつ × 天気の種類」・
    気温の幅・降水確率の刻みで、どれも作る前に数式で分かる。重複を除く前の
    繰り返しの回数を数える（手間は繰り返しの回数に比例するので、重複で減る分は
    引かない。実際の件数以上になる）。読み出しは ``weather.prerecord_phrases`` と
    同じものを使う。食い違うと見積もりが外れる。読み出しで例外になる設定は、
    その例外のまま列挙に任せる（ここでは新しい種類の失敗にしない）。

    気温の幅が :data:`MAX_TEMP_SPAN` を超えるとき（値が大きすぎて整数にできないときも）
    は、数えずに :class:`CoverageTooLarge`。
    """
    work = _span(scheduler.hourly_hours(config.section("schedule.hourly")))

    settings = config.section("weather")
    prerecord = settings.get("prerecord", {}) or {}

    if weather._sentence_template(settings, "sentence_weather"):
        whens = list(prerecord.get("whens", []) or [])
        work += len(weather._locations(settings)) * len(whens) * len(weather.WMO_CODES)

    temp_templates = sum(1 for key in ("sentence_temp", "sentence_temp_max")
                         if weather._sentence_template(settings, key))
    if temp_templates:
        try:
            span = _span(weather._prerecord_temp_range(prerecord))
        except OverflowError:  # 無限大（1e999）などは int にできない
            raise CoverageTooLarge(
                "気温の作り置き（weather.prerecord.temp_min〜temp_max）の値が大きすぎます。"
                + _TEMP_HINT) from None
        if span > MAX_TEMP_SPAN:
            raise CoverageTooLarge(
                "気温の作り置き（weather.prerecord.temp_min〜temp_max）が {0} 度分あります"
                "（上限は {1} 度分）。{2}".format(span, MAX_TEMP_SPAN, _TEMP_HINT))
        work += span * temp_templates

    if weather._sentence_template(settings, "sentence_pop"):
        work += 101  # 0〜100 を刻みで割った数。刻みが 1 のときが最大
    return work


#: 気温の幅が広すぎるときの直し方。
_TEMP_HINT = ("weather.prerecord の temp_min と temp_max を、実際に出る気温の範囲（例: -5〜40）に"
              "絞ってください")

#: 件数が多すぎるときの直し方（作る前の見積もりで、何が増えたかまでは分からない）。
_COUNT_HINT = ("weather.prerecord の whens・temp_min・temp_max、weather.open_meteo.locations、"
               "時報の時刻（schedule.hourly）のうち、増やしたものを減らしてください")


def _check_size_before(config: Config, limit: int) -> None:
    """文言を作る前の見積もり(:func:`_estimate_work`)が ``limit`` を超えるなら ``CoverageTooLarge``。"""
    work = _estimate_work(config)
    if work > limit:
        raise CoverageTooLarge("作り置きする文言が多すぎます（約 {0} 件。上限は {1} 件）。{2}".format(
            work, limit, _COUNT_HINT))


# -- 文言の長さ（件数が少なくても、1 件が巨大だと手間とメモリを使い切る） ----------------
def _spec_number(spec: str) -> int:
    """書式指定（``>200`` ``.5f`` ``02d`` など）に書かれた数の最大（幅・桁数）。無ければ 0。

    桁数の多すぎる数は、整数に直さず巨大な値として扱う（Python 3.11 以降は 4300 桁を超える
    文字列を整数にできない）。``format`` が読むのは半角の数字だけ。
    """
    largest = 0
    for digits in re.findall(r"[0-9]+", spec):
        largest = max(largest, 10 ** 9 if len(digits) > 9 else int(digits))
    return largest


def _int_chars(number: int) -> int:
    """整数を文字にしたときの長さ（桁数の多すぎる整数は文字列にできないので、ビット数から見積もる）。"""
    if number.bit_length() < 4000:
        return len(str(number))
    return number.bit_length() // 3 + 1


def _count_text(count: int) -> str:
    """件数・文字数を文に入れる形に（巨大な数は、桁を並べない）。"""
    return "{0:,}".format(count) if count < 10 ** 15 else "天文学的な数"


class _Shape:
    """テンプレートの形。置換でない部分の文字数と、置換ごとの ``(名前, 書式指定の数)``。"""

    def __init__(self, template: str) -> None:
        self.literal = 0
        self.fields: List[Tuple[str, int]] = []
        for text, field, spec, _conversion in string.Formatter().parse(template):
            self.literal += len(text)
            if field is not None:
                self.fields.append((re.split(r"[.\[]", field, maxsplit=1)[0],
                                    _spec_number(spec or "")))

    @property
    def widest(self) -> int:
        """書式指定に書かれた数の最大（無ければ 0）。"""
        return max((number for _, number in self.fields), default=0)

    def chars(self, lengths: Mapping[str, int]) -> int:
        """置換の値の長さが ``lengths`` のとき、1 つの文が取りうる最大の文字数。

        置換 1 つは、値の長さと書式指定の幅のうち長いほうになる（精度で切り詰めたり、
        値が短かったりすれば、これより短い）。名前が分からない置換は 0 文字に数える
        （そのテンプレートは ``format`` で例外になり、列挙が同じ例外にする）。
        """
        return self.literal + sum(max(number, lengths.get(name, 0)) for name, number in self.fields)


def _shape_of(key: str, template: str) -> Optional[_Shape]:
    """``template`` の形。波括弧が壊れていれば ``None``（列挙が同じ例外にする）。

    書式指定の数が :data:`MAX_FORMAT_SPEC` を超えるなら ``CoverageTooLarge``
    （``{label:>200000000}`` は、1 つの文を 2 億文字にする）。
    """
    try:
        shape = _Shape(template)
    except ValueError:
        return None
    if shape.widest > MAX_FORMAT_SPEC:
        raise CoverageTooLarge(
            "{0} の書式指定に、幅または桁数 {1} が書かれています（上限は {2}）。1 つの文が"
            "巨大になるので、作り置きの数え上げを省きます。{0} の : のあとの数字を {2} 以下に"
            "してください".format(key, shape.widest if shape.widest < 10 ** 9 else "巨大な数",
                           MAX_FORMAT_SPEC))
    return shape


def _announce_templates_checked(config: Config) -> None:
    """時刻アナウンスのテンプレートの書式指定が大きすぎないか（時刻が 1 つでもあるときだけ）。"""
    if not _span(scheduler.hourly_hours(config.section("schedule.hourly"))):
        return
    settings = config.section("time_signal")
    for name in ("announce_template", "noon_template"):
        template = settings.get(name)
        if isinstance(template, str):
            _shape_of("time_signal." + name, template)


def _weather_chars(config: Config, max_chars: int) -> List[Tuple[str, int]]:
    """天気の文言の文字数の見積もり（上限）を ``(設定のキー, 文字数)`` で返す。

    作る前に、テンプレートの形・地点の ``label``・「いつ」の語・気温の幅から、文言の数 ×
    1 つの文の最大の長さで見積もる。合計が ``max_chars`` を超えたところで打ち切る。
    """
    settings = config.section("weather")
    prerecord = settings.get("prerecord", {}) or {}
    sizes: List[Tuple[str, int]] = []

    template = weather._sentence_template(settings, "sentence_weather")
    whens = list(prerecord.get("whens", []) or []) if template else []
    locations = weather._locations(settings) if template else []
    if template and whens and locations:
        shape = _shape_of("weather.sentence_weather", template)
        if shape is not None:
            lengths = {"when": max(len(str(when)) for when in whens),
                       "weather": max(len(word) for word in weather.WMO_CODES.values())}
            each = len(whens) * len(weather.WMO_CODES)
            chars = 0
            for location in locations:
                lengths["label"] = len(weather._location_label(location))
                chars += shape.chars(lengths) * each
                if chars > max_chars:
                    break
            sizes.append(("weather.sentence_weather", chars))

    values = weather._prerecord_temp_range(prerecord)
    for name in ("sentence_temp", "sentence_temp_max"):
        template = weather._sentence_template(settings, name)
        if template and _span(values):
            shape = _shape_of("weather." + name, template)
            if shape is not None:
                digits = max(_int_chars(values.start), _int_chars(values.stop - 1))
                sizes.append(("weather." + name,
                              shape.chars({"temp": digits, "temp_max": digits}) * _span(values)))

    template = weather._sentence_template(settings, "sentence_pop")
    if template:
        shape = _shape_of("weather.sentence_pop", template)
        if shape is not None:
            sizes.append(("weather.sentence_pop", shape.chars({"pop": 3}) * 101))
    return sizes


#: 文言が長すぎるときの直し方（見積もりで最も大きかった設定のキーごと）。
_CHARS_HINTS = {
    "weather.sentence_weather": "weather.open_meteo.locations の label を短くするか、weather.prerecord の "
                                "whens と地点を減らしてください",
    "weather.sentence_temp": "weather.sentence_temp と weather.prerecord の temp_min・temp_max を"
                             "見直してください",
    "weather.sentence_temp_max": "weather.sentence_temp_max と weather.prerecord の temp_min・"
                                 "temp_max を見直してください",
    "weather.sentence_pop": "weather.sentence_pop を短くしてください",
}


def _check_chars_before(config: Config, limit: int) -> None:
    """文言を作る前の文字数の見積もりが ``limit`` を超えるなら ``CoverageTooLarge``。

    見積もるのは、設定の値で文言が増える・長くなる天気の文言（地点の ``label`` ×
    「いつ」× 天気の種類・気温の幅・降水確率の刻み）と、時刻アナウンスのテンプレートの
    書式指定。時刻アナウンスの文言の長さは、作りながら数える（:class:`_Chars`）。
    読み出しで例外になる設定は、その例外のまま列挙に任せる（見積もりは新しい種類の
    失敗にしない）。
    """
    try:
        _announce_templates_checked(config)
        sizes = _weather_chars(config, limit)
    except CoverageTooLarge:
        raise
    except Exception:
        return
    total = sum(chars for _, chars in sizes)
    if total > limit:
        biggest = max(sizes, key=lambda size: size[1])[0]
        raise CoverageTooLarge(
            "作り置きする文言が長すぎます（天気の文言で約 {0} 文字。上限は {1} 文字）。{2}".format(
                _count_text(total), _count_text(limit), _CHARS_HINTS[biggest]))


class _Chars:
    """作った文言の文字数を数え、合計が上限を超えたところで ``CoverageTooLarge`` にする。"""

    def __init__(self, limit: Optional[int]) -> None:
        self.limit = limit
        self.used = 0

    def take(self, texts: Iterable[str]) -> List[str]:
        """``texts`` を順に作って数え、リストにして返す（上限を超えたら、そこで打ち切る）。"""
        taken: List[str] = []
        for text in texts:
            self.used += len(text)
            if self.limit is not None and self.used > self.limit:
                raise CoverageTooLarge(
                    "作り置きする文言が長すぎます（{0} 文字を超えました。上限は {1} 文字）。"
                    "時報の文言（time_signal）・closing.extra_text・ひとこと（quotes.file）・"
                    "天気の文のうち、長くしたものを短くしてください".format(
                        _count_text(self.used), _count_text(self.limit)))
            taken.append(text)
        return taken


def _phrase_groups(config: Config, include_quotes: bool, max_phrases: Optional[int] = None,
                   max_chars: Optional[int] = None) -> List[Tuple[str, List[str]]]:
    """種類ごとの文言（整える前・重複あり）を、列挙の順に返す。

    ``max_phrases`` を渡すと、作る前の見積もりと、作ったあとの件数が上限を
    超えたとき ``CoverageTooLarge``（省略すれば確かめない）。``max_chars`` を渡すと、
    文言の文字数の合計についても同じ（作る前の見積もりと、作りながらの合計）。
    """
    if max_phrases is not None:
        _check_size_before(config, max_phrases)
    if max_chars is not None:
        _check_chars_before(config, max_chars)
    chars = _Chars(max_chars)
    groups = [("announce", chars.take(announcement_phrases(config))),
              ("closing", chars.take(closing_phrases(config)))]
    if include_quotes:
        groups.append(("quote", chars.take(quote_phrases(config))))
    groups.append(("weather", chars.take(weather.prerecord_phrases(config.section("weather")))))
    if max_phrases is not None:
        count = sum(len(texts) for _, texts in groups)
        if count > max_phrases:
            raise CoverageTooLarge(
                "作り置きする文言が多すぎます（{0} 件。上限は {1} 件）。weather.prerecord・"
                "weather.open_meteo.locations・ひとこと（quotes.file）・時報の時刻の数を"
                "減らしてください".format(count, max_phrases))
    return groups


def _kind_by_phrase(config: Config, include_quotes: bool, max_phrases: Optional[int] = None,
                    max_chars: Optional[int] = None) -> Dict[str, str]:
    """文言 → 種類（列挙の順）。同じ文言が複数の種類にあれば、最初の種類に数える。"""
    kinds: Dict[str, str] = {}
    for kind, texts in _phrase_groups(config, include_quotes, max_phrases, max_chars):
        for text in _unique(texts):
            kinds.setdefault(text, kind)
    return kinds


def collect_phrases(config: Config, include_quotes: bool) -> List[str]:
    """事前生成する文言を集める。

    天気予報の文言（``chime.weather.prerecord_phrases``）は
    ``include_quotes`` の指定に関わらず常に含める。天気だけ作り置きが
    無いと、Pi 上で天気の文だけ無音になってしまうため。
    ``weather.enabled`` が False の場合も同様に含める
    （あとで有効化したときに作り置きが無くて困るより、常に列挙しておく
    ほうが安全という判断）。

    文言は実行時の照合と同じく前後の空白を落とし、空になるものは除く。
    """
    return list(_kind_by_phrase(config, include_quotes))


@dataclass(frozen=True)
class Coverage:
    """作り置きの声が揃っているかの集計（:func:`coverage` の結果）。"""

    #: 列挙した文言の数（``collect_phrases`` の件数と同じ）。
    total: int
    #: 作り置きの声が無い文言（列挙の順）。
    missing: List[str]
    #: 種類（``PHRASE_KINDS``）→ ``(声がある件数, 全件数)``。
    by_kind: Dict[str, Tuple[int, int]]

    @property
    def ok(self) -> bool:
        """声の無い文言が 1 件も無ければ True。"""
        return not self.missing


def coverage(config: Config, lookup: Callable[[str], Optional[str]],
             include_quotes: bool = True,
             max_phrases: Optional[int] = MAX_COVERAGE_PHRASES,
             max_chars: Optional[int] = MAX_COVERAGE_CHARS) -> Coverage:
    """:func:`collect_phrases` が挙げる文言のうち、作り置きの声がある数・無い文言を数える。

    ``lookup`` は文言から WAV のパスを返す（無ければ ``None``）。呼び出し側は
    ``TTSService.prerecorded_lookup`` を渡す。VOICEVOX には問い合わせないので、
    Pi 上でも使える。同じ文言が複数の種類にあるときは、最初の種類に数える
    （種類ごとの件数の合計が ``total`` と一致する）。

    起動のたびに呼ばれるので、手間は ``max_phrases`` 件・``max_chars`` 文字分までにする。
    設定の値（気温の幅など）で文言の数がそれを超えるときや、1 件が巨大なとき（地点の
    ``label`` が長い、書式指定の幅が :data:`MAX_FORMAT_SPEC` を超える）は、列挙せずに
    :class:`CoverageTooLarge` を出す（気温の幅は :data:`MAX_TEMP_SPAN` 度分まで）。
    それぞれ ``None`` なら確かめない。ふつうの設定（既定は 138 件・約 4,000 文字）の結果は
    変わらない。
    """
    available = dict.fromkeys(PHRASE_KINDS, 0)
    totals = dict.fromkeys(PHRASE_KINDS, 0)
    missing: List[str] = []
    for text, kind in _kind_by_phrase(config, include_quotes, max_phrases, max_chars).items():
        totals[kind] += 1
        if lookup(text):
            available[kind] += 1
        else:
            missing.append(text)
    return Coverage(total=sum(totals.values()), missing=missing,
                    by_kind={kind: (available[kind], totals[kind]) for kind in PHRASE_KINDS})


def phrases_in_use(config: Config) -> List[str]:
    """現在使われている全文言（ひとことを含む）を返す。

    ``--prune`` の判定に使う。ひとことを今回作り直さない
    （``--include-quotes`` を付けない）場合でも、ひとことは使われているので、
    その音声を消してはならない。
    """
    return collect_phrases(config, include_quotes=True)


def _default_config() -> Config:
    """既定設定だけの :class:`Config`（現地の ``config.json`` は読まない）。"""
    return Config(DEFAULT_CONFIG, base_dir=BASE_DIR)


def phrases_to_generate(config: Config, include_quotes: bool) -> List[str]:
    """生成する文言。``config`` の文言に、既定設定の文言を足したもの（和集合）。

    ``config.json`` の配列は既定値を丸ごと置き換える（地点に京都だけを書くと
    既定の大津が外れる）。``--config`` で渡した設定の文言だけを作ると、
    同梱の作り置きにある文言が抜けてしまうため、既定設定の文言は常に含める。
    順序は ``config`` の文言が先で、重複は除く。
    """
    return _unique(collect_phrases(config, include_quotes)
                   + collect_phrases(_default_config(), include_quotes))


def phrases_to_keep(config: Config) -> List[str]:
    """``--prune`` で残す文言。ひとことを含めて生成する文言と同じ（和集合）。"""
    return phrases_to_generate(config, include_quotes=True)


def find_stale_entries(manifest: Mapping[str, str],
                       keep_phrases: Iterable[str]) -> List[Tuple[str, str]]:
    """``manifest`` のうち、``keep_phrases`` に含まれないエントリを列挙する。

    実際の削除は行わない（呼び出し側が ``--prune`` のときだけ削除に使う）。
    削除前に「何が消えるか」を確認できるよう、判定と実行を分けている。
    """
    keep = set(keep_phrases)
    return [(phrase, filename) for phrase, filename in manifest.items() if phrase not in keep]
