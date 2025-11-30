import platform
import logging

logger = logging.getLogger(__name__)

class Environment:
    """
    実行環境を判定・管理するクラス
    Requirement 3.3: 環境自動判定機能
    """
    IS_RASPBERRY_PI = False
    IS_WSL = False
    
    @classmethod
    def detect(cls):
        """
        OSやカーネル情報を基に環境を判定する
        """
        uname = platform.uname()
        system = uname.system
        release = uname.release
        
        if system == 'Linux':
            if 'microsoft' in release.lower() or 'wsl' in release.lower():
                cls.IS_WSL = True
                cls.IS_RASPBERRY_PI = False
                logger.info("Environment detected: Development (WSL)")
            elif 'aarch64' in uname.machine or 'arm' in uname.machine:
                cls.IS_RASPBERRY_PI = True
                cls.IS_WSL = False
                logger.info("Environment detected: Production (Raspberry Pi)")
            else:
                # Fallback logic
                logger.warning(f"Unknown Linux environment: {release}. Treating as Development.")
                cls.IS_WSL = True
        else:
            logger.warning(f"Non-Linux environment detected: {system}. Treating as Development.")
            cls.IS_WSL = True

    @classmethod
    def is_production(cls):
        return cls.IS_RASPBERRY_PI

