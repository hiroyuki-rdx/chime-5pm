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
    Requirement 3.2: 音声再生機能 (Sequential Playback)
    """
    def __init__(self, announce_file, chime_file):
        self.announce_file = announce_file
        self.chime_file = chime_file
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

        logger.info(f"Starting playback sequence...")

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

        # Validate files
        if not os.path.exists(self.announce_file):
            logger.error(f"Announce file not found: {self.announce_file}")
            return
        if not os.path.exists(self.chime_file):
            logger.error(f"Chime file not found: {self.chime_file}")
            return

        try:
            pygame.mixer.init()

            # --- Phase 1: Announcement (WAV) ---
            logger.info(f"Phase 1: Playing announcement ({self.announce_file})")
            # WAVなどの効果音的なものはSoundオブジェクトとして扱うのが一般的だが、
            # 長尺の場合やリソース管理の観点からMusicストリームを使う手もある。
            # ここでは要件「アナウンスが完全に終了するまで待機」を確実にするため、
            # 両方とも music ストリームで順次再生するアプローチを取る（競合回避）。
            
            pygame.mixer.music.load(self.announce_file)
            pygame.mixer.music.set_volume(1.0)
            pygame.mixer.music.play()
            
            # Wait for announcement to finish
            while pygame.mixer.music.get_busy():
                time.sleep(0.5)
            
            logger.info("Announcement finished.")
            
            # --- Phase 2: Chime (MP3) with Fade-in ---
            logger.info(f"Phase 2: Playing chime ({self.chime_file}) with fade-in")
            pygame.mixer.music.load(self.chime_file)
            pygame.mixer.music.set_volume(0.0)
            pygame.mixer.music.play()
            
            # Fade-in logic
            steps = 20
            duration = FADE_DURATION_MS / 1000.0
            step_time = duration / steps
            
            for i in range(steps + 1):
                vol = i / steps
                pygame.mixer.music.set_volume(vol)
                time.sleep(step_time)
            
            logger.info("Playback started with max volume.")
            
            # Wait for chime to finish
            while pygame.mixer.music.get_busy():
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"Audio playback failed: {e}")

    def _play_mock(self):
        """開発環境用モックロジック"""
        logger.info("[MOCK] Initializing audio driver...")
        
        # Phase 1
        if os.path.exists(self.announce_file):
            logger.info(f"[MOCK] Phase 1: Playing announcement {self.announce_file}")
        else:
            logger.warning(f"[MOCK] Phase 1: File not found {self.announce_file}")
        
        time.sleep(2) # Simulate announcement duration
        logger.info("[MOCK] Announcement finished.")

        # Phase 2
        if os.path.exists(self.chime_file):
            logger.info(f"[MOCK] Phase 2: Loading chime {self.chime_file}")
        else:
             logger.warning(f"[MOCK] Phase 2: File not found {self.chime_file}")

        logger.info(f"[MOCK] Fading in volume 0% -> 100% ({FADE_DURATION_MS}ms)...")
        time.sleep(FADE_DURATION_MS / 1000.0)
        logger.info("[MOCK] Playback finished.")
