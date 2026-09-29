"""JSON ファイルの読み書きの共通処理。

設定・状態・ひとこと・作り置きの目録は、利用者が Windows のメモ帳などで
手直しすることがある。そのとき起きやすい失敗（文字コードが Shift_JIS になる、
全角の引用符やカンマが混ざる、最後の項目のあとにカンマを付ける）を、
生の例外ではなく「どこを・どう直すか」が分かる日本語の案内にして返す。

読み込みは BOM 付きの UTF-8 も受け付ける（メモ帳の「UTF-8（BOM 付き）」）。
書き込みは一時ファイルに書いてから置き換えるので、途中で電源が落ちても
書きかけのファイルが残らない。
"""

from __future__ import annotations

import codecs
import json
import os
from typing import Any, List

#: 全角のまま混ざりやすい記号（引用符・カンマ・コロン・空白）。
_FULLWIDTH_MARKS = "“”‘’，：\u3000"


class JsonFileError(Exception):
    """JSON ファイルを読めなかった場合に送出する。

    ``str()`` は利用者向けの日本語の案内。``kind`` で原因を区別できる。

    ``missing``
        ファイルが無い。
    ``encoding``
        UTF-8 として読めない（Shift_JIS などで保存されている）。
    ``syntax``
        JSON の書き方が正しくない。
    ``io``
        それ以外の読み込みエラー（権限がない、ディレクトリだった など）。
    """

    def __init__(self, path: str, kind: str, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.kind = kind


def read_json(path: str) -> Any:
    """JSON ファイルを読み込んで返す。読めなければ :class:`JsonFileError`。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError as exc:
        raise JsonFileError(path, "missing", "{0} が見つかりません".format(path)) from exc
    except OSError as exc:
        raise JsonFileError(
            path, "io", "{0} を読めません: {1}".format(path, exc)) from exc

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # utf-8-sig の位置は BOM を除いた側から数えるので、ファイルの
        # 先頭からの位置に直す（1 始まり）。
        offset = exc.start + 1
        if raw.startswith(codecs.BOM_UTF8):
            offset += len(codecs.BOM_UTF8)
        raise JsonFileError(
            path, "encoding",
            "{0} を UTF-8 として読めません（{1} バイト目）。"
            "メモ帳などで保存するときは文字コードに『UTF-8』を選んでください"
            "（Shift_JIS／ANSI では読めません）。".format(path, offset)) from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        message = "{0} の {1} 行 {2} 文字目で JSON の書き方が正しくありません: {3}".format(
            path, exc.lineno, exc.colno, exc.msg)
        hints = _syntax_hints(text, exc)
        if hints:
            message += "（ヒント: {0}）".format("。".join(hints))
        raise JsonFileError(path, "syntax", message) from exc


def _syntax_hints(text: str, exc: json.JSONDecodeError) -> List[str]:
    """構文エラーの位置から、よくある原因のヒントを拾う（検出できる範囲で）。"""
    hints: List[str] = []

    lines = text.split("\n")
    if 1 <= exc.lineno <= len(lines):
        if any(mark in lines[exc.lineno - 1] for mark in _FULLWIDTH_MARKS):
            hints.append("全角の記号が混ざっていませんか")

    # 最後の項目のあとに付けたカンマ。Python 3.13 以降はそのことを msg で
    # 教えてくれる。それより前は、エラーの位置が閉じ括弧になり、その直前
    # （空白を除く）がカンマになる。
    trailing_comma = exc.msg.startswith("Illegal trailing comma") or (
        text[exc.pos:exc.pos + 1] in ("}", "]")
        and text[:exc.pos].rstrip().endswith(","))
    if trailing_comma:
        hints.append("最後の項目のあとにカンマは付けられません")
    return hints


def write_json_atomic(path: str, data: Any, sort_keys: bool = False) -> None:
    """``data`` を JSON として書き出す（一時ファイル経由で置き換える）。

    途中で失敗しても、元のファイルはそのまま残り、一時ファイルも残さない。
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = "{0}.{1}.tmp".format(path, os.getpid())
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=sort_keys)
            handle.write("\n")
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
