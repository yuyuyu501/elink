"""Test reviewed staged changes, then commit and push. Never builds a release."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()


def reviewed_state():
    if git('diff', '--name-only') or git('ls-files', '--others', '--exclude-standard'):
        raise RuntimeError('Review and stage task changes first; unstaged/untracked source remains.')
    return git('rev-parse', 'HEAD'), git('write-tree')


def synchronize(message):
    before = reviewed_state()
    branch = git('symbolic-ref', '--quiet', '--short', 'HEAD')
    remote = git('config', f'branch.{branch}.remote')
    target = git('config', f'branch.{branch}.merge')
    if remote == '.' or not target.startswith('refs/heads/'):
        raise RuntimeError('A remote branch upstream is required.')
    changed = bool(git('diff', '--cached', '--name-only'))
    if changed and not message:
        raise RuntimeError('--message is required for staged changes.')
    subprocess.run([sys.executable, '-m', 'pytest', '-q'], cwd=ROOT, check=True)
    if reviewed_state() != before:
        raise RuntimeError('Source/index changed while testing. Review and rerun before syncing.')
    subprocess.run(['git', 'fetch', remote, target], cwd=ROOT, check=True)
    if subprocess.run(['git', 'merge-base', '--is-ancestor', 'FETCH_HEAD', 'HEAD'], cwd=ROOT).returncode:
        raise RuntimeError('Remote has new commits. Integrate/review them and rerun tests; no push performed.')
    if reviewed_state() != before:
        raise RuntimeError('Source/index changed during fetch. Review and rerun before syncing.')
    if changed:
        subprocess.run(['git', 'commit', '-m', message], cwd=ROOT, check=True)
    subprocess.run(['git', 'push', remote, f'HEAD:{target}'], cwd=ROOT, check=True)
    print(f'Tests passed; synchronized {git("rev-parse", "--short", "HEAD")} to {remote}/{target.removeprefix("refs/heads/")}.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--message', help='Commit message for the reviewed staged changes')
    args = parser.parse_args()
    try:
        synchronize(args.message)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Sync stopped: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
