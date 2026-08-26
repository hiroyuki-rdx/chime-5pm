"""時報のあとに流す「ひとこと」の管理。

``assets/quotes.json`` から読み込み、直近に使ったものを避けながら 1 つ選ぶ。
時刻専用のひとこと（お昼、夕方など）があればそちらを優先候補に加える。
"""

from __future__ import annotations

import json
import logging
import os
import random
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

FALLBACK_QUOTES: List[str] = [
    "今日も一日、おつかれさまです。",
    "こまめな休憩が、集中力の近道です。",
    "水分補給を忘れずに。",
]


class QuoteError(RuntimeError):
    """ひとことを選べなかった場合に送出する。"""


def load_quotes(path: str) -> Dict[str, Any]:
    """ひとこと定義ファイルを読み込む。

    形式::

        {
          "general": ["...", "..."],
          "by_hour": {"12": ["お昼の一言"], "16": ["夕方の一言"]}
        }
    """
    if not os.path.exists(path):
        logger.warning("ひとことファイルが見つかりません: %s（内蔵の予備を使います）", path)
        return {"general": list(FALLBACK_QUOTES), "by_hour": {}}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("ひとことファイルを読めません: %s: %s", path, exc)
        return {"general": list(FALLBACK_QUOTES), "by_hour": {}}

    if isinstance(data, list):
        data = {"general": data, "by_hour": {}}
    if not isinstance(data, Mapping):
        logger.error("ひとことファイルの形式が不正です: %s", path)
        return {"general": list(FALLBACK_QUOTES), "by_hour": {}}

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
        general = list(FALLBACK_QUOTES)
    return {"general": general, "by_hour": by_hour}


class QuotePicker:
    """ひとことを選ぶ。直近に使ったものは避ける。"""

    def __init__(self, path: str, avoid_recent: int = 8,
                 rng: Optional[random.Random] = None) -> None:
        self.path = path
        self.avoid_recent = max(0, int(avoid_recent))
        self.rng = rng or random.Random()
        self._data = load_quotes(path)

    def reload(self) -> None:
        self._data = load_quotes(self.path)

    def candidates(self, hour: Optional[int] = None) -> List[str]:
        """対象時刻で使えるひとことの一覧を返す。"""
        quotes: List[str] = list(self._data.get("general", []))
        if hour is not None:
            quotes.extend(self._data.get("by_hour", {}).get(str(int(hour)), []))
        # 重複を除きつつ順序を保つ
        seen = set()
        unique: List[str] = []
        for quote in quotes:
            if quote and quote not in seen:
                seen.add(quote)
                unique.append(quote)
        return unique

    def pick(self, hour: Optional[int] = None,
             recent: Sequence[str] = ()) -> str:
        """ひとことを 1 つ選ぶ。"""
        quotes = self.candidates(hour)
        if not quotes:
            raise QuoteError("選べるひとことがありません: {0}".format(self.path))

        blocked = set(list(recent)[-self.avoid_recent:]) if self.avoid_recent else set()
        fresh = [quote for quote in quotes if quote not in blocked]
        return self.rng.choice(fresh or quotes)
