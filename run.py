#!/usr/bin/env python3
import time
import logging
from datetime import datetime
from src.config import ANNOUNCE_FILE, CHIME_FILE, TRIGGER_HOUR, CHECK_INTERVAL_SEC
from src.environment import Environment
from src.player import ChimePlayer

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    """
    メインループ
    Requirement 3.1: スケジューリング機能 (平日のみ)
    """
    logger.info("Starting Campus Evening Chime System (v1.2.0)...")
    Environment.detect()
    
    player = ChimePlayer(ANNOUNCE_FILE, CHIME_FILE)
    
    try:
        while True:
            now = datetime.now()
            
            # 平日(0-4: 月-金) チェック
            is_weekday = now.weekday() < 5
            
            # トリガー時刻チェック (17時台)
            is_trigger_time = now.hour == TRIGGER_HOUR
            
            if is_weekday and is_trigger_time:
                player.play()
            
            # CPU負荷低減
            time.sleep(CHECK_INTERVAL_SEC)
            
    except KeyboardInterrupt:
        logger.info("System stopping...")

if __name__ == "__main__":
    main()
