weather.py 自動起動無効化手順書既存の天気予報ボットが起動しないように設定します。以下の3つのパターンのうち、該当するものを実行してください。パターン1: Systemd (推奨確認)最も可能性が高い方法です。サービスの検索systemctl list-units --type=service | grep weather
もし weather.service などが見つかったら次へ。停止と無効化sudo systemctl stop weather.service
sudo systemctl disable weather.service
パターン2: Cron (crontab)起動時に実行する設定があるか確認します。設定を開くcrontab -e
# または管理者設定
sudo crontab -e
編集@reboot python3 ... weather.py という行があれば、行頭に # を付けてコメントアウトするか削除して保存します。パターン3: rc.local (古い手法)ファイルを開くsudo nano /etc/rc.local
編集weather.py を実行している行があれば削除またはコメントアウトします。最終手段: ファイルのリネームどうしても自動起動元がわからない場合、ファイル名を変えて物理的に動かなくします。mv /home/pi/weather.py /home/pi/weather.py.bak
（エラーログは出続けますが、動作は止まります）