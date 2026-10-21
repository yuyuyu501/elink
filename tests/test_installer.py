import hashlib
import json
import urllib.parse

import pytest

from scripts import package_installer as package
from scripts import publish_installer as publisher
from tests.test_release import package_fixture


def test_payload_requires_original_verified_bytes(tmp_path, monkeypatch):
    _, receipt, archive = package_fixture(tmp_path, monkeypatch)
    receipt['commit'] = 'a' * 40
    (tmp_path / 'verification.json').write_text(json.dumps(receipt), encoding='utf-8')
    validated, files = package.validate_payload(tmp_path)
    assert validated['version'] == receipt['version'] and files[0] == archive
    archive.write_bytes(b'different application with the same version')
    with pytest.raises(RuntimeError, match='digest mismatch'):
        package.validate_payload(tmp_path)


@pytest.mark.parametrize('changed', ['zip', 'receipt', 'checksum', None])
def test_payload_is_anchored_to_all_existing_github_assets(tmp_path, monkeypatch, changed):
    _, receipt, archive = package_fixture(tmp_path, monkeypatch)
    receipt['commit'] = 'a' * 40
    (tmp_path / 'verification.json').write_text(json.dumps(receipt), encoding='utf-8')
    _, files = package.validate_payload(tmp_path)
    assets = [dict(id=i, name=p.name, size=p.stat().st_size, state='uploaded', digest='sha256:' + package.sha256(p))
              for i, p in enumerate(files)]
    if changed:
        assets[{'zip': 0, 'receipt': 2, 'checksum': 1}[changed]]['digest'] = 'sha256:' + '0' * 64
    release = dict(tag_name='v' + receipt['version'], draft=False, assets=assets)
    def api(url, *_):
        if '/git/ref/' in url:
            return {'object': dict(type='commit', sha=receipt['commit'])}
        return release
    if changed:
        with pytest.raises(RuntimeError, match='differs from published'):
            package.verify_payload_publication(api, receipt, files)
    else:
        assert package.verify_payload_publication(api, receipt, files) == assets


def installer_fixture(tmp_path, monkeypatch, installer_revision=1):
    revision, payload = 'b' * 40, 'a' * 40
    monkeypatch.setattr(publisher, 'git', lambda *args: {
        ('status', '--porcelain'): '', ('rev-parse', 'HEAD'): revision,
        ('remote', 'get-url', 'origin'): 'https://github.com/yuyuyu501/elink.git',
    }[args])
    monkeypatch.setattr(publisher, 'source_snapshot', lambda: {'script': 'hash'})
    setup_name, checksum_name, report_name = package.installer_filenames('0.4.37', installer_revision)
    setup = tmp_path / setup_name
    setup.write_bytes(b'verified installer')
    originals = [dict(id=i, name=name, size=100, state='uploaded', digest='sha256:' + str(i) * 64)
                 for i, name in enumerate(('Elink-0.4.37-windows-x64.zip', *package.ORIGINAL_NAMES))]
    checks = dict.fromkeys(('ok', 'offline_payload_identical', 'current_user_install', 'shortcut', 'repair',
                           'uninstaller_preserved', 'uninstall', 'added_files_removed', 'configuration_preserved',
                           'unrelated_startup_preserved', 'unicode_path'), True)
    checks['self_test'] = dict(ok=True, runtime_stopped=True, audio_frames=10, video_frames=90)
    checks['zip_update'] = dict(ok=True, directory_replacement=True, restart_confirmed=True, configuration_preserved=True)
    checks['case_insensitive_path'] = True
    checks['unsafe_directories_rejected'] = True
    report = dict(version='0.4.37', format='offline-inno-setup', packaging_commit=revision, payload_commit=payload,
                  installer_revision=installer_revision,
                  source_sha256={'script': 'hash'}, payload_files=10, original_assets=originals,
                  installer_test=checks, package_sha256=package.sha256(setup), package_size=setup.stat().st_size)
    (tmp_path / report_name).write_text(json.dumps(report), encoding='utf-8')
    (tmp_path / checksum_name).write_text(f'{package.sha256(setup)}  {setup.name}\n', encoding='ascii')
    return report, setup


@pytest.mark.parametrize('problem', ['self_test', 'update', 'repair', 'uninstall', 'source', 'bytes', 'originals',
                                   'unsafe_directories_rejected'])
def test_installer_cannot_publish_without_lifecycle_and_provenance(tmp_path, monkeypatch, problem):
    report, setup = installer_fixture(tmp_path, monkeypatch)
    if problem == 'self_test':
        report['installer_test']['self_test']['ok'] = False
    elif problem == 'update':
        report['installer_test']['zip_update']['restart_confirmed'] = False
    elif problem in ('repair', 'uninstall', 'unsafe_directories_rejected'):
        report['installer_test'][problem] = False
    elif problem == 'source':
        report['source_sha256'] = {}
    elif problem == 'bytes':
        setup.write_bytes(b'not the verified installer')
    else:
        report['original_assets'] = []
    (tmp_path / 'Setup.verification.json').write_text(json.dumps(report), encoding='utf-8')
    with pytest.raises(RuntimeError):
        publisher.validate_installer(tmp_path)


