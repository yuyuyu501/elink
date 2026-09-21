"""Default update workflow: test/sync -> package/self-test -> publish preview Release."""
import argparse
from pathlib import Path
import subprocess
import sys
import urllib.error

from scripts.check_and_sync import synchronize
from scripts.package_windows import package
from scripts.publish_release import publish


def release(message, notes):
    if not notes.is_file() or not notes.read_text(encoding='utf-8').strip():
        raise RuntimeError('Write and review the release notes before starting.')
    synchronize(message)
    directory = package()
    return publish(directory, notes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--message', required=True)
    parser.add_argument('--notes', type=Path, required=True)
    args = parser.parse_args()
    try:
        release(args.message, args.notes.resolve())
    except urllib.error.HTTPError as exc:
        print(f'Release stopped: GitHub HTTP {exc.code}', file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f'Release stopped: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
