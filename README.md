Campus Chime System 大学施設用チャイムシステム（ラズパイ 3B 向け）。毎日 17:00 に weather.py を停止させ、フェードインで hotaru.mp3 を再生します。必要ファイル構成~/campus_chime/
├── campus_chime.py # メインスクリプト
├── campus_chime.service # Systemd 設定ファイル
└── hotaru.mp3 # 音源ファイル (別途用意)
インストール手順 (Raspberry Pi)準備# リポジトリのクローン
git clone [https://github.com/](https://github.com/)<YOUR_USER>/campus-chime.git ~/campus_chime
cd ~/campus_chime

# ライブラリインストール

sudo apt update
sudo apt install -y python3-pip libsdl2-2.0-0
pip3 install pygame --break-system-packages
音源の配置著作権フリーの auld_lang_syne.mp3 をこのフォルダに配置する。自動起動の設定# サービスファイルの配置
sudo cp campus_chime.service /etc/systemd/system/

# 有効化と起動

sudo systemctl daemon-reload
sudo systemctl enable campus_chime.service
sudo systemctl start campus_chime.service
確認 sudo systemctl status campus_chime.service
動作テスト (WSL/Windows)音は鳴りませんが、ログで動作を確認できます。python3 campus_chime.py
