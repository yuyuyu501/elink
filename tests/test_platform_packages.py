import hashlib
import json

import pytest

from elink import __version__
from scripts import publish_release as publisher


@pytest.mark.parametrize('platform,prefix,extension', [('linux-x64', 'Linux', 'deb'), ('android-arm64', 'Android', 'apk')])
@pytest.mark.parametrize('problem', [None, 'source', 'commit', 'bytes', 'checksum', 'verification', 'duplicate'])
def test_platform_packages_require_same_source_and_verified_bytes(tmp_path, platform, prefix, extension, problem):
    name = f'Elink-{__version__}-{platform}.{extension}'
    package = tmp_path / name
    package.write_bytes(b'tested platform package')
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    reference = dict(commit='a' * 40, source_sha256={'elink/app.py': 'b' * 64})
    report = dict(version=__version__, platform=platform, package=name, source_dirty=False,
                  sha256=digest, bytes=package.stat().st_size, **reference)
    report.update(self_test=dict(ok=True, runtime_stopped=True, audio_frames=20, video_frames=90),
                  signature_verified=True, unit_tests=True, lint=True)
    checksum = tmp_path / f'{prefix}.sha256'
    checksum.write_text(f'{digest}  {name}\n', encoding='ascii')
    if problem == 'source':
        report['source_sha256'] = {}
    elif problem == 'commit':
        report['commit'] = 'c' * 40
    elif problem == 'bytes':
        package.write_bytes(b'different build')
    elif problem == 'checksum':
        checksum.write_text('wrong checksum')
    elif problem == 'verification':
        report['self_test']['audio_frames'] = 0
        report['signature_verified'] = False
    (tmp_path / f'{prefix}.verification.json').write_text(json.dumps(report), encoding='utf-8')
    directories = [tmp_path] * (2 if problem == 'duplicate' else 1)
    if problem:
        with pytest.raises(RuntimeError):
            publisher.validate_platform_packages(directories, reference)
    else:
        assert len(publisher.validate_platform_packages(directories, reference)) == 3


def test_cross_platform_release_requires_every_platform_ci():
    requested = []
    def api(url):
        requested.append(url)
        if '/commits/' in url:
            return dict(sha='abc')
        conclusion = 'failure' if '/android.yml/' in url else 'success'
        return dict(workflow_runs=[dict(id=1, head_sha='abc', event='push', conclusion=conclusion)])
    with pytest.raises(RuntimeError, match='android.yml'):
        publisher.ensure_synced_ci(api, 'abc', ('test.yml', 'linux.yml', 'android.yml'))
    assert len(requested) == 4
