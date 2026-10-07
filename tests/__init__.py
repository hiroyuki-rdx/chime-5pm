"""テストパッケージ。

``python3 -m unittest discover -s tests -t .`` で実行する。
リポジトリルートを ``sys.path`` に通し、アプリのログ出力を抑制する。
"""

import logging
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# テスト出力を読みやすくするため、アプリのログは抑制する
# （必要なテストは tests/support.py の logs_enabled() で一時的に戻す。
# 抜けるときは、ここで設定した状態へ戻る）。
logging.disable(logging.CRITICAL)
