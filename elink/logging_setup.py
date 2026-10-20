"""Persistent application errors, without frame pixels or input payloads."""
from functools import lru_cache
import logging
from logging.handlers import RotatingFileHandler
import platform
import traceback

import av

from . import __version__


@lru_cache(maxsize=1)
def environment_details():
    return dict(elink_version=__version__, python_version=platform.python_version(),
                system=platform.platform(), av_version=av.__version__)


def configure_logging(root):
    directory = root / 'logs'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'elink.log'
    logger = logging.getLogger('elink')
    for existing in list(logger.handlers):
        if getattr(existing, '_elink_file_log', False):
            logger.removeHandler(existing)
            existing.close()
    handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3,
                                  encoding='utf-8')
    handler._elink_file_log = True
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info('Application started: %s', environment_details())
    return path


def exception_details(exc):
    # Never capture locals: media, clipboard contents and credentials can live there.
    trace = traceback.TracebackException.from_exception(exc, capture_locals=False)
    return dict(type=type(exc).__name__[:128], message=str(exc)[:512],
                traceback=''.join(trace.format())[-4096:])
