# Project Knowledge Base

## Project: Campus Evening Chime System

### 概要

大学施設において、平日 17:00 に「蛍の光」を自動再生するシステム。Raspberry Pi 3B 上で動作し、開発環境（WSL）と本番環境を自動判定する。

### アーキテクチャ

- **Hardware**: Raspberry Pi 3 Model B
- **OS**: Raspberry Pi OS / Windows (WSL for Dev)
- **Language**: Python 3.9+
- **Key Libraries**:
  - `pygame`: 音声再生用（フェードイン処理含む）
  - `schedule` or `time` loop: 定時実行監視
- **Deployment**: GitHub -> Raspberry Pi (Systemd service)

### 重要な技術的決定

- **環境判定**: ホスト名や OS 情報を取得して動作モード（本番/Mock）を切り替える。
- **二重再生防止**: 日付をキーとしたフラグ管理を行う。
- **フェードイン**: 音量を 0.0 から 1.0 まで徐々に上げる処理を実装する。

### 運用・保守

- **Logs**: systemd journal (`journalctl`)
- **Updates**: `git pull` && `sudo systemctl restart chime.service`

### デプロイ手順 (Deployment Guide)

1. **Raspberry Pi セットアップ**

   ```bash
   sudo apt update
   sudo apt install python3-pygame git
   ```

2. **インストール**

   ```bash
   cd /home/pi
   git clone https://github.com/hiroyuki-rdx/chime-5pm.git steam5pm
   cd steam5pm
   ```

3. **サービス登録**

   ```bash
   sudo cp chime.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable chime.service
   sudo systemctl start chime.service
   ```

4. **動作確認**
   ```bash
   sudo systemctl status chime.service
   journalctl -u chime.service -f
   ```
