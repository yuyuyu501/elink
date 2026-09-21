"""Publish a verified Windows package from a clean, synced commit with passing CI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from elink import __version__
from scripts.package_windows import ROOT, source_snapshot, validate_self_test

REPO = 'yuyuyu501/elink'
API = f'https://api.github.com/repos/{REPO}'


class GitHub:
    def __init__(self):
        environment = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
        result = subprocess.run(['git', 'credential', 'fill'],
                                input='protocol=https\nhost=github.com\n\n', cwd=ROOT,
                                text=True, capture_output=True, env=environment)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode or not values.get('password'):
            raise RuntimeError('GitHub HTTPS credentials unavailable; configure Git Credential Manager.')
        self.token = values['password']  # Never log or persist credentials.

    def api(self, url, method='GET', value=None, binary=None):
        if urllib.parse.urlsplit(url).hostname not in ('api.github.com', 'uploads.github.com'):
            raise RuntimeError('Unexpected GitHub API host')
        headers = {'Authorization': 'Bearer ' + self.token, 'User-Agent': 'Elink-release',
                   'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        body = None
        if value is not None:
            body = json.dumps(value).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if binary is not None:
            body = binary
            headers['Content-Type'] = 'application/octet-stream'
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()


def validate_package(directory):
    if git('status', '--porcelain'):
        raise RuntimeError('Release requires a clean source worktree.')
    if git('remote', 'get-url', 'origin') not in (f'git@github.com:{REPO}.git',
                                                  f'https://github.com/{REPO}.git',
                                                  f'https://github.com/{REPO}'):
        raise RuntimeError('origin is not the authorized release repository.')
    receipt = json.loads((directory / 'verification.json').read_text(encoding='utf-8'))
    if receipt['version'] != __version__ or receipt['source_dirty'] or receipt['commit'] != git('rev-parse', 'HEAD'):
        raise RuntimeError('Package version/commit does not match the clean current source.')
    if receipt['source_sha256'] != source_snapshot():
        raise RuntimeError('Package source hashes differ from the current worktree.')
    validate_self_test(receipt['self_test'])
    if receipt.get('updater_test', {}).get('ok') is not True:
        raise RuntimeError('Packaged updater test has not passed.')
    archive = directory / f'Elink-{__version__}-windows-x64.zip'
    with archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if receipt['package_sha256'] != digest:
        raise RuntimeError('Package checksum does not match verification.json.')
    if (directory / 'SHA256SUMS.txt').read_text(encoding='ascii') != f'{digest}  {archive.name}\n':
        raise RuntimeError('SHA256SUMS.txt does not match the package.')
    return receipt, (archive, directory / 'SHA256SUMS.txt', directory / 'verification.json')


def ensure_synced_ci(api, revision):
    if api(API + '/commits/main')['sha'] != revision:
        raise RuntimeError('Package commit is not the current remote main commit.')
    runs = api(API + '/actions/workflows/test.yml/runs?head_sha=' + revision + '&per_page=20')['workflow_runs']
    matches = [r for r in runs if r['head_sha'] == revision and r['event'] == 'push']
    if not matches or max(matches, key=lambda r: r['id'])['conclusion'] != 'success':
        raise RuntimeError('Windows CI has not passed for this commit; wait/fix it and retry publishing.')


def existing_release(api, tag):
    # Drafts may not be returned by the by-tag endpoint; paginate authenticated releases.
    for page in range(1, 101):
        items = api(API + f'/releases?per_page=100&page={page}')
        for item in items:
            if item['tag_name'] == tag:
                return item
        if len(items) < 100:
            return None
    raise RuntimeError('Release history exceeded lookup limit.')


def validate_existing_tag(api, tag, revision):
    try:
        reference = api(API + '/git/ref/tags/' + tag)['object']
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise
    for _ in range(5):
        if reference['type'] == 'commit':
            if reference['sha'] != revision:
                raise RuntimeError('Existing version tag points to a different commit; use a new version.')
            return
        if reference['type'] != 'tag':
            break
        reference = api(API + '/git/tags/' + reference['sha'])['object']
    raise RuntimeError('Cannot resolve existing version tag.')


def publish(directory, notes, api=None):
    receipt, files = validate_package(directory)
    body = notes.read_text(encoding='utf-8').strip()
    if not body:
        raise RuntimeError('Release notes are required.')
    api = api or GitHub().api
    ensure_synced_ci(api, receipt['commit'])
    tag = 'v' + __version__
    release = existing_release(api, tag)
    if release is not None and not release['draft']:
        raise RuntimeError('Version already published; refusing to replace it.')
    validate_existing_tag(api, tag, receipt['commit'])
    fields = dict(tag_name=tag, target_commitish=receipt['commit'],
                  name=f'Elink {__version__} Windows Preview', body=body,
                  draft=True, prerelease=True, make_latest='false')
    if release is None:
        release = api(API + '/releases', 'POST', fields)
    else:
        if release['target_commitish'] != receipt['commit']:
            raise RuntimeError('Existing draft was created for another commit; it was not changed.')
        release = api(API + '/releases/' + str(release['id']), 'PATCH', fields)
    print(f"Draft ready: {release['id']}", flush=True)
    if {a['name'] for a in release['assets']} - {p.name for p in files}:
        raise RuntimeError('Draft contains unexpected assets; review it before publishing.')
    for path in files:
        data = path.read_bytes()
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        asset = next((a for a in release['assets'] if a['name'] == path.name), None)
        if asset is None:
            print(f'Uploading {path.name} ({len(data)} bytes)', flush=True)
            url = release['upload_url'].split('{', 1)[0] + '?name=' + urllib.parse.quote(path.name)
            asset = api(url, 'POST', binary=data)
        if asset.get('state') != 'uploaded' or asset['size'] != len(data) or asset.get('digest') != digest:
            raise RuntimeError('Asset verification failed; retaining draft: ' + path.name)
        print(f'Verified {path.name}', flush=True)
    # Recheck before public visibility, including edits made during a long upload.
    validate_package(directory)
    ensure_synced_ci(api, receipt['commit'])
    release = api(API + '/releases/' + str(release['id']), 'PATCH',
                  dict(draft=False, prerelease=True, make_latest='false'))
    if release['draft']:
        raise RuntimeError('GitHub did not publish the draft.')
    print('Published: ' + release['html_url'], flush=True)
    return release['html_url']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--notes', type=Path, required=True)
    args = parser.parse_args()
    try:
        publish(args.package.resolve(), args.notes.resolve())
    except urllib.error.HTTPError as exc:
        print(f'Publication stopped: GitHub HTTP {exc.code}', file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f'Publication stopped: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
