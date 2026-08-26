"""実行環境の判定。

実機（Raspberry Pi 等の Linux）と開発環境（WSL / macOS / Windows）を区別し、
音声を実際に鳴らしてよい環境かどうかを判定する。
"""

from __future__ import annotations

import os
import platform
import shutil
from typing import Dict


def is_wsl() -> bool:
    """WSL（Windows Subsystem for Linux）上で動作しているかを返す。"""
    release = platform.uname().release.lower()
    if "microsoft" in release or "wsl" in release:
        return True
    # WSL2 のカーネルによっては release に痕跡が残らないことがある。
    return bool(os.environ.get("WSL_DISTRO_NAME"))


def is_production_linux() -> bool:
    """音声出力を行ってよい Linux 実機かどうかを返す。"""
    if platform.uname().system != "Linux":
        return False
    return not is_wsl()


def has_command(name: str) -> bool:
    """外部コマンドが PATH 上に存在するかを返す。"""
    return bool(name) and shutil.which(name) is not None


def describe() -> Dict[str, str]:
    """ログ出力用に環境情報をまとめて返す。"""
    uname = platform.uname()
    return {
        "system": uname.system,
        "release": uname.release,
        "machine": uname.machine,
        "python": platform.python_version(),
        "wsl": str(is_wsl()),
        "production": str(is_production_linux()),
    }
