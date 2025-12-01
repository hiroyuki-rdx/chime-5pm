# Campus Evening Chime System

大学施設用チャイムシステム（Raspberry Pi 3 Model B 向け）。
平日（月〜金）の 16:57 に、既存の `weather.py` プロセスを停止させ、アナウンスと「蛍の光」を自動再生して帰宅を促します。

## 1. ディレクトリ構成

```
~/steam5pm/
├── campus_chime.py      # [Main] アプリケーション本体
├── campus_chime.service # [Config] systemd設定ファイル
├── requirements.txt     # [Dep] 依存ライブラリ
├── assets/              # [Res] 音声リソース格納
│   ├── announce.wav     # アナウンス音声 (VOICEVOX:ずんだもん)
│   └── hotaru.mp3       # 楽曲ファイル (Auld Lang Syne)
└── docs/                # ドキュメント類
    ├── REQUIREMENTS.md  # 要件定義書
    └── ...
```

## 2. インストール手順 (Raspberry Pi)

### 準備

```bash
# リポジトリのクローン
git clone https://github.com/hiroyuki-rdx/chime-5pm.git ~/steam5pm
cd ~/steam5pm

# ライブラリインストール
sudo apt update
sudo apt install -y python3-pip libsdl2-2.0-0
pip3 install -r requirements.txt --break-system-packages
```

### 自動起動の設定

```bash
# サービスファイルの配置
sudo cp campus_chime.service /etc/systemd/system/

# 有効化と起動
sudo systemctl daemon-reload
sudo systemctl enable campus_chime.service
sudo systemctl start campus_chime.service

# 状態確認
sudo systemctl status campus_chime.service
```

## 3. 動作テスト (WSL/Windows)

開発環境 (WSL) では、音声再生を行わずにログ出力のみを行う「Mock モード」で動作します。

```bash
python3 campus_chime.py
```

出力例:

```
INFO - Starting Campus Chime System...
INFO - [MOCK] Starting playback sequence...
INFO - [MOCK] Playing announcement: ...
```

## 4. ライセンス・クレジット

- **音声合成**: VOICEVOX:ずんだもん
- **楽曲**: Auld Lang Syne (Public Domain / Copyright Free)
