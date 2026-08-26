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
    （例: ``"晴れ　時々　くもり"``）。これを単純に削除すると、Open JTalk の
    形態素解析（MeCab）が正しく区切れなくなり、読み上げが崩壊する
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
    return text[:cut].rstrip("、")


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
    """Open-Meteo JSON から読み上げに必要な要素を抜き出す。"""
    if not isinstance(payload, Mapping):
        raise WeatherError("Open-Meteo の応答形式が想定外です。")
    daily = payload.get("daily")
    if not isinstance(daily, Mapping) or not daily.get("time"):
        raise WeatherError("Open-Meteo の応答に日別予報が含まれていません。")

    def _at(key: str) -> Any:
        values = daily.get(key)
        if isinstance(values, list) and values:
            return values[0]
        return None

    raw_date = str(daily["time"][0])
    try:
        target_date = date.fromisoformat(raw_date)
    except ValueError:
        target_date = today

    code = _at("weather_code")
    try:
        weather = WMO_CODES.get(int(code), "")
    except (TypeError, ValueError):
        weather = ""
    if not weather:
        raise WeatherError("Open-Meteo の天気コードを解釈できません: {0}".format(code))

    def _as_int(value: Any) -> Optional[int]:
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None

    return {
        "when": _relative_label(target_date, today),
        "label": str(settings.get("label", "")),
        "weather": weather,
        "temp_max": _as_int(_at("temperature_2m_max")),
        "temp_min": _as_int(_at("temperature_2m_min")),
        "pop": _as_int(_at("precipitation_probability_max")),
    }


def build_text(parts: Mapping[str, Any], settings: Mapping[str, Any]) -> str:
    """抜き出した要素から読み上げ文を組み立てる。"""
    details: List[str] = []
    if parts.get("temp_max") is not None:
        details.append("最高気温は{0}度".format(parts["temp_max"]))
    if parts.get("temp_min") is not None:
        details.append("最低気温は{0}度".format(parts["temp_min"]))
    if parts.get("pop") is not None:
        details.append("降水確率は{0}パーセント".format(parts["pop"]))

    separator = str(settings.get("details_separator", "、"))
    suffix = str(settings.get("suffix", "です。"))
    detail_text = separator.join(details) + suffix if details else ""

    # 予期しない長文の予報が来た場合の保険。読点の位置で切り詰める。
    weather_text = truncate_weather_text(
        str(parts.get("weather", "")), settings.get("max_weather_chars", 40))

    template = str(settings.get("template", "{when}の{label}の天気は、{weather}。{details}"))
    text = template.format(
        when=parts.get("when", ""),
        label=parts.get("label", ""),
        weather=weather_text,
        details=detail_text,
    )
    return text.strip()


class WeatherService:
    """天気予報の取得・整形・キャッシュ。"""

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self.settings = dict(settings)
        self.provider = str(self.settings.get("provider", "jma")).lower()
        self.timeout = float(self.settings.get("timeout_seconds", 8.0))
        self.cache_seconds = float(self.settings.get("cache_minutes", 60)) * 60.0
        self._cache: Optional[Tuple[float, date, str]] = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def url(self) -> str:
        """使用する API の URL を返す。"""
        if self.provider == "jma":
            area_code = str(self.settings.get("jma", {}).get("area_code", "130000"))
            return JMA_ENDPOINT.format(area_code=area_code)
        if self.provider == "open_meteo":
            section = self.settings.get("open_meteo", {})
            query = urllib.parse.urlencode({
                "latitude": section.get("latitude", 35.6895),
                "longitude": section.get("longitude", 139.6917),
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

    def describe(self, today: Optional[date] = None, use_cache: bool = True) -> str:
        """読み上げ用の天気予報テキストを返す。"""
        if not self.enabled:
            raise WeatherError("天気予報機能が無効化されています。")

        resolved_today = today or date.today()
        now = time.monotonic()
        if (use_cache and self._cache
                and self._cache[1] == resolved_today
                and now - self._cache[0] < self.cache_seconds):
            logger.debug("天気予報をキャッシュから取得しました。")
            return self._cache[2]

        payload = fetch_json(self.url(), self.timeout)
        parts = self.parse(payload, resolved_today)
        text = build_text(parts, self.settings)
        if not text:
            raise WeatherError("天気予報の読み上げ文を組み立てられませんでした。")

        self._cache = (now, resolved_today, text)
        return text
