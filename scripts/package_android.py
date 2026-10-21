"""Build and verify an arm64 Android Preview using a persistent local signing key."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

from elink import __version__
from elink.core.security import protect
from scripts.package_metadata import source_identity
from scripts.package_windows import ROOT


def signing_environment(environment):
    environment = dict(environment)
    if environment.get('ELINK_ANDROID_KEYSTORE'):
        return environment
    if os.name != 'nt':
        raise RuntimeError('Configure ELINK_ANDROID_KEYSTORE and signing passwords outside the repository')
    directory = Path(os.environ['LOCALAPPDATA']) / 'ElinkDeveloper' / 'android-signing'
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / 'elink-preview.p12'
    secret = directory / 'password.dpapi'
    if key.exists() != secret.exists():
        raise RuntimeError('Incomplete signing identity; existing files were preserved')
    password = protect(secret.read_bytes(), decrypt=True).decode('ascii') if secret.exists() else secrets.token_hex(24)
    environment.update(ELINK_ANDROID_KEYSTORE=str(key), ELINK_ANDROID_STORE_PASSWORD=password,
                       ELINK_ANDROID_KEY_PASSWORD=password, ELINK_ANDROID_KEY_ALIAS='elink')
    if not key.exists():
        keytool = Path(environment['JAVA_HOME']) / 'bin' / 'keytool.exe'
        subprocess.run([str(keytool), '-genkeypair', '-keystore', str(key), '-storetype', 'PKCS12',
                        '-storepass:env', 'ELINK_ANDROID_STORE_PASSWORD', '-keypass:env', 'ELINK_ANDROID_KEY_PASSWORD',
                        '-alias', 'elink', '-keyalg', 'RSA', '-keysize', '3072', '-validity', '10000',
                        '-dname', 'CN=Elink Preview'], env=environment, check=True)
        secret.write_bytes(protect(password.encode('ascii')))
    return environment


def package(output=None, gradle=None):
    identity = source_identity()
    if identity['source_dirty']:
        raise RuntimeError('Android Preview builds require committed, synchronized source')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = (output or ROOT / 'dist' / 'android' / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    environment = signing_environment(os.environ)
    environment.setdefault('ANDROID_HOME', str(ROOT / '.artifacts/android/sdk'))
    executable = gradle or ROOT / 'android' / ('gradlew.bat' if os.name == 'nt' else 'gradlew')
    subprocess.run([str(executable), '--no-daemon', 'testReleaseUnitTest', 'lintRelease', 'assembleRelease'],
                   cwd=ROOT / 'android', env=environment, check=True)
    apk = ROOT / 'android/app/build/outputs/apk/release/app-release.apk'
    sdk = Path(environment['ANDROID_HOME']) / 'build-tools/35.0.0'
    verifier = sdk / ('apksigner.bat' if os.name == 'nt' else 'apksigner')
    validation = subprocess.check_output([str(verifier), 'verify', '--verbose', '--print-certs', str(apk)],
                                         env=environment, text=True, encoding='utf-8')
    aapt = sdk / ('aapt.exe' if os.name == 'nt' else 'aapt')
    metadata = subprocess.check_output([str(aapt), 'dump', 'badging', str(apk)], env=environment, text=True, encoding='utf-8')
    if f"versionName='{__version__}'" not in metadata or "native-code: 'arm64-v8a'" not in metadata:
        raise RuntimeError('APK version or ABI mismatch')
    if "name='io.github.yuyuyu501.elink'" not in metadata:
        raise RuntimeError('APK application ID mismatch')
    destination = output / f'Elink-{__version__}-android-arm64.apk'
    shutil.copy2(apk, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    receipt = dict(version=__version__, platform='android-arm64', package=destination.name,
                   sha256=digest, bytes=destination.stat().st_size, signature_verified=True,
                   certificate=validation, unit_tests=True, lint=True, manual_acceptance='pending', **identity)
    if source_identity() != identity:
        raise RuntimeError('Source changed during Android packaging')
    (output / 'Android.sha256').write_text(f'{digest}  {destination.name}\n', encoding='ascii')
    (output / 'Android.verification.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(destination)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--gradle', type=Path)
    args = parser.parse_args()
    package(args.output, args.gradle)
