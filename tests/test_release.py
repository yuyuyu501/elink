import hashlib
import json
import urllib.error

import pytest

from scripts import publish_release as publisher
from scripts import release_windows as workflow
from scripts.check_decoder_colors import CASES as DECODER_COLOR_CASES


def test_github_delete_handles_empty_success_response(monkeypatch):
    class Response:
        status = 204
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def json(self):
            raise AssertionError('A 204 response has no JSON body.')
    class Session:
        def __init__(self, **_):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        def request(self, method, url, **_):
            assert method == 'DELETE' and url == publisher.API + '/releases/assets/42'
            return Response()
    monkeypatch.setattr(publisher.aiohttp, 'ClientSession', Session)
    github = publisher.GitHub.__new__(publisher.GitHub)
    github.token = 'test-only'
    assert github.api(publisher.API + '/releases/assets/42', 'DELETE') is None


@pytest.mark.parametrize('failure', ['sync', 'package', None])
def test_workflow_order_and_failure_stops_later_stages(tmp_path, monkeypatch, failure):
    notes = tmp_path / 'notes.md'
    notes.write_text('Release notes')
    calls = []
    def step(name, result=None):
        def run(*_):
            calls.append(name)
            if failure == name:
                raise RuntimeError(name + ' failed')
            return result
        return run
    monkeypatch.setattr(workflow, 'synchronize', step('sync'))
    monkeypatch.setattr(workflow, 'package', step('package', tmp_path))
    monkeypatch.setattr(workflow, 'publish', step('publish', 'url'))
    if failure:
        with pytest.raises(RuntimeError):
            workflow.release('change', notes)
    else:
        assert workflow.release('change', notes) == 'url'
    assert calls == {'sync': ['sync'], 'package': ['sync', 'package'],
                     None: ['sync', 'package', 'publish']}[failure]


def package_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, 'git', lambda *args: {
        ('status', '--porcelain'): '', ('remote', 'get-url', 'origin'): 'git@github.com:yuyuyu501/elink.git',
        ('rev-parse', 'HEAD'): 'abc',
    }[args])
    monkeypatch.setattr(publisher, 'source_snapshot', lambda: {'source': 'hash'})
    archive = tmp_path / f'Elink-{publisher.__version__}-windows-x64.zip'
    archive.write_bytes(b'tested archive')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = dict(version=publisher.__version__, source_dirty=False, commit='abc', source_sha256={'source': 'hash'},
                   self_test=dict(ok=True, runtime_stopped=True, audio_frames=10, video_frames=90),
                   gpu_test=dict(colors=[dict(native_rgb_error=2)] * 5,
                                 capture_diagnostics=dict(ok=True, gray_start_recovered=True,
                                                          continued_after_real_failure=True),
                                 live_settings=[dict(renderer=r, decoder=d, same_session=True, native_rgb_error=2)
                                                for r, d in [('cpu', 'software'), ('gpu', 'hardware')] * 3],
                                 live_capture=[dict(monotonic_pts=True, capture_backend='gpu', max_rgb_error=2)] * 4,
                                 decoder_colors=[dict(width=w, height=h, colorspace=space, color_range=color_range,
                                                      input_format=fmt, hardware_format='nv12',
                                                      software_colorspace=space, hardware_colorspace=space,
                                                      software_color_range=color_range, hardware_color_range=color_range,
                                                      software_rgb_error=2, hardware_rgb_error=2,
                                                      decoder_rgb_difference=0, native_rgb_error=2, composed_rgb_error=2)
                                                 for w, h, space, color_range, fmt in DECODER_COLOR_CASES],
                                 streams=[dict(decoder=d, native_video=True, cpu_fallback=True)
                                          for d in ('software-h264', 'D3D11VA')],
                                 super_resolution={str(s): [dict(native_rgb_error=2)] * 5 for s in (75, 67, 50)},
                                 super_resolution_streams=[dict(render_scale=s, upscaler='FSR1-EASU-RCAS',
                                                                native_video=True, cpu_fallback=True,
                                                                fsr_gpu=dict(samples=20)) for s in (75, 67, 50)],
                                 rtx_colors=[dict(native_rgb_error=2)] * 5,
                                 rtx_stream=dict(render_scale=50, upscaler='RTX-VSR', native_video=True,
                                                 cpu_fallback=True, rtx_gpu=dict(samples=20), rtx_probe_mae=.5),
                                 rtx_options=dict(default_fsr=True, active_driver_exposed=True, unavailable_removed=True),
                                 session_notices=[dict(renderer=r, native_banner=True, dismiss=True, auto_hide=True)
                                                  for r in ('cpu', 'gpu')],
                                 rtx_fallback=dict(fsr_fallback=True, native_video=True, silent=True, preference_fsr=True),
                                 native_capture=[dict(width=w, height=h, format='nv12', max_rgb_error=2,
                                                      colorspace=1, decoded_colorspace=1,
                                                      color_range=2, decoded_color_range=2)
                                                 for w, h in ((1920, 1080), (1440, 810), (1280, 720))]),
                   updater_test=dict(ok=True), package_sha256=digest)
    (tmp_path / 'verification.json').write_text(json.dumps(receipt), encoding='utf-8')
    (tmp_path / 'SHA256SUMS.txt').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    notes = tmp_path / 'notes.md'
    notes.write_text('release notes')
    return notes, receipt, archive


