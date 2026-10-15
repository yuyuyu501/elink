import copy
from types import SimpleNamespace

import av
import pytest

from elink.core.codecs import DesktopDecoder
from elink.core.profiling import PipelineProfile
from elink.core.transport import settings
from elink.models import ValidationError
from scripts.benchmark_matrix import FRAME_RATES, RESOLUTIONS, assess, cases
from scripts.benchmark_stream import presentation_issues, result


@pytest.mark.parametrize('case', list(cases()))
def test_all_ui_combinations_have_explicit_codec_budget_outcome(case):
    # Independent boundary expectations: no silent downgrade of resolution/FPS.
    valid = (case['height'] <= 1080 or case['height'] == 1440 and case['fps'] <= 144
             or case['height'] == 2160 and case['fps'] <= 60)
    if valid:
        actual = settings(case)
        assert all(actual[key] == value for key, value in case.items())
    else:
        with pytest.raises(ValidationError, match='Level 5.2'):
            settings(case)


def test_sweep_presets_cover_current_ui():
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from elink.ui.controls import StreamOptions
    app = QApplication.instance() or QApplication([])
    options = StreamOptions()
    try:
        assert {tuple(options.resolution.itemData(i)) for i in range(options.resolution.count())} == set(RESOLUTIONS)
        assert {options.fps.itemData(i) for i in range(options.fps.count())} == set(FRAME_RATES)
        assert len(list(cases())) == 35
    finally:
        options.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_matrix_does_not_call_zero_presentation_or_slow_encode_a_pass():
    report = dict(config=dict(fps=90), rates=dict(encoded_frames_per_second=90,
                  decoded_frames_per_second=89, presented_frames_per_second=88, decode_errors_per_second=0),
                  host=dict(metadata=dict(encoder='h264_nvenc')),
                  client=dict(metadata=dict(decoder='D3D11VA', renderer='opengl-yuv')))
    assert assess(report)['within_five_percent']
    for key, value in [('presented_frames_per_second', 0), ('encoded_frames_per_second', 40),
                       ('decode_errors_per_second', .1)]:
        bad = copy.deepcopy(report)
        bad['rates'][key] = value
        assert not assess(bad)['within_five_percent']
    report['invalid_window_samples'] = 1
    assert not assess(report)['valid_measurement']
    report['invalid_window_samples'] = 0
    report['client']['metadata']['decoder'] = 'software-h264'
    assert not assess(report)['hardware_path']
    assert not assess(report)['within_five_percent']
    assert presentation_issues(dict(visible=True, minimized=False, exposed=True)) == []
    assert presentation_issues(dict(visible=True, minimized=True, exposed=False)) == ['minimized', 'unexposed']


def test_report_uses_warmed_interval_deltas_instead_of_startup_totals():
    args = SimpleNamespace(present=True, fps=60)
    before = dict(host=dict(counters=dict(encoded_frames=100, encoded_bytes=10_000_000)),
                  client=dict(counters=dict(decoded_frames=90, presented_frames=85, decode_errors=1)))
    after = dict(host=dict(counters=dict(encoded_frames=700, encoded_bytes=35_000_000)),
                 client=dict(counters=dict(decoded_frames=690, presented_frames=685, decode_errors=1)))
    report = result(args, before, after, 10)
    assert report['rates']['decoded_frames_per_second'] == 60
    assert report['rates']['presented_frames_per_second'] == 60
    assert report['rates']['decode_errors_per_second'] == 0
    assert report['rates']['encoded_mbps'] == 20


def test_decode_error_reason_is_available_in_session_report():
    class BadCodec:
        def decode(self, packet):
            raise av.InvalidDataError(1094995529, 'invalid packet')
    decoder = DesktopDecoder.__new__(DesktopDecoder)
    decoder.codec, decoder.hardware, decoder.mode = BadCodec(), True, 'hardware'
    profile = PipelineProfile()
    frame = SimpleNamespace(data=b'bad', timestamp=0, _elink_profile=profile,
                            _elink_queued_at=profile.clock())
    assert decoder.decode(frame) == []
    snapshot = profile.snapshot()
    assert snapshot['counters']['decode_errors'] == 1
    assert 'InvalidDataError' in snapshot['metadata']['last_decode_error']
