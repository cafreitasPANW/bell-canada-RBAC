import os
import logging
from dotenv import load_dotenv
from common.color_formatter import ColorFormatter

load_dotenv()

# Get log level from environment variable, default to INFO
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

# Validate log level and convert to logging constant
VALID_LOG_LEVELS = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}

# Use INFO as fallback if invalid level is provided
if LOG_LEVEL not in VALID_LOG_LEVELS:
    print(f"Warning: Invalid LOG_LEVEL '{LOG_LEVEL}'. Using INFO as default.")
    LOG_LEVEL = 'INFO'

LOG_LEVEL_VALUE = VALID_LOG_LEVELS[LOG_LEVEL]

class LoggerFactory:
    @staticmethod
    def get_logger(name):
        handler = logging.StreamHandler()
        # Set formatter to include timestamp, level, and message
        handler.setFormatter(ColorFormatter('%(asctime)s %(levelname)s: %(message)s'))
        handler.setLevel(LOG_LEVEL_VALUE)  # Set handler level to match logger level
        logger = logging.getLogger(name)
        logger.setLevel(LOG_LEVEL_VALUE)
        logger.handlers = []
        logger.addHandler(handler)
        logger.propagate = False

        return logger