@pytest.mark.parametrize('problem', ['checksum', 'commit', 'updater', 'source', 'gpu_missing', 'gpu_black',
                                   'fsr_missing', 'fsr_black', 'fsr_timer', 'capture_missing', 'capture_color',
                                   'rtx_missing', 'rtx_fallback', 'rtx_timer', 'rtx_inactive', 'rtx_notice', 'rtx_options',
                                   'session_notices',
                                   'decoder_colors_missing', 'decoder_clipped', 'decoder_range', 'decoder_native',
                                   'live_missing', 'live_reconnect', 'live_black', 'live_capture',
                                   'capture_diagnostics'])
def test_publication_rejects_unverified_or_mismatched_package(tmp_path, monkeypatch, problem):
    _, receipt, archive = package_fixture(tmp_path, monkeypatch)
    if problem == 'checksum':
        archive.write_bytes(b'corrupt')
    elif problem == 'commit':
        receipt['commit'] = 'other'
    elif problem == 'updater':
        receipt['updater_test']['ok'] = False
    elif problem == 'gpu_missing':
        receipt.pop('gpu_test')
    elif problem == 'gpu_black':
        receipt['gpu_test']['colors'][0]['native_rgb_error'] = 240
    elif problem == 'fsr_missing':
        receipt['gpu_test'].pop('super_resolution_streams')
    elif problem == 'fsr_black':
        receipt['gpu_test']['super_resolution']['67'][0]['native_rgb_error'] = 240
    elif problem == 'fsr_timer':
        receipt['gpu_test']['super_resolution_streams'][0]['fsr_gpu'] = None
    elif problem == 'rtx_missing':
        receipt['gpu_test'].pop('rtx_colors')
    elif problem == 'rtx_fallback':
        receipt['gpu_test']['rtx_stream']['upscaler'] = 'FSR1-EASU-RCAS'
    elif problem == 'rtx_timer':
        receipt['gpu_test']['rtx_stream']['rtx_gpu'] = None
    elif problem == 'rtx_inactive':
        receipt['gpu_test']['rtx_stream']['rtx_probe_mae'] = 0
    elif problem == 'rtx_notice':
        receipt['gpu_test']['rtx_fallback']['silent'] = False
    elif problem == 'rtx_options':
        receipt['gpu_test']['rtx_options']['active_driver_exposed'] = False
    elif problem == 'session_notices':
        receipt['gpu_test']['session_notices'][0]['dismiss'] = False
    elif problem == 'capture_missing':
        receipt['gpu_test'].pop('native_capture')
    elif problem == 'capture_color':
        receipt['gpu_test']['native_capture'][0]['decoded_colorspace'] = 5
    elif problem == 'decoder_colors_missing':
        receipt['gpu_test'].pop('decoder_colors')
    elif problem == 'decoder_clipped':
        receipt['gpu_test']['decoder_colors'][0]['hardware_rgb_error'] = 18
    elif problem == 'decoder_range':
        receipt['gpu_test']['decoder_colors'][0]['hardware_color_range'] = 0
    elif problem == 'decoder_native':
        receipt['gpu_test']['decoder_colors'][0]['native_rgb_error'] = 18
    elif problem == 'live_missing':
        receipt['gpu_test'].pop('live_settings')
    elif problem == 'live_reconnect':
        receipt['gpu_test']['live_settings'][0]['same_session'] = False
    elif problem == 'live_black':
        receipt['gpu_test']['live_settings'][1]['native_rgb_error'] = 240
    elif problem == 'live_capture':
        receipt['gpu_test']['live_capture'][0]['monotonic_pts'] = False
    elif problem == 'capture_diagnostics':
        receipt['gpu_test']['capture_diagnostics']['gray_start_recovered'] = False
    else:
        receipt['source_sha256'] = {}
    (tmp_path / 'verification.json').write_text(json.dumps(receipt), encoding='utf-8')
    with pytest.raises(RuntimeError):
        publisher.validate_package(tmp_path)


