import os

# プロジェクトのルートディレクトリ
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 音源ファイルのパス
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
ANNOUNCE_FILE = os.path.join(ASSETS_DIR, "announce.wav")
CHIME_FILE = os.path.join(ASSETS_DIR, "hotaru.mp3")

# 再生設定
TRIGGER_HOUR = 17  # 17時
FADE_DURATION_MS = 2000  # フェードイン時間 (ミリ秒)
CHECK_INTERVAL_SEC = 1   # 監視ループの待機時間
