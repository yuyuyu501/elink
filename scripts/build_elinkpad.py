"""Compile and validate the experimental driver without installing system components."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "10.0.26100.6584"
SHA256 = "c393d03dfb640b5c92f546b32f6770ef68cd3aaf691956e7d66d8e2c28a1b55e"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-wdk", action="store_true", help="Fetch pinned Microsoft WDK into .artifacts only")
    args = parser.parse_args()
    wdk = ROOT / ".artifacts" / "toolchains" / "wdk-26100.6584" / "c"
    if not wdk.is_dir():
        if not args.fetch_wdk:
            parser.error("WDK not found; run again with --fetch-wdk")
        package = ROOT / ".artifacts" / "downloads" / "wdk-x64-26100.6584.nupkg"
        package.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(f"https://api.nuget.org/v3-flatcontainer/microsoft.windows.wdk.x64/{VERSION}/microsoft.windows.wdk.x64.{VERSION}.nupkg", package)
        with package.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != SHA256:
            raise RuntimeError("WDK package hash mismatch")
        with zipfile.ZipFile(package) as archive:
            archive.extractall(wdk.parent)
    program_files = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    vswhere = program_files / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    install = subprocess.check_output([str(vswhere), "-latest", "-products", "*", "-requires",
                                       "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], text=True).strip()
    if not install:
        raise RuntimeError("Visual Studio C++ Build Tools are required")
    vc = Path(install) / "VC" / "Tools" / "MSVC"
    vc = sorted(vc.iterdir(), key=lambda p: tuple(map(int, p.name.split("."))))[-1]
    sdk = program_files / "Windows Kits" / "10"
    sdk_version = "10.0.26100.0"
    includes = [vc / "include"] + [sdk / "Include" / sdk_version / name for name in ("ucrt", "shared", "um")]
    includes += [wdk / "Include" / sdk_version / "km", wdk / "Include" / "wdf" / "umdf" / "2.15"]
    libs = [vc / "lib" / "x64"] + [sdk / "Lib" / sdk_version / name / "x64" for name in ("ucrt", "um")]
    libs += [wdk / "Lib" / "wdf" / "umdf" / "x64" / "2.15"]
    env = os.environ.copy()
    env["INCLUDE"] = os.pathsep.join(map(str, includes))
    env["LIB"] = os.pathsep.join(map(str, libs))
    compiler = vc / "bin" / "Hostx64" / "x64"
    env["PATH"] = str(compiler) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32")
    output = ROOT / "build" / "elinkpad"
    output.mkdir(parents=True, exist_ok=True)
    source = ROOT / "native" / "elinkpad"

    def run(command):
        subprocess.run(list(map(str, command)), cwd=output, env=env, check=True)

    common = [compiler / "cl.exe", "/nologo", "/W4", "/WX", "/wd4505", "/GS", "/sdl", "/MT", "/guard:cf", "/D_AMD64_", "/D_WIN64", "/DUNICODE", "/D_UNICODE"]
    run(common + [source / "test_protocol.c", "/Fe:test_protocol.exe"])
    run([output / "test_protocol.exe"])
    run(common + [source / "device.c", "/Fe:ElinkPadDevice.exe", "/link", "Swdevice.lib", "ole32.lib", "/DYNAMICBASE", "/NXCOMPAT", "/guard:cf"])
    run(common + ["/wd4324", "/DUMDF_VERSION_MAJOR=2", "/DUMDF_VERSION_MINOR=15", "/c", source / "driver.c"])
    run([compiler / "link.exe", "/nologo", "/DLL", "/OUT:ElinkPad.dll", "/DYNAMICBASE", "/NXCOMPAT", "/guard:cf",
         "driver.obj", "WdfDriverStubUm.lib", "ntdll.lib", "OneCoreUAP.lib", "mincore.lib", "advapi32.lib"])
    package = output / "package"
    package.mkdir(exist_ok=True)
    for name, location in [("elinkpad.inf", source), ("ElinkPad.dll", output)]:
        shutil.copy2(location / name, package / name)
    run([wdk / "tools" / sdk_version / "x64" / "infverif.exe", "/u", package / "elinkpad.inf"])
    run([wdk / "bin" / sdk_version / "x86" / "Inf2Cat.exe", f"/driver:{package}", "/os:10_X64", "/uselocaltime"])
    for name in ("README.md",):
        shutil.copy2(source / name, output / name)
    shutil.copy2(ROOT / "licenses" / "HIDMaestro-LICENSE.txt", output / "HIDMaestro-LICENSE.txt")
    manifest = {"version": "0.4.0-experimental", "installed": False, "signed": False,
                "wdk_package": VERSION, "wdk_sha256": SHA256,
                "files": {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in [package / "elinkpad.inf", package / "ElinkPad.dll", package / "elinkpad.cat", output / "ElinkPadDevice.exe"]}}
    (output / "build-info.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    destination = ROOT / "dist" / "ElinkPad-0.4.0-experimental-windows-x64.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.iterdir()):
            archive.write(path, "package/" + path.name)
        for name in ("README.md", "HIDMaestro-LICENSE.txt", "ElinkPadDevice.exe", "build-info.json", "test_protocol.exe"):
            archive.write(output / name, name)
        for path in sorted(source.iterdir()):
            if path.is_file():
                archive.write(path, "native/elinkpad/" + path.name)
        archive.write(ROOT / "scripts" / "build_elinkpad.py", "scripts/build_elinkpad.py")
        archive.write(ROOT / "scripts" / "test_elinkpad_live.py", "scripts/test_elinkpad_live.py")
        archive.write(ROOT / "licenses" / "HIDMaestro-LICENSE.txt", "licenses/HIDMaestro-LICENSE.txt")
    print(destination)
    print(f"Unsigned experimental package: {package}. No driver or certificate installed.")


if __name__ == "__main__":
    main()
