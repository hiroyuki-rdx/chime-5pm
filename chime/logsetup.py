"""ログの設定（systemd の journal に重大度を伝える）。

systemd は標準出力の各行を、接頭辞が無ければすべて info として記録する。
そのままでは ``journalctl -p err`` が常に空になる。行頭に ``<3>`` のような
sd-daemon の接頭辞を付けると、その行の重大度として記録される。

接頭辞は標準出力が journal に繋がっているときだけ付ける。ターミナルや CI
の出力に ``<6>`` が混ざらないようにするためで、判定は systemd が渡す
環境変数 ``JOURNAL_STREAM``（``デバイス番号:inode 番号``）で行う。
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import IO, Optional

#: 自前のハンドラに付ける目印（2 回目以降の呼び出しで見つけて更新する）。
_HANDLER_MARK = "_chime_handler"


def priority_prefix(levelno: int) -> str:
    """ログレベルに対応する sd-daemon の接頭辞（``<3>`` など）を返す。"""
    if levelno >= logging.CRITICAL:
        return "<2>"
    if levelno >= logging.ERROR:
        return "<3>"
    if levelno >= logging.WARNING:
        return "<4>"
    if levelno >= logging.INFO:
        return "<6>"
    return "<7>"


class JournalPriorityFormatter(logging.Formatter):
    """整形後の各行の先頭に、journal 用の重大度の接頭辞を付ける。

    複数行のメッセージや traceback も、systemd は 1 行ずつ別のエントリとして
    記録する。先頭行だけに付けると、残りの行が info になってしまうため、
    すべての行に付ける。
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        prefix = priority_prefix(record.levelno)
        return "\n".join(prefix + line for line in text.split("\n"))


def journal_stream_matches(stream: Optional[IO[str]]) -> bool:
    """``stream`` が systemd の journal に繋がっているかを返す。

    環境変数 ``JOURNAL_STREAM`` の ``デバイス番号:inode 番号`` が、``stream``
    の ``os.fstat`` の結果と一致するときだけ ``True``。ファイル記述子が
    無い（``io.StringIO`` など）、環境変数が無い・読めない場合は ``False``。
    """
    value = os.environ.get("JOURNAL_STREAM", "")
    device, _, inode = value.partition(":")
    try:
        stat = os.fstat(stream.fileno())
        return stat.st_dev == int(device) and stat.st_ino == int(inode)
    except Exception:
        return False


def _own_handler(root: logging.Logger) -> Optional[logging.StreamHandler]:
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARK, False):
            return handler
    return None


def setup_logging(level_name: str, log_format: str, timezone: str = "",
                  stream: Optional[IO[str]] = None) -> None:
    """ログ設定。タイムスタンプは設定したタイムゾーンで表示する。

    出力先は ``stream``（省略時は標準出力）。2 回目以降の呼び出しでは、
    最初に作った自前のハンドラのレベルと書式を更新する（重複させない）。
    ルートロガーに自前以外のハンドラが既にあるときは、ハンドラを追加しない
    （``logging.basicConfig`` と同じ）。
    """
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    root = logging.getLogger()

    handler = _own_handler(root)
    if handler is None and not root.handlers:
        handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    if handler is not None:
        formatter_class = (JournalPriorityFormatter
                           if journal_stream_matches(handler.stream) else logging.Formatter)
        handler.setLevel(level)
        handler.setFormatter(formatter_class(log_format))
        root.setLevel(level)

    if not timezone:
        return
    try:
        from zoneinfo import ZoneInfo

        tzinfo = ZoneInfo(timezone)
    except Exception:  # pragma: no cover - tzdata 欠落時は OS のローカル時刻のまま
        return

    def _converter(timestamp):
        return datetime.fromtimestamp(timestamp, tzinfo).timetuple()

    for existing in root.handlers:
        if existing.formatter is not None:
            # インスタンス属性として差し替える（クラス属性だと self が渡ってしまう）
            existing.formatter.converter = _converter


def emit_early_error(text: str) -> None:
    """ログ設定の前に起きた致命的なエラーを、標準エラー出力へ出す。

    標準エラー出力が journal に繋がっているときは、``journalctl -p err``
    で拾えるよう、各行に error の接頭辞を付ける。
    """
    lines = str(text).split("\n")
    if journal_stream_matches(sys.stderr):
        prefix = priority_prefix(logging.ERROR)
        lines = [prefix + line for line in lines]
    print("\n".join(lines), file=sys.stderr)
