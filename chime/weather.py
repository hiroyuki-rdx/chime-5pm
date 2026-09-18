"""天気予報の取得と読み上げ文の組み立て。

時報のあとに流す「おまけ」用。API キーの不要な 2 つの提供元に対応する。

``jma``
    気象庁の防災情報 JSON（``https://www.jma.go.jp/bosai/forecast/data/forecast/``）。
    日本語の予報文をそのまま使えるため既定値。
``open_meteo``
    Open-Meteo（``https://api.open-meteo.com/``）。緯度経度で指定でき、
    国外や細かい地点でも使える。

いずれも失敗しうる前提で、例外は :class:`WeatherError` に正規化する。
放送本体（時報・蛍の光）は天気取得の成否に依存しない。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

JMA_ENDPOINT = "https://www.jma.go.jp/bosai/forecast/data/forecast/{area_code}.json"
OPEN_METEO_ENDPOINT = "https://api.open-meteo.com/v1/forecast"

USER_AGENT = "campus-chime/3.0 (+https://github.com/hiroyuki-rdx/chime-5pm)"

#: WMO 天気コード → 日本語（Open-Meteo 用）
WMO_CODES: Dict[int, str] = {
    0: "快晴", 1: "おおむね晴れ", 2: "薄ぐもり", 3: "くもり",
    45: "霧", 48: "霧氷をともなう霧",
    51: "弱い霧雨", 53: "霧雨", 55: "強い霧雨",
    56: "弱い着氷性の霧雨", 57: "着氷性の霧雨",
    61: "弱い雨", 63: "雨", 65: "強い雨",
    66: "弱い着氷性の雨", 67: "着氷性の雨",
    71: "弱い雪", 73: "雪", 75: "強い雪", 77: "霧雪",
    80: "にわか雨", 81: "強いにわか雨", 82: "激しいにわか雨",
    85: "にわか雪", 86: "強いにわか雪",
    95: "雷雨", 96: "ひょうをともなう雷雨", 99: "激しい雷雨",
}


class WeatherError(RuntimeError):
    """天気予報を取得・解釈できなかった場合に送出する。"""


def fetch_json(url: str, timeout: float) -> Any:
    """JSON を取得する。失敗は :class:`WeatherError` に正規化する。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise WeatherError("天気 API が HTTP {0} を返しました: {1}".format(exc.code, url)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise WeatherError("天気 API へ接続できません: {0}".format(exc)) from exc

    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeatherError("天気 API の応答を解釈できません: {0}".format(exc)) from exc


def normalize_weather_text(text: str) -> str:
    """気象庁の予報文中の空白を読点「、」に変換する。

    気象庁の ``weathers`` は全角スペースが形態素の境界を表している
    （例: ``"晴れ　時々　くもり"``）。これを単純に削除すると、読み上げエンジンの
    形態素解析が正しく区切れなくなり、読み上げが崩壊する
    （「所により」を含む長文で実測 23 秒・読み崩れを確認済み）。
    スペースを削除するのではなく読点に置き換えることで、区切りを保ったまま
    自然な文にする。連続する空白は 1 つの読点にまとめ、前後の余分な読点は削る。
    """
    collapsed = re.sub(r"[ 　]+", "、", str(text).strip())
    return collapsed.strip("、")


def drop_after_markers(text: str, markers: Optional[Iterable[str]]) -> str:
    """``markers`` のいずれかが最初に現れる位置以降を切り捨てる。

    気象庁の予報文には「所により」のような地域限定の但し書きが続くことがあり、
    館内放送としては冗長かつ読み上げが長くなる原因になる。``markers`` が
    空（``None`` や ``[]``）なら何も切り捨てない。

    切り捨てた結果が空文字列になる場合（例: 予報文が「所により」で始まる）は、
    切り捨てを行わず元の文をそのまま返す。読み上げ文が「天気は、。」のように
    壊れるくらいなら、多少長い文のほうが害が小さい。
    """
    if not markers:
        return text
    cut = len(text)
    for marker in markers:
        marker = str(marker)
        if not marker:
            continue
        index = text.find(marker)
        if index != -1 and index < cut:
            cut = index
    result = text[:cut].rstrip("、")
    return result if result else text


def truncate_weather_text(text: str, max_chars: Any, separator: str = "、") -> str:
    """``max_chars`` 文字を超える場合、``separator`` の位置で切り詰める。

    予期しない長文の予報が来た場合の保険（NFR: 読み上げ時間の上限）。
    文の途中で不自然にぶつ切りにならないよう、必ず区切り記号の位置で切る。
    区切りが見つからない場合は切らない（中途半端な文を読み上げるより安全）。
    """
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 40
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text.rfind(separator, 0, limit)
    if cut <= 0:
        return text
    # cut > 0 が確定しているため、text[:cut] は必ず空文字列にならない
    # （drop_after_markers と同じ「切り詰めた結果が空にならない」方針を、
    # ここでは cut <= 0 のガードがそのまま満たしている）。
    return text[:cut]


def _relative_label(target: date, today: date) -> str:
    delta = (target - today).days
    return {0: "今日", 1: "明日", 2: "明後日"}.get(delta, "{0}月{1}日".format(target.month, target.day))


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _pick_area(areas: List[Mapping[str, Any]], area_name: str) -> Optional[Mapping[str, Any]]:
    """``area_name`` に前方一致する地域を選ぶ（無ければ先頭）。"""
    if not areas:
        return None
    if area_name:
        for area in areas:
            name = str(area.get("area", {}).get("name", ""))
            if name.startswith(area_name) or area_name.startswith(name):
                return area
    return areas[0]


def _series_by_key(time_series: List[Mapping[str, Any]], key: str) -> Optional[Mapping[str, Any]]:
    for series in time_series:
        for area in series.get("areas", []) or []:
            if key in area:
                return series
    return None


def parse_jma(payload: Any, settings: Mapping[str, Any], today: date) -> Dict[str, Any]:
    """気象庁 JSON から読み上げに必要な要素を抜き出す。"""
    if not isinstance(payload, list) or not payload:
        raise WeatherError("気象庁 API の応答形式が想定外です。")

    root = payload[0]
    if not isinstance(root, Mapping):
        raise WeatherError("気象庁 API の応答形式が想定外です。")

    time_series = [s for s in root.get("timeSeries", []) or [] if isinstance(s, Mapping)]
    area_name = str(settings.get("area_name", ""))
    # 気温の timeSeries は観測地点名（"大津" など）で、天気・降水確率の細分区域名
    # （"南部" など）とは体系が異なる。temp_area_name が空なら従来どおり
    # area_name を使う（後方互換）。
    temp_area_name = str(settings.get("temp_area_name", "")) or area_name

    weather_series = _series_by_key(time_series, "weathers")
    if weather_series is None:
        raise WeatherError("気象庁 API の応答に天気予報が含まれていません。")

    area = _pick_area(list(weather_series.get("areas", []) or []), area_name)
    if area is None:
        raise WeatherError("気象庁 API の応答に対象地域が含まれていません。")

    weathers = [str(w) for w in area.get("weathers", []) or []]
    time_defines = [_parse_iso(t) for t in weather_series.get("timeDefines", []) or []]
    if not weathers:
        raise WeatherError("気象庁 API の応答に天気予報が含まれていません。")

    index = 0
    for candidate, moment in enumerate(time_defines):
        if moment and moment.date() == today and candidate < len(weathers):
            index = candidate
            break

    target_date = today
    if index < len(time_defines) and time_defines[index]:
        target_date = time_defines[index].date()

    weather_text = normalize_weather_text(weathers[index])
    weather_text = drop_after_markers(weather_text, settings.get("drop_after", ["所により"]))
    if not weather_text:
        # 空文字列ガードはここ 1 箇所に集約する。予報文（正規化・切り捨て後）が
        # 空 = 実質的に天気情報が無いということなので、build_text で
        # 「今日の滋賀の天気は、。」のような壊れた文を組み立てさせるのではなく、
        # ここで WeatherError を送出する。呼び出し側（chime.sequence）はこれを
        # 受けて設計どおり「ひとこと」に切り替える。
        raise WeatherError("気象庁 API の応答に天気予報の本文が含まれていません。")

    result: Dict[str, Any] = {
        "when": _relative_label(target_date, today),
        "label": str(settings.get("label") or area.get("area", {}).get("name", "")),
        "weather": weather_text,
        "temp_max": None,
        "temp_min": None,
        "pop": None,
    }

    temps = _collect_jma_temps(time_series, temp_area_name)
    result["temp_min"], result["temp_max"] = temps.get(target_date, (None, None))
    result["pop"] = _collect_jma_pop(time_series, area_name, target_date)
    return result


def _collect_jma_temps(time_series: List[Mapping[str, Any]],
                       temp_area_name: str) -> Dict[date, Tuple[Optional[int], Optional[int]]]:
    """気温の時系列を日付ごとの (最低, 最高) に畳み込む。

    ``temp_area_name`` は気温の観測地点名（例: "大津"）。天気・降水確率の
    細分区域名（area_name）とは体系が異なるため、呼び出し側で解決した値を渡す。
    """
    series = _series_by_key(time_series, "temps")
    collected: Dict[date, Tuple[Optional[int], Optional[int]]] = {}
    if series is None:
        return collected

    area = _pick_area(list(series.get("areas", []) or []), temp_area_name)
    if area is None:
        return collected

    time_defines = [_parse_iso(t) for t in series.get("timeDefines", []) or []]
    for moment, raw in zip(time_defines, area.get("temps", []) or []):
        if moment is None:
            continue
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            continue
        low, high = collected.get(moment.date(), (None, None))
        # 気象庁の気温時系列は 00 時が最低気温、09 時が最高気温を表す。
        if moment.hour < 6:
            low = value if low is None else min(low, value)
        else:
            high = value if high is None else max(high, value)
        collected[moment.date()] = (low, high)
    return collected


def _collect_jma_pop(time_series: List[Mapping[str, Any]], area_name: str,
                     target_date: date) -> Optional[int]:
    """対象日の降水確率の最大値を返す。"""
    series = _series_by_key(time_series, "pops")
    if series is None:
        return None
    area = _pick_area(list(series.get("areas", []) or []), area_name)
    if area is None:
        return None

    time_defines = [_parse_iso(t) for t in series.get("timeDefines", []) or []]
    values: List[int] = []
    for moment, raw in zip(time_defines, area.get("pops", []) or []):
        if moment is None or moment.date() != target_date:
            continue
        try:
            values.append(int(float(raw)))
        except (TypeError, ValueError):
            continue
    return max(values) if values else None


def parse_open_meteo(payload: Any, settings: Mapping[str, Any], today: date) -> Dict[str, Any]:
    """Open-Meteo JSON から読み上げに必要な要素を抜き出す。

    天気・気温は ``current``（現況）から取る。時報で知りたいのは「今」の
    天気・気温であり、``daily`` の値はその日 1 日ぶんの予報であって現況ではない
    ため。``daily`` は ``sentence_temp_max`` / ``sentence_pop`` を設定で
    有効にした場合の opt-in 用に残しており、無くても現況の天気・気温は
    組み立てられる（temp_max / temp_min / pop は ``None`` になるだけ）。
    """
    if not isinstance(payload, Mapping):
        raise WeatherError("Open-Meteo の応答形式が想定外です。")

    current = payload.get("current")
    if not isinstance(current, Mapping):
        raise WeatherError("Open-Meteo の応答に現況（current）が含まれていません。")

    code = current.get("weather_code")
    try:
        weather = WMO_CODES.get(int(code), "")
    except (TypeError, ValueError):
        weather = ""
    if not weather:
        raise WeatherError("Open-Meteo の現況の天気コードを解釈できません: {0}".format(code))

    def _as_int(value: Any) -> Optional[int]:
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None

    daily = payload.get("daily")
    if not isinstance(daily, Mapping):
        daily = {}

    def _at(key: str) -> Any:
        values = daily.get(key)
        if isinstance(values, list) and values:
            return values[0]
        return None

    target_date = today
    raw_date = _at("time")
    if raw_date is not None:
        try:
            target_date = date.fromisoformat(str(raw_date))
        except ValueError:
            target_date = today

    return {
        "when": _relative_label(target_date, today),
        "label": str(settings.get("label", "")),
        "weather": weather,
        "temp": _as_int(current.get("temperature_2m")),
        "temp_max": _as_int(_at("temperature_2m_max")),
        "temp_min": _as_int(_at("temperature_2m_min")),
        "pop": _as_int(_at("precipitation_probability_max")),
    }


def _round_pop_to_step(pop: Any, pop_step: Any) -> Any:
    """降水確率を ``pop_step`` の刻みに丸める。

    ``pop_step`` が正の整数に変換できない場合は丸めずにそのまま返す
    （呼び出し側の ``parts["pop"]`` 自体は変更しない方針とも一致する）。
    Python 組み込みの :func:`round` は偶数丸め（銀行丸め）のため、
    ちょうど中間の値（例: 25）は偶数側の刻み（20）に丸められる。
    """
    try:
        step = int(pop_step)
    except (TypeError, ValueError):
        return pop
    if step <= 0:
        return pop
    return int(round(pop / step)) * step


def _format_weather_sentence(template: str, when: Any, label: Any, weather: Any,
                             max_chars: Any) -> str:
    # 予期しない長文の予報が来た場合の保険。読点の位置で切り詰める。
    weather_text = truncate_weather_text(str(weather), max_chars)
    return template.format(when=when, label=label, weather=weather_text)


def _format_temp_sentence(template: str, temp: Any) -> str:
    return template.format(temp=temp)


def _format_temp_max_sentence(template: str, temp_max: Any) -> str:
    return template.format(temp_max=temp_max)


def _format_pop_sentence(template: str, pop: Any, pop_step: Any) -> str:
    return template.format(pop=_round_pop_to_step(pop, pop_step))


def build_sentences(parts: Mapping[str, Any], settings: Mapping[str, Any]) -> List[str]:
    """1 地点ぶんの ``parts`` から読み上げ文のリストを返す（1〜4 文）。

    地名・気温・最高気温・降水確率を別々の完全な文に分けることで、各文の
    語彙が有限に閉じ、全パターンを VOICEVOX で作り置きできるようにする
    （1 文にまとめると組み合わせが爆発するため）。

    文の順序は 天気 → 気温（現況） → 最高気温 → 降水確率 で固定。

    - ``sentence_weather`` の文は常に出す（``parts["weather"]`` が空文字列に
      ならないことは parse_jma / parse_open_meteo 側で保証済み）。
    - ``parts["temp"]`` / ``parts["temp_max"]`` / ``parts["pop"]`` が
      ``None`` なら、対応する文は出さない。
    - テンプレートが空文字列（``""``）ならその文は出さない
      （放送を短くしたい利用者向け。既定では ``sentence_temp_max`` /
      ``sentence_pop`` が空文字列で、現況の天気・気温だけを読む）。
    - ``pop`` は ``prerecord.pop_step`` の刻みに丸めてから埋め込む。
      ``parts["pop"]`` 自体は生値のまま変更しない。
    """
    sentences: List[str] = []
    max_chars = settings.get("max_weather_chars", 40)
    pop_step = settings.get("prerecord", {}).get("pop_step")

    weather_template = str(settings.get("sentence_weather", ""))
    if weather_template:
        sentences.append(_format_weather_sentence(
            weather_template, parts.get("when", ""), parts.get("label", ""),
            parts.get("weather", ""), max_chars))

    temp = parts.get("temp")
    temp_template = str(settings.get("sentence_temp", ""))
    if temp is not None and temp_template:
        sentences.append(_format_temp_sentence(temp_template, temp))

    temp_max = parts.get("temp_max")
    temp_max_template = str(settings.get("sentence_temp_max", ""))
    if temp_max is not None and temp_max_template:
        sentences.append(_format_temp_max_sentence(temp_max_template, temp_max))

    pop = parts.get("pop")
    pop_template = str(settings.get("sentence_pop", ""))
    if pop is not None and pop_template:
        sentences.append(_format_pop_sentence(pop_template, pop, pop_step))

    return sentences


def prerecord_phrases(settings: Mapping[str, Any]) -> List[str]:
    """作り置きすべき天気の文言を全列挙して返す（重複なし・順序安定）。

    ``build_sentences`` と同じテンプレート・同じ丸め規則（``_format_*_sentence``
    ヘルパー）を使って文を組み立てる。2 箇所に書くと語彙がずれるため、
    文を作る処理はそのヘルパーに一本化している。

    - ``open_meteo.locations`` の各 ``label`` × ``prerecord.whens`` ×
      ``WMO_CODES`` の全値 → ``sentence_weather`` の文
    - ``prerecord.temp_min`` 〜 ``temp_max``（両端含む） → ``sentence_temp`` の文
      （現況の気温。既定で有効）
    - ``prerecord.temp_min`` 〜 ``temp_max``（両端含む） → ``sentence_temp_max`` の文
      （その日の最高気温。既定では空文字列で無効）
    - 0 〜 100 を ``prerecord.pop_step`` 刻み → ``sentence_pop`` の文
      （既定では空文字列で無効）
    - テンプレートが空文字列なら、その種類は列挙しない。
    """
    phrases: List[str] = []
    seen = set()

    def _add(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            phrases.append(text)

    prerecord = settings.get("prerecord", {}) or {}
    max_chars = settings.get("max_weather_chars", 40)

    weather_template = str(settings.get("sentence_weather", ""))
    if weather_template:
        locations = settings.get("open_meteo", {}).get("locations", []) or []
        whens = list(prerecord.get("whens", []) or [])
        for location in locations:
            label = str(location.get("label", ""))
            for when in whens:
                for code in sorted(WMO_CODES):
                    _add(_format_weather_sentence(
                        weather_template, when, label, WMO_CODES[code], max_chars))

    temp_template = str(settings.get("sentence_temp", ""))
    if temp_template:
        try:
            temp_min = int(prerecord.get("temp_min", 0))
            temp_max = int(prerecord.get("temp_max", 0))
        except (TypeError, ValueError):
            temp_min, temp_max = 0, -1  # 範囲が不正なら列挙しない
        for value in range(temp_min, temp_max + 1):
            _add(_format_temp_sentence(temp_template, value))

    temp_max_template = str(settings.get("sentence_temp_max", ""))
    if temp_max_template:
        try:
            temp_min = int(prerecord.get("temp_min", 0))
            temp_max = int(prerecord.get("temp_max", 0))
        except (TypeError, ValueError):
            temp_min, temp_max = 0, -1  # 範囲が不正なら列挙しない
        for value in range(temp_min, temp_max + 1):
            _add(_format_temp_max_sentence(temp_max_template, value))

    pop_template = str(settings.get("sentence_pop", ""))
    if pop_template:
        try:
            pop_step = int(prerecord.get("pop_step"))
        except (TypeError, ValueError):
            pop_step = 0
        if pop_step > 0:
            for value in range(0, 101, pop_step):
                _add(_format_pop_sentence(pop_template, value, pop_step))

    return phrases


def build_text(parts: Mapping[str, Any], settings: Mapping[str, Any]) -> str:
    """抜き出した要素から読み上げ文（1 本の文字列）を組み立てる。

    ``parts["weather"]`` が空文字列にならないことはここでは保証しない。
    空文字ガードは呼び出し側の parse_jma / parse_open_meteo に集約しており
    （空なら WeatherError を送出する）、ここで二重に防御はしない。

    文を作る処理自体は :func:`build_sentences` に一本化しており、ここでは
    それを連結するだけ（後方互換用。1 文ずつの再生には ``build_sentences``
    を使うこと）。
    """
    return "".join(build_sentences(parts, settings))


class WeatherService:
    """天気予報の取得・整形・キャッシュ。"""

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self.settings = dict(settings)
        self.provider = str(self.settings.get("provider", "jma")).lower()
        self.timeout = float(self.settings.get("timeout_seconds", 8.0))
        self.cache_seconds = float(self.settings.get("cache_minutes", 60)) * 60.0
        # 地点ごとにキャッシュを持つ（大津のキャッシュが京都に流用されないように）。
        # キーは jma なら固定の "jma"、open_meteo なら地点の label。
        self._cache: Dict[str, Tuple[float, date, List[str]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def url(self, location: Optional[Mapping[str, Any]] = None) -> str:
        """使用する API の URL を返す。

        ``open_meteo`` の複数地点一括クエリは応答形式が変わる（配列になる）
        ため使わず、地点ごとに個別の URL を組み立てる。``location`` に
        ``{"latitude": ..., "longitude": ...}`` を渡すとその地点の URL を、
        省略すると ``open_meteo.locations`` の先頭地点の URL を返す
        （地点を特定しない既存の呼び出し元、例えば ``--weather`` CLI の
        URL 表示との後方互換のため）。
        """
        if self.provider == "jma":
            area_code = str(self.settings.get("jma", {}).get("area_code", "130000"))
            return JMA_ENDPOINT.format(area_code=area_code)
        if self.provider == "open_meteo":
            if location is None:
                locations = self.settings.get("open_meteo", {}).get("locations", []) or []
                if not locations:
                    raise WeatherError("Open-Meteo の地点が設定されていません。")
                location = locations[0]
            query = urllib.parse.urlencode({
                "latitude": location.get("latitude"),
                "longitude": location.get("longitude"),
                # 現況（読み上げの本体）。daily は sentence_temp_max /
                # sentence_pop を有効にしたときの opt-in 用に残す。1 地点
                # 1 回の HTTP で両方が返るようにしておく。
                "current": "weather_code,temperature_2m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max",
                "timezone": "Asia/Tokyo",
                "forecast_days": 1,
            })
            return OPEN_METEO_ENDPOINT + "?" + query
        raise WeatherError("未知の天気提供元です: {0}".format(self.provider))

    def parse(self, payload: Any, today: date) -> Dict[str, Any]:
        if self.provider == "jma":
            return parse_jma(payload, self.settings.get("jma", {}), today)
        if self.provider == "open_meteo":
            return parse_open_meteo(payload, self.settings.get("open_meteo", {}), today)
        raise WeatherError("未知の天気提供元です: {0}".format(self.provider))

    def _targets(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """``(キャッシュキー, URL, parse 用設定)`` のリストを返す。

        jma は従来どおり単一地域のまま（後方互換）。open_meteo は
        ``open_meteo.locations`` の各要素ごとに個別の URL を組み立てる。
        """
        if self.provider == "jma":
            return [("jma", self.url(), dict(self.settings.get("jma", {})))]
        if self.provider == "open_meteo":
            locations = self.settings.get("open_meteo", {}).get("locations", []) or []
            targets: List[Tuple[str, str, Dict[str, Any]]] = []
            for location in locations:
                label = str(location.get("label", ""))
                targets.append((label, self.url(location), {"label": label}))
            return targets
        raise WeatherError("未知の天気提供元です: {0}".format(self.provider))

    def _parse_one(self, payload: Any, parse_settings: Mapping[str, Any],
                   today: date) -> Dict[str, Any]:
        if self.provider == "jma":
            return parse_jma(payload, parse_settings, today)
        if self.provider == "open_meteo":
            return parse_open_meteo(payload, parse_settings, today)
        raise WeatherError("未知の天気提供元です: {0}".format(self.provider))

    def _describe_one(self, cache_key: str, url: str, parse_settings: Mapping[str, Any],
                      today: date, use_cache: bool) -> List[str]:
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if (use_cache and cached
                and cached[1] == today
                and now - cached[0] < self.cache_seconds):
            logger.debug("天気予報をキャッシュから取得しました（%s）。", cache_key)
            return cached[2]

        payload = fetch_json(url, self.timeout)
        parts = self._parse_one(payload, parse_settings, today)
        sentences = build_sentences(parts, self.settings)
        if not sentences:
            raise WeatherError("天気予報の読み上げ文を組み立てられませんでした。")

        self._cache[cache_key] = (now, today, sentences)
        return sentences

    def describe_sentences(self, today: Optional[date] = None,
                           use_cache: bool = True) -> List[str]:
        """全地点ぶんの文を、地点の順に平坦なリストで返す。

        1 地点の取得に失敗しても、取得できた地点の文は返す（例えば大津だけ
        取れたら大津だけ読む）。失敗した地点は警告ログを出す。全地点が
        失敗したときだけ :class:`WeatherError` を送出する。``enabled`` が
        ``False`` の場合は従来どおり :class:`WeatherError` を送出する。
        """
        if not self.enabled:
            raise WeatherError("天気予報機能が無効化されています。")

        resolved_today = today or date.today()
        targets = self._targets()
        if not targets:
            raise WeatherError("天気予報の取得先が設定されていません。")

        sentences: List[str] = []
        failures = 0
        for cache_key, url, parse_settings in targets:
            try:
                sentences.extend(self._describe_one(
                    cache_key, url, parse_settings, resolved_today, use_cache))
            except WeatherError as exc:
                failures += 1
                logger.warning("天気予報を取得できませんでした（%s）: %s", cache_key, exc)

        if failures == len(targets):
            raise WeatherError("天気予報を取得できませんでした（全地点で失敗）。")
        return sentences

    def describe(self, today: Optional[date] = None, use_cache: bool = True) -> str:
        """読み上げ用の天気予報テキストを 1 本の文字列として返す（後方互換）。

        1 文ずつ独立した音声として再生したい場合は :meth:`describe_sentences`
        を使うこと。文を作る処理自体はそちらに一本化している。
        """
        return "".join(self.describe_sentences(today=today, use_cache=use_cache))
