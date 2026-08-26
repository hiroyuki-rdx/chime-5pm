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

## 環境

- OS:  <!-- 例: Raspberry Pi OS Lite 32bit (Bookworm) -->
- 機種: <!-- 例: Raspberry Pi 3 Model B -->
- バージョン: <!-- python3 campus_chime.py --version の出力 -->
