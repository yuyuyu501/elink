import hashlib
import json

import pytest

from scripts import publish_release as publisher
from scripts import release_windows as workflow


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
                   updater_test=dict(ok=True), package_sha256=digest)
    (tmp_path / 'verification.json').write_text(json.dumps(receipt), encoding='utf-8')
    (tmp_path / 'SHA256SUMS.txt').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    notes = tmp_path / 'notes.md'
    notes.write_text('release notes')
    return notes, receipt, archive


@pytest.mark.parametrize('problem', ['checksum', 'commit', 'updater', 'source'])
def test_publication_rejects_unverified_or_mismatched_package(tmp_path, monkeypatch, problem):
    _, receipt, archive = package_fixture(tmp_path, monkeypatch)
    if problem == 'checksum':
        archive.write_bytes(b'corrupt')
    elif problem == 'commit':
        receipt['commit'] = 'other'
    elif problem == 'updater':
        receipt['updater_test']['ok'] = False
    else:
        receipt['source_sha256'] = {}
    (tmp_path / 'verification.json').write_text(json.dumps(receipt), encoding='utf-8')
    with pytest.raises(RuntimeError):
        publisher.validate_package(tmp_path)


@pytest.mark.parametrize('scenario', ['success', 'bad_asset', 'existing_public', 'ci_failed', 'wrong_tag', 'resume'])
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

    if scenario in ('success', 'resume'):
        assert publisher.publish(tmp_path, notes, api) == release['html_url']
        assert mutations[-1][2]['draft'] is False
        assert sum('uploads.github.com' in url for url, _, _ in mutations) == (2 if scenario == 'resume' else 3)
    else:
        with pytest.raises(RuntimeError):
            publisher.publish(tmp_path, notes, api)
        assert not any(value and value.get('draft') is False for _, _, value in mutations)
        if scenario in ('ci_failed', 'wrong_tag', 'existing_public'):
            assert mutations == []