@pytest.mark.parametrize('scenario', ['success', 'missing_tag', 'bad_asset', 'existing_public', 'ci_failed', 'wrong_tag', 'resume'])
def test_publish_only_after_ci_and_all_asset_checks(tmp_path, monkeypatch, scenario):
    notes, _, _ = package_fixture(tmp_path, monkeypatch)
    mutations = []
    release = dict(id=42, tag_name='v' + publisher.__version__, target_commitish='abc',
                   draft=scenario != 'existing_public', assets=[],
                   upload_url='https://uploads.github.com/repos/yuyuyu501/elink/releases/42/assets{?name}', html_url='https://github.com/yuyuyu501/elink/releases/test')
    if scenario == 'resume':
        data = (tmp_path / 'SHA256SUMS.txt').read_bytes()
        release['assets'].append(dict(name='SHA256SUMS.txt', size=len(data), state='uploaded',
                                      digest='sha256:' + hashlib.sha256(data).hexdigest()))

    def api(url, method='GET', value=None, binary=None):
        if method != 'GET':
            mutations.append((url, method, value))
        if '/commits/main' in url:
            return {'sha': 'abc'}
        if '/actions/' in url:
            return {'workflow_runs': [dict(id=1, head_sha='abc', event='push',
                                          conclusion='failure' if scenario == 'ci_failed' else 'success')]}
        if '/git/ref/' in url:
            if scenario == 'missing_tag':
                raise urllib.error.HTTPError(url, 404, 'Not Found', None, None)
            return {'object': dict(type='commit', sha='wrong' if scenario == 'wrong_tag' else 'abc')}
        if '/releases?' in url:
            return [release] if scenario in ('existing_public', 'resume') else []
        if 'uploads.github.com' in url:
            return dict(state='uploaded', size=len(binary), digest='sha256:' +
                        (hashlib.sha256(binary).hexdigest() if scenario != 'bad_asset' else '0' * 64))
        if method == 'POST':
            assert value['draft'] is True
            return release
        if method == 'PATCH':
            return dict(release, **value)
        raise AssertionError(url)

    if scenario in ('success', 'missing_tag', 'resume'):
        assert publisher.publish(tmp_path, notes, api) == release['html_url']
        assert mutations[-1][2]['draft'] is False
        assert sum('uploads.github.com' in url for url, _, _ in mutations) == (2 if scenario == 'resume' else 3)
    else:
        with pytest.raises(RuntimeError):
            publisher.publish(tmp_path, notes, api)
        assert not any(value and value.get('draft') is False for _, _, value in mutations)
        if scenario in ('ci_failed', 'wrong_tag', 'existing_public'):
            assert mutations == []
