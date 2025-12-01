#!/usr/bin/env python3
import os
import time
import logging
import platform
import subprocess
import argparse
from datetime import datetime

# 依存ライブラリのインポート試行
try:
    import pygame
except ImportError:
    pygame = None

# 2.1 定数定義
TARGET_HOUR = 16
TARGET_MINUTE = 57
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# パスは設計書のディレクトリ構成に合わせて assets/ を参照
ANNOUNCE_FILE = os.path.join(BASE_DIR, "assets", "announce.wav")
MUSIC_FILE = os.path.join(BASE_DIR, "assets", "hotaru.mp3")
FADE_IN_MS = 2000
CHECK_INTERVAL = 1.0
CONFLICT_APP = "weather.py"

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class EnvironmentHandler:
    """
    責務: OS判定と競合排除
    """
    
    @staticmethod
    def is_production_linux():
        """
        Linux実機かどうかを判定 (WSLを除く)
        """
        uname = platform.uname()
        if uname.system != 'Linux':
            return False
        if 'microsoft' in uname.release.lower() or 'wsl' in uname.release.lower():
            return False
        return True

    @classmethod
    def kill_conflict_process(cls):
        """
        Linux実機の場合のみ subprocess.run(["pkill", "-f", CONFLICT_APP]) を実行
        """
        if cls.is_production_linux():
            try:
                logger.info(f"Attempting to kill conflict process: {CONFLICT_APP}")
                subprocess.run(["pkill", "-f", CONFLICT_APP], check=False)
            except Exception as e:
                logger.error(f"Failed to execute pkill: {e}")
        else:
            logger.info(f"[MOCK] Would kill process '{CONFLICT_APP}' here (Development Env)")

class SoundDesigner:
    """
    責務: 音声再生制御
    """
    
    def play_sequence(self):
        """
        env.kill_conflict_process() (競合排除)
        環境判定 (Real/Mock) に基づき再生処理へ分岐
        """
        EnvironmentHandler.kill_conflict_process()
        
        if EnvironmentHandler.is_production_linux():
            self._play_real()
        else:
            self._play_mock()

    def _play_real(self):
        """実機用ロジック - Pygame使用"""
        if pygame is None:
            logger.error("pygame module is not installed.")
            return
            
        if not os.path.exists(ANNOUNCE_FILE) or not os.path.exists(MUSIC_FILE):
            logger.error("Audio files not found.")
            return

        try:
            pygame.mixer.init()
            
            # アナウンス再生 (フェードなし)
            logger.info(f"Playing announcement: {ANNOUNCE_FILE}")
            pygame.mixer.music.load(ANNOUNCE_FILE)
            pygame.mixer.music.play()
            
            # 終了待ち
            while pygame.mixer.music.get_busy():
                time.sleep(0.1)
                
            # 楽曲再生 (フェードイン)
            logger.info(f"Playing music: {MUSIC_FILE}")
            pygame.mixer.music.load(MUSIC_FILE)
            pygame.mixer.music.play(loops=0, fade_ms=FADE_IN_MS)
            
            # 終了待ち
            while pygame.mixer.music.get_busy():
                time.sleep(0.5)
                
            logger.info("Playback sequence completed.")
            
        except Exception as e:
            logger.error(f"Audio playback error: {e}")

    def _play_mock(self):
        """WSL用ロジック - ログ出力のみ"""
        logger.info("[MOCK] Starting playback sequence...")
        
        # アナウンスシミュレーション
        logger.info(f"[MOCK] Playing announcement: {ANNOUNCE_FILE}")
        time.sleep(2) # アナウンスの長さをシミュレート
        
        # 楽曲シミュレーション
        logger.info(f"[MOCK] Playing music with fade-in ({FADE_IN_MS}ms): {MUSIC_FILE}")
        time.sleep(3) # 楽曲再生時間をシミュレート
        
        logger.info("[MOCK] Playback sequence completed.")

def main():
    """
    責務: メインループとスケジューリング
    """
    # 引数解析
    parser = argparse.ArgumentParser(description="Campus Evening Chime System")
    parser.add_argument("--test", action="store_true", help="Test mode: Play sequence immediately and exit")
    args = parser.parse_args()

    logger.info("Starting Campus Chime System (Technical Specifications v1)")
    
    # 初期化
    designer = SoundDesigner()
    
    # テストモード: 即時実行して終了
    if args.test:
        logger.info("Running in TEST MODE (Immediate playback)")
        designer.play_sequence()
        logger.info("Test completed. Exiting.")
        return

    last_played_date = None
    
    try:
        while True:
            now = datetime.now()
            
            # 曜日判定: weekday() < 5 (月=0 〜 金=4)
            if now.weekday() < 5:
                # 時刻判定
                is_time = (now.hour == TARGET_HOUR and now.minute == TARGET_MINUTE)
                # 実際には秒単位のズレやループタイミングを考慮し、
                # 「17:00台」かつ「今日まだ再生していない」で判定するのが安全だが、
                # 設計書のロジック「17:00 かつ 未再生」を厳密に解釈しつつ実装する。
                # ここでは「17:00:00 〜 17:00:59」の間であればトリガーとする。
                
                current_date = now.date()
                
                if now.hour == TARGET_HOUR and now.minute == TARGET_MINUTE:
                    if last_played_date != current_date:
                        logger.info("Trigger time reached.")
                        designer.play_sequence()
                        last_played_date = current_date
                
                # 日付が変わったらリセット... は last_played_date != current_date で自然に達成される
                
            else:
                # 土日は何もしない
                pass
            
            time.sleep(CHECK_INTERVAL)
            
    except KeyboardInterrupt:
        logger.info("System stopping...")

if __name__ == "__main__":
    main()

