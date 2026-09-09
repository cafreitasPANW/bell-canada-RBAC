import logging

class ColorFormatter(logging.Formatter):
    COLORS = {
        'ERROR': '\033[91m',    # Red
        'WARNING': '\033[93m',  # Orange/Yellow
        'INFO': '\033[92m',     # Green
        'DEBUG': '\033[0m',     # Default
    }
    RESET = '\033[0m'

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        # Add timestamp to the log output
        message = super().format(record)
        return f"{color}{message}{self.RESET}"
