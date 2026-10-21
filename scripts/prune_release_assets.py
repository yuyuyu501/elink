"""Remove obsolete 0.4.x attachments while retaining release records and tags."""
import argparse
import json
from pathlib import Path
import re

from scripts.publish_release import API, GitHub


def obsolete(release):
    match = re.fullmatch(r'v?0\.4\.(0|[1-9][0-9]*)', release.get('tag_name', ''))
    return bool(match and int(match[1]) != 37)


def releases(api):
    for page in range(1, 101):
        items = api(API + f'/releases?per_page=100&page={page}')
        yield from items
        if len(items) < 100:
            return
    raise RuntimeError('Release pagination limit exceeded')


def prune(api, apply=False):
    before = list(releases(api))
    preserved = {r['id']: {a['id'] for a in r['assets']} for r in before if not obsolete(r)}
    plan = [dict(tag=r['tag_name'], release_id=r['id'], id=a['id'], name=a['name'], size=a['size'])
            for r in before if obsolete(r) for a in r['assets']]
    if apply:
        for asset in plan:
            api(API + f"/releases/assets/{asset['id']}", method='DELETE')
            print(f"Removed {asset['tag']}: {asset['name']}", flush=True)
        after = list(releases(api))
        if {r['id'] for r in before} != {r['id'] for r in after}:
            raise RuntimeError('Release records changed during cleanup')
        for release in after:
            if obsolete(release) and release['assets']:
                raise RuntimeError('Obsolete attachments remain')
            if release['id'] in preserved and {a['id'] for a in release['assets']} != preserved[release['id']]:
                raise RuntimeError('Preserved release assets changed')
    return dict(applied=apply, count=len(plan), bytes=sum(a['size'] for a in plan), assets=plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    receipt = prune(GitHub().api, args.apply)
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in receipt.items() if k != 'assets'}))


if __name__ == '__main__':
    main()
