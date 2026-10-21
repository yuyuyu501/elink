"""Source identity and redistributable dependency notices for platform packages."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess

from scripts.package_windows import ROOT, source_snapshot


def source_identity(manifest=None):
    if manifest:
        identity = json.loads(Path(manifest).read_text(encoding='utf-8'))
        for name, digest in identity['source_sha256'].items():
            path = (ROOT / name).resolve()
            if not path.is_relative_to(ROOT) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError('Build source differs from manifest: ' + name)
        if identity.get('source_dirty') or len(identity['commit']) != 40:
            raise RuntimeError('A synchronized source manifest is required')
        return identity
    return dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                source_dirty=bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
                source_sha256=source_snapshot())


def copy_python_licenses(output):
    for distribution in importlib.metadata.distributions():
        for file in distribution.files or []:
            if (any(part.lower() in ('licenses', 'license', 'copying') for part in file.parts)
                    or file.name.lower().startswith(('license', 'copying', 'notice'))):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    destination = output / distribution.metadata['Name'] / str(file).replace('../', '')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
