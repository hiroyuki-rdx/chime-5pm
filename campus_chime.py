#!/usr/bin/env python3
"""キャンパス時報システム（エントリポイント）。

実処理は ``chime/`` パッケージにある。使い方は ``--help`` を参照。

    python3 campus_chime.py --help
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chime.cli import run  # noqa: E402  (sys.path 調整後にインポートする)

if __name__ == "__main__":
    sys.exit(run())
