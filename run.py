#!/usr/bin/env python3
import time
import logging
from datetime import datetime
from src.config import CHIME_FILE, TRIGGER_HOUR, CHECK_INTERVAL_SEC
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
    """
    logger.info("Starting Campus Evening Chime System...")
    Environment.detect()
    
    player = ChimePlayer(CHIME_FILE)
    
    try:
        while True:
            now = datetime.now()
            
            # 平日(0-4) かつ 17:00台 かつ まだ再生していない場合
            if now.weekday() < 5 and now.hour == TRIGGER_HOUR:
                player.play()
            
            # CPU負荷低減
            time.sleep(CHECK_INTERVAL_SEC)
            
    except KeyboardInterrupt:
        logger.info("System stopping...")

if __name__ == "__main__":
    main()

