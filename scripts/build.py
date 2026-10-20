"""Build the independent Windows app and its generic native libraries."""
import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from elink.processes import native_environment


def main(dist_dir=None):
    project = Path(__file__).resolve().parent.parent
    dist_dir = (project / (dist_dir or 'dist')).resolve()
    build_env = native_environment()
    from scripts.build_video import build
    video_library = build()
    windows = Path(os.environ["SystemRoot"])
    # Developer toolchains can carry incompatible copies of Windows' ICU/UCRT DLLs.
    # Only expose the Python environment and Windows system directories to collection.
    build_env["PATH"] = os.pathsep.join(map(str, (Path(sys.executable).parent, windows / "System32", windows)))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
                    "--name", "Elink", "--onedir", "--distpath", str(dist_dir),
                    "--add-data", "elink/resources;elink/resources",
                    "--collect-data", "cryptography", "--collect-all", "av", "--collect-all", "aiortc",
                    "--collect-all", "pylibsrtp", "--collect-all", "dxcam", "--collect-all", "soundcard",
                    "--collect-all", "sounddevice", "--collect-all", "comtypes", "--hidden-import", "cv2",
                    "--exclude-module", "comtypes.test", "--exclude-module", "pytest",
                    "main.py"], cwd=project, env=build_env, check=True)
    output = dist_dir / "Elink"
    shutil.copy2(video_library, output / 'elink_video.dll')
    for name in ("README.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(project / name, output / name)
    shutil.copytree(project / "docs", output / "docs", dirs_exist_ok=True)
    shutil.copytree(project / "native" / "elinkpad", output / "native" / "elinkpad", dirs_exist_ok=True)
    if (project / "licenses").is_dir():
        shutil.copytree(project / "licenses", output / "licenses", dirs_exist_ok=True)
    for distribution in importlib.metadata.distributions():
        for file in distribution.files or []:
            if any(part.lower() in ("licenses", "license", "copying") for part in file.parts) or file.name.lower().startswith(("license", "copying")):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    destination = output / "licenses" / "python-packages" / distribution.metadata["Name"] / str(file).replace("../", "")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    versions = {name: importlib.metadata.version(name) for name in ("PySide6", "cryptography", "pyinstaller", "aiortc", "av", "aiohttp", "dxcam", "soundcard", "sounddevice", "numpy")}
    import av
    from elink import __version__
    (output / "build-info.json").write_text(json.dumps({"elink": __version__, "python": sys.version,
                                                        "packages": versions, "media_libraries": av.library_versions},
                                                       indent=2), encoding="utf-8")
    print(output / "Elink.exe")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist-dir', type=Path, help='Output directory; defaults to dist.')
    main(parser.parse_args().dist_dir)