@pytest.mark.parametrize('installer_revision', [1, 2])
@pytest.mark.parametrize('retire', [False, True])
@pytest.mark.parametrize('scenario', ['success', 'resume', 'collision', 'wrong_tag', 'original_changed', 'ci_failed',
                                   'bad_upload'])
def test_installer_upload_protects_payload_and_retires_only_after_verification(
        tmp_path, monkeypatch, scenario, installer_revision, retire):
    report, setup = installer_fixture(tmp_path, monkeypatch, installer_revision)
    mutations = []
    release = dict(id=42, tag_name='v0.4.37', draft=False, prerelease=True, body='existing release notes',
                   assets=list(report['original_assets']), html_url='https://github.com/yuyuyu501/elink/releases/tag/v0.4.37',
                   upload_url='https://uploads.github.com/repos/yuyuyu501/elink/releases/42/assets{?name}')
    legacy = dict(id=77, name='Elink-0.4.37-windows-x64-Setup.exe', size=10,
                  state='uploaded', digest='sha256:' + 'f' * 64)
    release['assets'].append(legacy)
    plan = dict(version='0.4.37', assets=[dict(legacy)]) if retire else None
    before = [dict(a) for a in release['assets']]
    if scenario == 'original_changed':
        release['assets'] = [dict(a, size=1) for a in release['assets']]
    if scenario in ('resume', 'collision'):
        release['assets'].append(dict(name=setup.name, id=99, state='uploaded', size=setup.stat().st_size,
                                      digest='sha256:' + ('0' * 64 if scenario == 'collision' else package.sha256(setup))))
    def api(url, method='GET', value=None, binary=None):
        if method != 'GET':
            mutations.append((url, method, value))
        if '/commits/main' in url:
            return {'sha': report['packaging_commit']}
        if '/actions/' in url:
            return {'workflow_runs': [dict(id=1, head_sha=report['packaging_commit'], event='push',
                                          conclusion='failure' if scenario == 'ci_failed' else 'success')]}
        if '/git/ref/' in url:
            return {'object': dict(type='commit', sha='c' * 40 if scenario == 'wrong_tag' else report['payload_commit'])}
        if method == 'DELETE':
            assert url.endswith('/releases/assets/77')
            for path in (setup, tmp_path / 'Setup.sha256', tmp_path / 'Setup.verification.json'):
                assert any(a['name'] == path.name and a['digest'] == 'sha256:' + package.sha256(path)
                           for a in release['assets'])
            release['assets'].remove(legacy)
            return None
        if 'uploads.github.com' in url:
            assert method == 'POST' and value is None
            name = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['name'][0]
            assert name not in {a['name'] for a in release['assets']}
            asset = dict(id=100 + len(mutations), name=name, state='uploaded', size=len(binary),
                         digest='sha256:' + ('0' * 64 if scenario == 'bad_upload' else hashlib.sha256(binary).hexdigest()))
            release['assets'].append(asset)
            return asset
        assert method == 'GET'
        return release
    if scenario in ('success', 'resume'):
        assert publisher.publish(tmp_path, api, installer_revision=installer_revision, retire=plan) == release['html_url']
        assert len(mutations) == (2 if scenario == 'resume' else 3) + int(retire)
        assert all(a in release['assets'] for a in before if not retire or a != legacy)
        assert (legacy in release['assets']) is not retire
        assert release['body'] == 'existing release notes' and release['draft'] is False
    else:
        with pytest.raises(RuntimeError):
            publisher.publish(tmp_path, api, installer_revision=installer_revision, retire=plan)
        assert not any(method == 'DELETE' for _, method, _ in mutations)
        assert mutations == [] or scenario == 'bad_upload'


def test_revised_installer_requires_real_case_variant_repair_test(tmp_path, monkeypatch):
    report, _ = installer_fixture(tmp_path, monkeypatch, 2)
    report['installer_test']['case_insensitive_path'] = False
    (tmp_path / 'Setup.verification.json').write_text(json.dumps(report), encoding='utf-8')
    with pytest.raises(RuntimeError, match='Case-insensitive'):
        publisher.validate_installer(tmp_path, 2)


@pytest.mark.parametrize('name', ['Elink-0.4.37-windows-x64.zip', 'SHA256SUMS.txt', 'verification.json',
                                 'Setup.exe', 'Elink-0.4.36-windows-x64-Setup.exe'])
def test_retirement_cannot_delete_portable_or_other_version_assets(name):
    asset = dict(id=42, name=name, size=10, state='uploaded', digest='sha256:' + 'f' * 64)
    with pytest.raises(RuntimeError, match='limited to pinned legacy'):
        publisher.retirement_assets(dict(version='0.4.37', assets=[asset]), '0.4.37')


def test_retirement_refuses_changed_assets_and_allows_resume_after_removal():
    asset = dict(id=42, name='setup-r2-verification.json', size=10, state='uploaded', digest='sha256:' + 'f' * 64)
    with pytest.raises(RuntimeError, match='changed since'):
        publisher.verify_retirement_assets(dict(assets=[dict(asset, id=43)]), [asset])
    publisher.verify_retirement_assets(dict(assets=[]), [asset])
