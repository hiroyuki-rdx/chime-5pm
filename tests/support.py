"""テスト共通のヘルパー。"""

from __future__ import annotations

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture_path(name: str) -> str:
    """``tests/fixtures/`` 配下のファイルパスを返す。"""
    return os.path.join(FIXTURES, name)


def load_fixture(name: str):
    """``tests/fixtures/`` の JSON を読み込む。"""
    with open(fixture_path(name), "r", encoding="utf-8") as handle:
        return json.load(handle)
