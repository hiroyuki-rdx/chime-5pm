import os
import time
import logging
from datetime import datetime
from .environment import Environment
from .config import FADE_DURATION_MS

# 外部ライブラリのインポート試行
try:
    import pygame
except ImportError:
    pygame = None

logger = logging.getLogger(__name__)

class ChimePlayer:
    """
    時報再生を担当するクラス
    Requirement 3.1: 時報再生機能, 音響処理, 二重再生防止
    """
    def __init__(self, file_path):
        self.file_path = file_path
        self.last_played_date = None

    def play(self):
        """
        環境に応じた再生処理を行う
        """
        today = datetime.now().date()
        
        # 二重再生防止 (Requirement 3.1)
        if self.last_played_date == today:
            logger.info("Chime already played today. Skipping.")
            return

        logger.info(f"Attempting to play chime: {self.file_path}")

        if Environment.is_production():
            self._play_production()
        else:
            self._play_mock()

        self.last_played_date = today

    def _play_production(self):
        """本番環境用再生ロジック"""
        if pygame is None:
            logger.error("pygame module is not installed. Cannot play audio.")
            return

        try:
            if not os.path.exists(self.file_path):
                logger.error(f"Audio file not found: {self.file_path}")
                return

            pygame.mixer.init()
            pygame.mixer.music.load(self.file_path)
            pygame.mixer.music.set_volume(0.0)
            pygame.mixer.music.play()
            
            # Fade-in: 0% -> 100%
            steps = 20
            duration = FADE_DURATION_MS / 1000.0
            step_time = duration / steps
            
            logger.info("Starting fade-in...")
            for i in range(steps + 1):
                vol = i / steps
                pygame.mixer.music.set_volume(vol)
                time.sleep(step_time)
            
            logger.info("Playback started with max volume.")
            
            # 再生終了まで待機
            while pygame.mixer.music.get_busy():
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"Audio playback failed: {e}")

    def _play_mock(self):
        """開発環境用モックロジック"""
        logger.info("[MOCK] Initializing audio driver...")
        if not os.path.exists(self.file_path):
            logger.warning(f"[MOCK] Audio file not found at {self.file_path}, but proceeding.")
        else:
            logger.info(f"[MOCK] Loading file: {self.file_path}")
            
        logger.info(f"[MOCK] Fading in volume 0% -> 100% ({FADE_DURATION_MS}ms)...")
        time.sleep(FADE_DURATION_MS / 1000.0)
        logger.info("[MOCK] Playback finished.")

