---
name: 不具合の報告
about: 放送されない・音が出ない等の不具合
title: "[BUG] "
labels: bug
---

## 症状

<!-- 何が起きたか。いつの放送か（例: 10/15 の 14:00 の時報） -->

## 期待する動作

## 確認したこと

```
# 状態
sudo systemctl status campus_chime.service

# 該当時刻前後のログ
journalctl -u campus_chime.service --since "today" --no-pager
```

<!-- 上記の出力を貼ってください -->

## 現在の状態（--status の出力）

```
python3 campus_chime.py --status
```

<!-- 版・時刻の同期・サービスの状態・直近の放送・次の予定を表示します。鳴らさず、何も書き換えません。出力を貼ってください -->

## 設置状態の点検（--check の出力）

```
python3 campus_chime.py --check
```

<!-- 設定・作り置きの音声・音源・書き込み先の問題を表示します。鳴らさず、何も書き換えません。出力を貼ってください -->

## 環境

- OS:  <!-- 例: Raspberry Pi OS Lite 32bit (Bookworm) -->
- 機種: <!-- 例: Raspberry Pi 3 Model B -->
- バージョン: <!-- python3 campus_chime.py --version の出力 -->
