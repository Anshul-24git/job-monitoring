import logging
from logging.handlers import RotatingFileHandler


def setup_logging(log_path: str) -> None:
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(formatter)

    logger.handlers = [stream_handler, file_handler]

