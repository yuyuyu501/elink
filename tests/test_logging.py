import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from elink import __version__
from elink.logging_setup import configure_logging, exception_details
from elink import logging_setup


@pytest.fixture
def application_logger():
    logger = logging.getLogger('elink')
    previous_level = logger.level
    previous_handlers = list(logger.handlers)
    try:
        yield logger
    finally:
        for handler in list(logger.handlers):
            if handler not in previous_handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(previous_level)


def test_utf8_file_keeps_chained_exception_and_notes_without_stdio_or_locals(tmp_path, monkeypatch, application_logger):
    import sys
    monkeypatch.setattr(sys, 'stderr', None)
    monkeypatch.setattr(sys, 'stdout', None)
    filename = configure_logging(tmp_path)
    private_value = 'do-not-record-local-input-payload'
    try:
        try:
            raise OSError('video processor unavailable')
        except OSError as cause:
            error = RuntimeError('采集校准失败')
            error.add_note('D3D11 stage=calibration_colors output=1280x720')
            raise error from cause
    except RuntimeError as exc:
        logging.getLogger('elink.core.media').exception('D3D11 capture failed')
        details = exception_details(exc)
    text = filename.read_text(encoding='utf-8')
    assert __version__ in text
    for expected in ('OSError: video processor unavailable', 'RuntimeError: 采集校准失败',
                     'stage=calibration_colors', 'test_utf8_file_keeps_chained_exception'):
        assert expected in text and expected in details['traceback']
    assert private_value not in text and private_value not in json.dumps(details)


def test_log_rotation_is_bounded_with_worker_threads(tmp_path, application_logger):
    filename = configure_logging(tmp_path)
    handler = next(h for h in application_logger.handlers if getattr(h, '_elink_file_log', False))
    handler.maxBytes = 600
    def record(index):
        logging.getLogger('elink.core.media').warning('测试错误 %s %s', index, 'x' * 100)
    with ThreadPoolExecutor(max_workers=3) as workers:
        list(workers.map(record, range(30)))
    files = sorted(filename.parent.iterdir())
    assert {p.name for p in files} == {'elink.log', 'elink.log.1', 'elink.log.2', 'elink.log.3'}
    assert all('测试错误' in p.read_text(encoding='utf-8') for p in files)
    assert all(p.stat().st_size < 1000 for p in files)


def test_reconfigure_logging_closes_previous_file_and_does_not_duplicate(tmp_path, application_logger):
    first = configure_logging(tmp_path / 'first')
    second = configure_logging(tmp_path / 'second')
    logging.getLogger('elink.core.media').error('one diagnostic')
    assert 'one diagnostic' not in first.read_text(encoding='utf-8')
    assert second.read_text(encoding='utf-8').count('one diagnostic') == 1


def test_windows_metadata_does_not_query_wmi_or_platform(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(logging_setup, 'sys', SimpleNamespace(platform='win32',
                         getwindowsversion=lambda: SimpleNamespace(major=10, minor=0, build=26200)))
    def blocked_query():
        raise AssertionError('platform.platform can block in Windows WMI')
    monkeypatch.setattr(logging_setup.platform, 'platform', blocked_query)
    logging_setup.environment_details.cache_clear()
    try:
        assert logging_setup.environment_details()['system'] == 'Windows-10.0.26200'
    finally:
        logging_setup.environment_details.cache_clear()


def test_startup_log_is_written_before_collecting_environment(tmp_path, monkeypatch, application_logger):
    def failed_metadata():
        raise RuntimeError('metadata unavailable')
    monkeypatch.setattr(logging_setup, 'environment_details', failed_metadata)
    with pytest.raises(RuntimeError, match='metadata unavailable'):
        configure_logging(tmp_path)
    text = (tmp_path / 'logs/elink.log').read_text(encoding='utf-8')
    assert f'Elink {__version__} file logging ready' in text
