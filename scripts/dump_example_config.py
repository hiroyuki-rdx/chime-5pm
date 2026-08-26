#!/usr/bin/env python3
"""``config.example.json`` を ``chime.config.DEFAULT_CONFIG`` から再生成する。

既定値を変更したら、このスクリプトを実行して雛形を更新すること。
（更新漏れは ``tests/test_config.py`` が検出する）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chime.config import EXAMPLE_CONFIG_PATH, dump_default_config  # noqa: E402

if __name__ == "__main__":
    dump_default_config(EXAMPLE_CONFIG_PATH)
    print("生成しました: {0}".format(EXAMPLE_CONFIG_PATH))
