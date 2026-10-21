"""Wrap an already-published verified portable ZIP in a full offline Inno Setup installer."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

from elink.updates import extract_verified
from scripts.package_windows import ROOT, source_snapshot, validate_gpu_test, validate_self_test
from scripts.publish_release import API, GitHub, git, validate_existing_tag


ORIGINAL_NAMES = ('SHA256SUMS.txt', 'verification.json')


def installer_filenames(version, revision=1):
    if type(revision) is not int or not 1 <= revision <= 65535:
        raise ValueError('Installer revision must be an integer from 1 to 65535.')
    suffix = f'-r{revision}' if revision > 1 else ''
    return (f'Elink-{version}-windows-x64-Setup{suffix}.exe',
            f'Setup{suffix}-SHA256SUMS.txt', f'setup{suffix}-verification.json')


def sha256(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def identity(asset):
    return {key: asset.get(key) for key in ('id', 'name', 'size', 'digest', 'state')}


def file_manifest(root, *, exclude_app=False):
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob('*'))
            if p.is_file() and (not exclude_app or len(p.relative_to(root).parts) == 1)}


def validate_payload(directory):
    receipt = json.loads((directory / 'verification.json').read_text(encoding='utf-8'))
    version = receipt['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or not re.fullmatch(r'[0-9a-f]{40}', receipt['commit']):
        raise RuntimeError('Invalid portable version/commit.')
    if receipt.get('source_dirty') is not False or receipt.get('updater_test', {}).get('ok') is not True:
        raise RuntimeError('Portable package was not verified from clean source.')
    validate_self_test(receipt['self_test'])
    validate_gpu_test(receipt['gpu_test'])
    archive = directory / f'Elink-{version}-windows-x64.zip'
    digest = sha256(archive)
    if receipt['package_sha256'] != digest:
        raise RuntimeError('Original portable ZIP digest mismatch.')
    if (directory / 'SHA256SUMS.txt').read_text(encoding='ascii') != f'{digest}  {archive.name}\n':
        raise RuntimeError('Original portable checksum file mismatch.')
    return receipt, (archive, *(directory / name for name in ORIGINAL_NAMES))


def published_release(api, version, revision):
    tag = 'v' + version
    release = api(API + '/releases/tags/' + tag)
    if release['draft'] or release['tag_name'] != tag:
        raise RuntimeError('Installer requires an existing published release.')
    # Unlike new-version publication, a missing tag is an error for an additive asset.
    api(API + '/git/ref/tags/' + tag)
    validate_existing_tag(api, tag, revision)
    return release


def verify_original_assets(release, originals):
    assets = {a['name']: identity(a) for a in release['assets']}
    for asset in originals:
        if asset['name'] not in assets or assets[asset['name']] != asset:
            raise RuntimeError('Original published asset changed: ' + asset['name'])


def verify_payload_publication(api, receipt, files):
    release = published_release(api, receipt['version'], receipt['commit'])
    originals = []
    for path in files:
        asset = next((a for a in release['assets'] if a['name'] == path.name), None)
        if (asset is None or asset['state'] != 'uploaded' or asset['size'] != path.stat().st_size
                or asset.get('digest') != 'sha256:' + sha256(path)):
            raise RuntimeError('Local payload differs from published asset: ' + path.name)
        originals.append(identity(asset))
    return originals


def package(directory, compiler, output=None, api=None, *, installer_revision=1):
    if os.name != 'nt':
        raise RuntimeError('Build and verify installers on Windows.')
    if git('status', '--porcelain'):
        raise RuntimeError('Installer packaging requires clean, committed source.')
    snapshot, revision = source_snapshot(), git('rev-parse', 'HEAD')
    receipt, files = validate_payload(directory)
    setup_name, checksum_name, report_name = installer_filenames(receipt['version'], installer_revision)
    originals = verify_payload_publication(api or GitHub().api, receipt, files)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = (output or ROOT / 'dist/installers') / ('v' + receipt['version']) / stamp
    destination.mkdir(parents=True, exist_ok=False)
    artifacts = ROOT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    # Keep diagnostics if an actual installation or updater test fails.
    work = Path(tempfile.mkdtemp(prefix='installer-check-', dir=artifacts))
    bundle = extract_verified(files[0], work / 'payload', receipt['version'], threading.Event())
    manifest = file_manifest(bundle)
    script = ROOT / 'packaging/windows/elink.iss'
    subprocess.run([str(compiler), '/Qp', f'/DAppVersion={receipt["version"]}',
                    f'/DInstallerRevision={installer_revision}', f'/DPayloadDir={bundle}',
                    f'/DOutputDir={destination}', str(script)], check=True, cwd=ROOT, timeout=600)
    setup = destination / setup_name
    from scripts.test_windows_installer import verify_installer
    print('Verifying actual installation/update/uninstall: ' + str(work), flush=True)
    verification = verify_installer(setup, bundle, work / 'installation', receipt['version'], manifest)
    if source_snapshot() != snapshot or git('rev-parse', 'HEAD') != revision or git('status', '--porcelain'):
        raise RuntimeError('Packaging source changed during installer verification.')
    digest = sha256(setup)
    report = dict(format='offline-inno-setup', version=receipt['version'], built_at=stamp,
                  installer_revision=installer_revision,
                  payload_commit=receipt['commit'], packaging_commit=revision, source_sha256=snapshot,
                  original_assets=originals, package_sha256=digest, package_size=setup.stat().st_size,
                  payload_files=len(manifest), compiler_sha256=sha256(compiler), installer_test=verification,
                  signed=False, channel='preview', manual_acceptance='pending')
    (destination / report_name).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    (destination / checksum_name).write_text(f'{digest}  {setup.name}\n', encoding='ascii')
    print('Verified offline installer: ' + str(destination), flush=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path, help='Original published portable ZIP/receipt directory.')
    parser.add_argument('--compiler', required=True, type=Path, help='Trusted Inno Setup 6.7+ ISCC.exe.')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--installer-revision', type=int, default=1, help='Use a new revision for installer-only fixes.')
    args = parser.parse_args()
    try:
        package(args.package.resolve(), args.compiler.resolve(), args.output.resolve() if args.output else None,
                installer_revision=args.installer_revision)
    except Exception as exc:
        print(f'Installer packaging stopped: {exc}', file=sys.stderr)
        raise


if __name__ == '__main__':
    main()
