"""Add a verified offline installer to an existing release without replacing any asset."""
import argparse
import json
from pathlib import Path
import re
import urllib.parse

from scripts.package_installer import ORIGINAL_NAMES, identity, published_release, sha256, verify_original_assets
from scripts.package_windows import source_snapshot, validate_self_test
from scripts.publish_release import API, REPO, GitHub, ensure_synced_ci, git


def validate_installer(directory):
    if git('status', '--porcelain'):
        raise RuntimeError('Installer publication requires clean source.')
    if git('remote', 'get-url', 'origin') not in (f'https://github.com/{REPO}.git', f'https://github.com/{REPO}',
                                                f'git@github.com:{REPO}.git'):
        raise RuntimeError('Unexpected publication repository.')
    report = json.loads((directory / 'setup-verification.json').read_text(encoding='utf-8'))
    if report['packaging_commit'] != git('rev-parse', 'HEAD') or report['source_sha256'] != source_snapshot():
        raise RuntimeError('Installer build tooling differs from the clean current source.')
    version = report['version']
    if (not re.fullmatch(r'\d+\.\d+\.\d+', version)
            or not re.fullmatch(r'[0-9a-f]{40}', report['payload_commit'])):
        raise RuntimeError('Invalid installer version/payload commit.')
    if report.get('format') != 'offline-inno-setup' or not report.get('payload_files', 0) > 0:
        raise RuntimeError('Missing offline installer provenance.')
    names = {f'Elink-{version}-windows-x64.zip', *ORIGINAL_NAMES}
    if {a['name'] for a in report['original_assets']} != names or len(report['original_assets']) != 3:
        raise RuntimeError('Original portable assets are not fully recorded.')
    checks = report['installer_test']
    required = ('ok', 'offline_payload_identical', 'current_user_install', 'shortcut', 'repair',
                'uninstaller_preserved', 'uninstall', 'added_files_removed', 'configuration_preserved',
                'unrelated_startup_preserved', 'unicode_path')
    if any(checks.get(key) is not True for key in required):
        raise RuntimeError('Actual installer lifecycle verification did not pass.')
    validate_self_test(checks['self_test'])
    if any(checks.get('zip_update', {}).get(key) is not True for key in
           ('ok', 'directory_replacement', 'restart_confirmed', 'configuration_preserved')):
        raise RuntimeError('Installed application ZIP updater did not pass.')
    setup = directory / f'Elink-{version}-windows-x64-Setup.exe'
    digest = sha256(setup)
    if report['package_sha256'] != digest or report['package_size'] != setup.stat().st_size:
        raise RuntimeError('Installer bytes differ from the verified build.')
    if (directory / 'Setup-SHA256SUMS.txt').read_text(encoding='ascii') != f'{digest}  {setup.name}\n':
        raise RuntimeError('Installer checksum file mismatch.')
    return report, (setup, directory / 'Setup-SHA256SUMS.txt', directory / 'setup-verification.json')


def publish(directory, api=None):
    report, files = validate_installer(directory)
    api = api or GitHub().api
    ensure_synced_ci(api, report['packaging_commit'])
    release = published_release(api, report['version'], report['payload_commit'])
    verify_original_assets(release, report['original_assets'])
    previous = [identity(a) for a in release['assets']]
    protected = {key: release.get(key) for key in
                 ('id', 'tag_name', 'target_commitish', 'name', 'body', 'draft', 'prerelease', 'published_at')}
    for path in files:
        data = path.read_bytes()
        digest = 'sha256:' + sha256(path)
        asset = next((a for a in release['assets'] if a['name'] == path.name), None)
        if asset is None:
            print(f'Adding {path.name} ({len(data)} bytes)', flush=True)
            url = release['upload_url'].split('{', 1)[0] + '?name=' + urllib.parse.quote(path.name)
            asset = api(url, 'POST', binary=data)
        if asset.get('state') != 'uploaded' or asset.get('size') != len(data) or asset.get('digest') != digest:
            raise RuntimeError('Installer asset differs; nothing was deleted or replaced: ' + path.name)
        print('Verified ' + path.name, flush=True)
    validate_installer(directory)
    ensure_synced_ci(api, report['packaging_commit'])
    after = published_release(api, report['version'], report['payload_commit'])
    if any(after.get(key) != value for key, value in protected.items()):
        raise RuntimeError('Published release metadata changed during additive upload.')
    verify_original_assets(after, previous)
    for path in files:
        asset = next((a for a in after['assets'] if a['name'] == path.name), {})
        if (asset.get('state') != 'uploaded' or asset.get('size') != path.stat().st_size
                or asset.get('digest') != 'sha256:' + sha256(path)):
            raise RuntimeError('Final installer asset verification failed: ' + path.name)
    print('Installer added: ' + after['html_url'], flush=True)
    return after['html_url']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path, help='Verified installer output directory.')
    args = parser.parse_args()
    publish(args.package.resolve())


if __name__ == '__main__':
    main()
