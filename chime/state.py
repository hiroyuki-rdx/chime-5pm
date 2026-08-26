"""再生状態の永続化。

``systemd`` の ``Restart=always`` によりプロセスが再起動しても二重再生しないよう、
「いつ・どのイベントを再生したか」をディスクに保存する。
直近に使った「ひとこと」も併せて記録し、連続で同じ文言が出るのを防ぐ。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_RECENT_QUOTES = 32


class State:
    """``cache/state.json`` の読み書き。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._data: Dict[str, Any] = {"last_fired": {}, "recent_quotes": []}
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("状態ファイルを読めません（初期化します）: %s: %s", self.path, exc)
            return
        if not isinstance(loaded, dict):
            logger.warning("状態ファイルの形式が不正です（初期化します）: %s", self.path)
            return

        last_fired = loaded.get("last_fired", {})
        recent = loaded.get("recent_quotes", [])
        self._data["last_fired"] = {
            str(key): str(value) for key, value in last_fired.items()
        } if isinstance(last_fired, dict) else {}
        self._data["recent_quotes"] = [str(item) for item in recent] if isinstance(recent, list) else []

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            temp_path = "{0}.{1}.tmp".format(self.path, os.getpid())
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_path, self.path)
        except OSError as exc:
            logger.error("状態ファイルを保存できません: %s: %s", self.path, exc)

    # -- 再生済み判定 ---------------------------------------------------
    def last_fired(self, key: str) -> Optional[str]:
        return self._data["last_fired"].get(key)

    def is_fired(self, key: str, day: str) -> bool:
        """``key`` のイベントが ``day``（YYYY-MM-DD）に再生済みかを返す。"""
        return self._data["last_fired"].get(key) == day

    def mark_fired(self, key: str, day: str) -> None:
        self._data["last_fired"][key] = day
        self.save()

    # -- ひとこと履歴 ---------------------------------------------------
    def recent_quotes(self) -> List[str]:
        return list(self._data["recent_quotes"])

    def remember_quote(self, quote: str) -> None:
        if not quote:
            return
        recent: List[str] = self._data["recent_quotes"]
        recent.append(quote)
        del recent[:-MAX_RECENT_QUOTES]
        self.save()
