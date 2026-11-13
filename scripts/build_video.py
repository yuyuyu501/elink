"""Build the maintained D3D11 bridge with the installed Windows SDK."""

import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build() -> Path:
    cmake = shutil.which("cmake")
    if cmake is None:
        installer = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        vswhere = installer / "Microsoft Visual Studio/Installer/vswhere.exe"
        installation = subprocess.check_output(
            [str(vswhere), "-latest", "-products", "*", "-requires",
             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
            text=True, timeout=15,
        ).strip()
        cmake = str(Path(installation) / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe")
    destination = ROOT / "out" / "video-build"
    env = dict(os.environ, VSLANG="1033")
    subprocess.run([cmake, "-S", str(ROOT / "native/windows/video"),
                    "-B", str(destination), "-A", "x64"], check=True, env=env)
    subprocess.run([cmake, "--build", str(destination), "--config", "Release"], check=True, env=env)
    library = destination / "Release/elink_video.dll"
    if not library.is_file():
        raise RuntimeError("Native video build did not produce elink_video.dll")
    return library


if __name__ == "__main__":
    print(build())
