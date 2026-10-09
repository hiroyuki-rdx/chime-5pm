"""時報のあとに流す「ひとこと」の管理。

``assets/quotes.json`` から読み込み、直近に使ったものを避けながら 1 つ選ぶ。
時刻専用のひとこと（お昼、夕方など）があればそちらを優先候補に加える。
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .jsonfile import JsonFileError, read_json

logger = logging.getLogger(__name__)

# 予備も読み上げ音声は作り置きから引く。assets/voice/ に作り置きのある文
# （assets/quotes.json と同じ文）でなければ無音になる。
FALLBACK_QUOTES: List[str] = [
    "今日も一日、おつかれさまなのだ。",
    "こまめな休憩が、集中力への近道なのだ。",
    "深呼吸をひとつ。肩の力を抜いてみるのだ。",
]


class QuoteError(RuntimeError):
    """ひとことを選べなかった場合に送出する。"""


def _fallback_quotes() -> Dict[str, Any]:
    """内蔵の予備だけの定義を返す。呼び出しごとに別のリストを作る（共有しない）。"""
    return {"general": list(FALLBACK_QUOTES), "by_hour": {}}


def load_quotes(path: str) -> Dict[str, Any]:
    """ひとこと定義ファイルを読み込む。

    形式::

        {
          "general": ["...", "..."],
          "by_hour": {"12": ["お昼の一言"], "16": ["夕方の一言"]}
        }
    """
    try:
        data = read_json(path)
    except JsonFileError as exc:
        if exc.kind == "missing":
            logger.warning("ひとことファイルが見つかりません: %s（内蔵の予備を使います）", path)
        else:
            logger.error("ひとことファイルを読めません（内蔵の予備を使います）: %s", exc)
        return _fallback_quotes()
    return _normalize_quotes(data, path)


def _normalize_quotes(data: Any, path: str) -> Dict[str, Any]:
    """読み込んだ JSON を ``{"general": [...], "by_hour": {...}}`` に整える。

    ファイルは読まない（``path`` はログ用）。形式が不正な部分は警告を出して
    捨て、1 件も使えなければ内蔵の予備を返す。
    """
    if isinstance(data, list):
        data = {"general": data, "by_hour": {}}
    if not isinstance(data, Mapping):
        logger.error("ひとことファイルの形式が不正です: %s", path)
        return _fallback_quotes()

    general_raw = data.get("general", [])
    if isinstance(general_raw, list):
        general = [str(item) for item in general_raw]
    else:
        if general_raw:
            logger.warning("ひとことファイルの general が配列ではありません: %s", path)
        general = []

    by_hour_raw = data.get("by_hour", {}) or {}
    by_hour: Dict[str, List[str]] = {}
    if isinstance(by_hour_raw, Mapping):
        for key, values in by_hour_raw.items():
            if isinstance(values, list):
                by_hour[str(key)] = [str(item) for item in values]
            elif values:
                logger.warning("ひとことファイルの by_hour[%s] が配列ではありません: %s", key, path)

    if not general and not by_hour:
        logger.warning("ひとことが 1 件も定義されていません: %s", path)
        return _fallback_quotes()
    return {"general": general, "by_hour": by_hour}


class QuotePicker:
    """ひとことを選ぶ。直近に使ったものは避ける。"""

    def __init__(self, path: str, avoid_recent: int = 8,
                 rng: Optional[random.Random] = None) -> None:
        self.path = path
        self.avoid_recent = max(0, int(avoid_recent))
        self.rng = rng or random.Random()
        self._data = load_quotes(path)

    def candidates(self, hour: Optional[int] = None) -> List[str]:
        """対象時刻で使えるひとことの一覧を返す。"""
        quotes: List[str] = list(self._data.get("general", []))
        if hour is not None:
            quotes.extend(self._data.get("by_hour", {}).get(str(int(hour)), []))
        # 空文字列と重複を除きつつ順序を保つ（dict は挿入順を保つ）
        return list(dict.fromkeys(quote for quote in quotes if quote))

    def pick(self, hour: Optional[int] = None,
             recent: Sequence[str] = ()) -> str:
        """ひとことを 1 つ選ぶ。"""
        quotes = self.candidates(hour)
        if not quotes:
            raise QuoteError("選べるひとことがありません: {0}".format(self.path))

        blocked = set(list(recent)[-self.avoid_recent:]) if self.avoid_recent else set()
        fresh = [quote for quote in quotes if quote not in blocked]
        return self.rng.choice(fresh or quotes)
