"""放送の履歴（1 回の放送につき JSON を 1 行）。

``cache/history.jsonl`` に、いつ・どの放送が・どう終わったかを 1 行ずつ追記する。
``--status`` で直近の放送を見るためのもので、放送そのものには影響しない。
書けなくても放送は止めない（警告を残すだけ）。読むときも、壊れた行や
知らない版の行は黙って飛ばす。

行が増えすぎないよう、``max_lines`` を超えたら新しい ``keep_lines`` 行だけに
書き直す（一時ファイルに書いてから置き換えるので、途中で電源が落ちても
履歴を失わない）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

logger = logging.getLogger(__name__)

#: 行の形式の版（形式を変えたら上げる。読むときは知らない版の行を飛ばす）。
#: 項目を足すだけなら上げない（``missing`` がそう）。読む側は、足した項目の
#: 無い古い行も受け入れる。
HISTORY_VERSION = 1

#: これを超えたら古い行を捨てる。
MAX_LINES = 600
#: 捨てるときに残す（新しい側の）行数。
KEEP_LINES = 500


def make_entry(*, at: datetime, key: str, kind: str, played: int, total: int,
               silent: Sequence[str] = (), warnings: Sequence[str] = (),
               degraded: bool = False, error: Optional[str] = None,
               missing: Sequence[str] = ()) -> Dict[str, Any]:
    """1 回の放送の履歴 1 件（JSON にできる辞書）を作る。

    ``at`` は放送の日時（タイムゾーン付き）で、``day`` はその日付（YYYY-MM-DD）。
    ``played`` は鳴らせた部品の数、``total`` は鳴らすはずだった部品の数、
    ``silent`` は音にならなかった部品の説明。``missing`` は、音源ファイルが無くて
    組み立てのときに積めなかった必須の部品（時報音・閉館アナウンス・蛍の光）の
    名前。``total`` には数えられていないので、``played == total`` でも「すべて鳴らせた」
    とは言えない。``error`` は放送が例外で終わったときの説明で、渡されたときだけ
    ``"error"`` を付ける。

    ``missing`` は後から足した項目で、``HISTORY_VERSION`` は上げていない。
    読む側は、この項目の無い古い行も受け入れること。

    ``result`` は次のいずれか（上から順に判定する）。

    ``error``
        ``error`` が渡された（放送が例外で終わった）。
    ``failed``
        1 つも鳴らせなかった。
    ``partial``
        一部しか鳴らせなかった、または音にならなかった部品・積めなかった部品がある。
    ``ok``
        すべて鳴らせた。
    """
    silent_texts = _texts(silent)
    missing_texts = _texts(missing)
    entry: Dict[str, Any] = {
        "v": HISTORY_VERSION,
        "at": at.isoformat(timespec="seconds"),
        "day": at.date().isoformat(),
        "key": key,
        "kind": kind,
        "result": _result(played, total, silent_texts, missing_texts, error),
        "played": played,
        "total": total,
        "silent": silent_texts,
        "missing": missing_texts,
        "warnings": _texts(warnings),
        "degraded": bool(degraded),
    }
    if error is not None:
        entry["error"] = error
    return entry


def _texts(values: Union[str, Sequence[str]]) -> List[str]:
    """文字列の並びを新しいリストにする（文字列 1 つなら、1 件として扱う）。"""
    if isinstance(values, str):
        return [values]
    return [str(value) for value in values]


def _result(played: int, total: int, silent: Sequence[str], missing: Sequence[str],
            error: Optional[str]) -> str:
    """放送の結果（``error`` / ``failed`` / ``partial`` / ``ok``）を返す。"""
    if error is not None:
        return "error"
    if played == 0:
        return "failed"
    if played < total or silent or missing:
        return "partial"
    return "ok"


class History:
    """``cache/history.jsonl`` の追記と読み出し。

    どちらも例外を出さない。書けなかった（``append`` が ``False``）ときも
    読めなかったときも、警告をログに残して放送を続ける。
    """

    def __init__(self, path: str, max_lines: int = MAX_LINES,
                 keep_lines: int = KEEP_LINES) -> None:
        self.path = path
        self.max_lines = max(1, max_lines)
        # 残す行数が上限より多いと、追記のたびに書き直すことになるので上限までにする。
        self.keep_lines = max(1, min(keep_lines, self.max_lines))

    # -- 追記 -----------------------------------------------------------
    def append(self, entry: Mapping[str, Any]) -> bool:
        """``entry`` を 1 行で追記する。書けたら ``True``、書けなければ ``False``。

        ``False`` のときは WARNING を残す（例外は出さない）。行数が ``max_lines`` を
        超えたら、新しい ``keep_lines`` 行だけに書き直す。書き直しに失敗しても、
        追記はできているので ``True`` を返す。
        """
        try:
            data = self._encode(entry)
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            self._write(data)
        except (OSError, TypeError, ValueError, RecursionError) as exc:
            # UnicodeEncodeError は ValueError の子。深すぎる入れ子は RecursionError。
            logger.warning("放送の履歴を書けません（放送は続けます）: %s: %s", self.path, exc)
            return False
        self._trim()
        return True

    @staticmethod
    def _encode(entry: Mapping[str, Any]) -> bytes:
        """``entry`` を改行を含まない 1 行（UTF-8、末尾は改行）にする。"""
        if not isinstance(entry, Mapping):
            raise TypeError("履歴の 1 件は辞書で渡してください: {0!r}".format(type(entry)))
        line = json.dumps(entry, ensure_ascii=False, allow_nan=False)
        return (line + "\n").encode("utf-8")

    def _write(self, data: bytes) -> None:
        """末尾に ``data`` を足す。前の書き込みが途中で切れていたら、改行で区切る。"""
        with open(self.path, "a+b") as handle:
            handle.write(_line_break_if_torn(handle) + data)

    def _trim(self) -> None:
        """``max_lines`` を超えていたら、新しい ``keep_lines`` 行だけに書き直す。"""
        try:
            lines = self._read_lines()
            if len(lines) <= self.max_lines:
                return
            _replace_file(self.path, lines[-self.keep_lines:])
        except OSError as exc:
            logger.warning("放送の履歴を整理できません（古い行が残ります）: %s: %s",
                           self.path, exc)

    # -- 読み出し -------------------------------------------------------
    def recent(self, limit: int = 8) -> List[Dict[str, Any]]:
        """新しい順に、最大 ``limit`` 件を返す。

        空行・壊れた行・形が違う行・知らない版の行は飛ばす（件数にも数えない）。
        ファイルが無ければ空のリスト。読めないときは WARNING を残して空のリスト。
        """
        if limit <= 0:
            return []
        try:
            lines = self._read_lines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("放送の履歴を読めません: %s: %s", self.path, exc)
            return []

        entries: List[Dict[str, Any]] = []
        for line in reversed(lines):
            entry = _parse(line)
            if entry is not None:
                entries.append(entry)
                if len(entries) >= limit:
                    break
        return entries

    def _read_lines(self) -> List[bytes]:
        """空でない行を、古い順にバイト列のまま返す（文字コードはここでは見ない）。"""
        with open(self.path, "rb") as handle:
            raw = handle.read()
        # JSON の文字列の中の U+2028 などで行を割らないよう、改行（\n）だけで区切る。
        return [line for line in raw.split(b"\n") if line.strip()]


def _line_break_if_torn(handle: Any) -> bytes:
    """ファイルが改行で終わっていなければ ``b"\\n"``、そうでなければ空を返す。

    電源が落ちて最後の行が途中で切れていると、そのまま追記して新しい行が
    巻き込まれる。改行で区切っておけば、壊れるのは切れた 1 行だけで済む。
    """
    if handle.seek(0, os.SEEK_END) == 0:
        return b""
    handle.seek(-1, os.SEEK_END)
    return b"" if handle.read(1) == b"\n" else b"\n"


def _replace_file(path: str, lines: Sequence[bytes]) -> None:
    """``lines`` だけの内容にファイルを置き換える（一時ファイル経由）。

    途中で失敗しても、元のファイルはそのまま残り、一時ファイルも残さない。
    """
    temp_path = "{0}.{1}.tmp".format(path, os.getpid())
    try:
        with open(temp_path, "wb") as handle:
            handle.write(b"\n".join(lines) + b"\n")
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def _parse(line: bytes) -> Optional[Dict[str, Any]]:
    """1 行を履歴の 1 件にする。読めない・形が違う・知らない版なら ``None``。"""
    try:
        entry = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):
        # UnicodeDecodeError と JSONDecodeError は ValueError の子。
        return None
    if not isinstance(entry, dict):
        return None
    version = entry.get("v")
    # bool は int の子なので、``True == 1`` で通らないよう型も見る。
    if type(version) is not int or version != HISTORY_VERSION:
        return None
    return entry
