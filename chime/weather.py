"""天気予報の取得と読み上げ文の組み立て。

時報のあとに流す「おまけ」用。提供元は Open-Meteo
（``https://api.open-meteo.com/``）だけで、API キーは不要。緯度経度で地点を
指定できるため、国外や細かい地点でも使える。

v6.0.0 で気象庁（JMA）の提供元を削除した。気象庁の予報文は自由文で語彙が閉じず、
作り置き（事前生成）できない。実行時の音声合成を持たない Pi ではその文が
無音になり、天気が流れなかったため。Open-Meteo は WMO 天気コードから文を
組み立てるので語彙が有限に閉じ、全パターンを作り置きできる。

失敗しうる前提で、例外は :class:`WeatherError` に正規化する。
放送本体（時報・蛍の光）は天気取得の成否に依存しない。
"""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

OPEN_METEO_ENDPOINT = "https://api.open-meteo.com/v1/forecast"

USER_AGENT = "campus-chime/3.0 (+https://github.com/hiroyuki-rdx/chime-5pm)"

#: WMO 天気コード → 日本語
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
    """JSON を取得する。失敗は :class:`WeatherError` に正規化する。

    ``http.client.IncompleteRead``・``BadStatusLine`` などの ``HTTPException``
    は ``OSError`` ではないため、個別に受けないと呼び出し側まで素通りする。
    """
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise WeatherError("天気 API が HTTP {0} を返しました: {1}".format(exc.code, url)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise WeatherError("天気 API へ接続できません: {0}".format(exc)) from exc
    except (http.client.HTTPException, ValueError) as exc:
        raise WeatherError("天気 API の応答を受け取れません: {0}: {1}".format(
            type(exc).__name__, exc)) from exc

    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeatherError("天気 API の応答を解釈できません: {0}".format(exc)) from exc


def _relative_label(target: date, today: date) -> str:
    delta = (target - today).days
    return {0: "今日", 1: "明日", 2: "明後日"}.get(delta, "{0}月{1}日".format(target.month, target.day))


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


def _format_weather_sentence(template: str, when: Any, label: Any, weather: Any) -> str:
    # {weather} は WMO_CODES の語（最長 10 文字）に限られるため、長さの上限や
    # 切り詰めは持たない。
    return template.format(when=when, label=label, weather=weather)


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
      ならないことは parse_open_meteo 側で保証済み）。
    - ``parts["temp"]`` / ``parts["temp_max"]`` / ``parts["pop"]`` が
      ``None`` なら、対応する文は出さない。
    - テンプレートが空文字列（``""``）ならその文は出さない
      （放送を短くしたい利用者向け。既定では ``sentence_temp_max`` /
      ``sentence_pop`` が空文字列で、現況の天気・気温だけを読む）。
    - ``pop`` は ``prerecord.pop_step`` の刻みに丸めてから埋め込む。
      ``parts["pop"]`` 自体は生値のまま変更しない。
    """
    sentences: List[str] = []
    pop_step = settings.get("prerecord", {}).get("pop_step")

    weather_template = str(settings.get("sentence_weather", ""))
    if weather_template:
        sentences.append(_format_weather_sentence(
            weather_template, parts.get("when", ""), parts.get("label", ""),
            parts.get("weather", "")))

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

    weather_template = str(settings.get("sentence_weather", ""))
    if weather_template:
        locations = settings.get("open_meteo", {}).get("locations", []) or []
        whens = list(prerecord.get("whens", []) or [])
        for location in locations:
            label = str(location.get("label", ""))
            for when in whens:
                for code in sorted(WMO_CODES):
                    _add(_format_weather_sentence(
                        weather_template, when, label, WMO_CODES[code]))

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


class WeatherService:
    """天気予報の取得・整形・キャッシュ（Open-Meteo 専用）。"""

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self.settings = dict(settings)
        # ``--weather`` の表示用の定数。設定の ``provider`` は読まない。v6.0.0 で
        # 廃止した旧設定（provider="jma" など）が config.json に残っていても、
        # 取得先は変わらない。
        self.provider = "open_meteo"
        self.timeout = float(self.settings.get("timeout_seconds", 8.0))
        self.cache_seconds = float(self.settings.get("cache_minutes", 60)) * 60.0
        # 地点ごとにキャッシュを持つ（大津のキャッシュが京都に流用されないように）。
        # キーは地点の label。
        self._cache: Dict[str, Tuple[float, date, List[str]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def url(self, location: Optional[Mapping[str, Any]] = None) -> str:
        """Open-Meteo の URL を返す。

        複数地点の一括クエリは応答形式が変わる（配列になる）ため使わず、地点
        ごとに個別の URL を組み立てる。``location`` に
        ``{"latitude": ..., "longitude": ...}`` を渡すとその地点の URL を、
        省略すると ``open_meteo.locations`` の先頭地点の URL を返す
        （地点を特定しない呼び出し元、例えば ``--weather`` CLI の URL 表示のため）。
        """
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

    def _targets(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """``(キャッシュキー, URL, parse 用設定)`` のリストを返す。

        ``open_meteo.locations`` の各要素ごとに個別の URL を組み立てる。
        """
        locations = self.settings.get("open_meteo", {}).get("locations", []) or []
        targets: List[Tuple[str, str, Dict[str, Any]]] = []
        for location in locations:
            label = str(location.get("label", ""))
            targets.append((label, self.url(location), {"label": label}))
        return targets

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
        try:
            parts = parse_open_meteo(payload, parse_settings, today)
            sentences = build_sentences(parts, self.settings)
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
            # 応答の想定外の形や、sentence_* テンプレートの書き間違い（未知の
            # 置換名など）。1 地点の失敗として扱い、他の地点を巻き込まない。
            raise WeatherError("天気予報の解析または文の組み立てに失敗しました（{0}）: {1}: {2}".format(
                cache_key, type(exc).__name__, exc)) from exc
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
        ``False`` の場合も :class:`WeatherError` を送出する。
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
