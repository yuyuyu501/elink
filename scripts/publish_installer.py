"""Publish a verified installer; optionally retire explicitly listed legacy installer assets."""
import argparse
import json
from pathlib import Path
import re
import urllib.parse

from scripts.package_installer import (ORIGINAL_NAMES, identity, installer_filenames, published_release,
                                       sha256, verify_original_assets)
from scripts.package_windows import source_snapshot, validate_self_test
from scripts.publish_release import API, REPO, GitHub, ensure_synced_ci, git


def validate_installer(directory, installer_revision=1):
    if git('status', '--porcelain'):
        raise RuntimeError('Installer publication requires clean source.')
    if git('remote', 'get-url', 'origin') not in (f'https://github.com/{REPO}.git', f'https://github.com/{REPO}',
                                                f'git@github.com:{REPO}.git'):
        raise RuntimeError('Unexpected publication repository.')
    report_name = installer_filenames('', installer_revision)[2]
    report = json.loads((directory / report_name).read_text(encoding='utf-8'))
    if report.get('installer_revision', 1) != installer_revision:
        raise RuntimeError('Installer revision differs from the verified build.')
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
                'unrelated_startup_preserved', 'unicode_path', 'unsafe_directories_rejected')
    if any(checks.get(key) is not True for key in required):
        raise RuntimeError('Actual installer lifecycle verification did not pass.')
    validate_self_test(checks['self_test'])
    if any(checks.get('zip_update', {}).get(key) is not True for key in
           ('ok', 'directory_replacement', 'restart_confirmed', 'configuration_preserved')):
        raise RuntimeError('Installed application ZIP updater did not pass.')
    setup_name, checksum_name, _ = installer_filenames(version, installer_revision)
    if installer_revision >= 2 and checks.get('case_insensitive_path') is not True:
        raise RuntimeError('Case-insensitive installed path verification did not pass.')
    setup = directory / setup_name
    digest = sha256(setup)
    if report['package_sha256'] != digest or report['package_size'] != setup.stat().st_size:
        raise RuntimeError('Installer bytes differ from the verified build.')
    if (directory / checksum_name).read_text(encoding='ascii') != f'{digest}  {setup.name}\n':
        raise RuntimeError('Installer checksum file mismatch.')
    return report, (setup, directory / checksum_name, directory / report_name)


def retirement_assets(plan, version):
    if plan is None:
        return []
    if plan.get('version') != version or not isinstance(plan.get('assets'), list) or not plan['assets']:
        raise RuntimeError('Retirement plan must identify this version and explicit legacy assets.')
    patterns = (rf'Elink-{re.escape(version)}-windows-x64-Setup(?:-r[1-9][0-9]*)?\.exe',
                r'Setup(?:-r[1-9][0-9]*)?-SHA256SUMS\.txt', r'setup(?:-r[1-9][0-9]*)?-verification\.json')
    seen = set()
    for asset in plan['assets']:
        if (not any(re.fullmatch(pattern, asset.get('name', '')) for pattern in patterns)
                or asset['name'] in seen or type(asset.get('id')) is not int or asset['id'] <= 0
                or type(asset.get('size')) is not int or asset['size'] <= 0
                or asset.get('state') != 'uploaded'
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest', ''))):
            raise RuntimeError('Retirement is limited to pinned legacy installer assets, never portable assets.')
        seen.add(asset['name'])
    return [identity(a) for a in plan['assets']]


def verify_retirement_assets(release, retired):
    for expected in retired:
        actual = next((identity(a) for a in release['assets'] if a['name'] == expected['name']), None)
        if actual is not None and actual != expected:
            raise RuntimeError('Legacy installer changed since retirement was reviewed: ' + expected['name'])


def verify_uploads(release, files):
    for path in files:
        asset = next((a for a in release['assets'] if a['name'] == path.name), {})
        if (asset.get('state') != 'uploaded' or asset.get('size') != path.stat().st_size
                or asset.get('digest') != 'sha256:' + sha256(path)):
            raise RuntimeError('Final installer asset verification failed: ' + path.name)


def publish(directory, api=None, *, installer_revision=1, retire=None):
    report, files = validate_installer(directory, installer_revision)
    api = api or GitHub().api
    ensure_synced_ci(api, report['packaging_commit'])
    release = published_release(api, report['version'], report['payload_commit'])
    verify_original_assets(release, report['original_assets'])
    retired = retirement_assets(retire, report['version'])
    verify_retirement_assets(release, retired)
    previous = [identity(a) for a in release['assets']]
    retained = [a for a in previous if a['name'] not in {r['name'] for r in retired}]
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
    validate_installer(directory, installer_revision)
    ensure_synced_ci(api, report['packaging_commit'])
    after = published_release(api, report['version'], report['payload_commit'])
    if any(after.get(key) != value for key, value in protected.items()):
        raise RuntimeError('Published release metadata changed during additive upload.')
    verify_original_assets(after, previous)
    verify_uploads(after, files)
    # Only retire the reviewed legacy assets after all replacement downloads are verified.
    verify_retirement_assets(after, retired)
    for asset in retired:
        current = published_release(api, report['version'], report['payload_commit'])
        if any(current.get(key) != value for key, value in protected.items()):
            raise RuntimeError('Release metadata changed before retirement.')
        verify_original_assets(current, retained)
        verify_uploads(current, files)
        verify_retirement_assets(current, retired)
        if any(a['id'] == asset['id'] for a in current['assets']):
            api(API + '/releases/assets/' + str(asset['id']), 'DELETE')
            print('Retired ' + asset['name'], flush=True)
    if retired:
        after = published_release(api, report['version'], report['payload_commit'])
        if any(after.get(key) != value for key, value in protected.items()):
            raise RuntimeError('Release metadata changed during retirement.')
        verify_original_assets(after, retained)
        verify_uploads(after, files)
        if any(a['name'] in {r['name'] for r in retired} for a in after['assets']):
            raise RuntimeError('Legacy installer retirement incomplete; retry the same reviewed plan.')
    print('Installer added: ' + after['html_url'], flush=True)
    return after['html_url']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path, help='Verified installer output directory.')
    parser.add_argument('--installer-revision', type=int, default=1)
    parser.add_argument('--retire-assets', type=Path,
                        help='User-authorized JSON plan with version and exact legacy asset identities.')
    args = parser.parse_args()
    retire = json.loads(args.retire_assets.read_text(encoding='utf-8')) if args.retire_assets else None
    publish(args.package.resolve(), installer_revision=args.installer_revision, retire=retire)


if __name__ == '__main__':
    main()
