"""再生状態の永続化。

``systemd`` の ``Restart=always`` によりプロセスが再起動しても二重再生しないよう、
「いつ・どのイベントを再生したか」をディスクに保存する。
直近に使った「ひとこと」も併せて記録し、連続で同じ文言が出るのを防ぐ。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .jsonfile import JsonFileError, read_json, write_json_atomic

logger = logging.getLogger(__name__)

MAX_RECENT_QUOTES = 32


class State:
    """``cache/state.json`` の読み書き。

    ``read_only=True`` のときは読み込むだけで、ファイルを作らず書かない
    （記録はメモリ上だけに残る）。動作確認の実行で、実機の記録を
    書き換えないために使う。
    """

    def __init__(self, path: str, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        self._data: Dict[str, Any] = {"last_fired": {}, "recent_quotes": []}
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        try:
            loaded = read_json(self.path)
        except JsonFileError as exc:
            if exc.kind != "missing":
                logger.warning("状態ファイルを読めません（初期化します）: %s", exc)
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
        if self.read_only:
            return
        try:
            write_json_atomic(self.path, self._data)
        except OSError as exc:
            logger.error("状態ファイルを保存できません: %s: %s", self.path, exc)

    # -- 再生済み判定 ---------------------------------------------------
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
