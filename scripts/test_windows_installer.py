"""Install, run, repair, update and uninstall in an isolated current-user folder."""
import json
import os
from pathlib import Path
import subprocess
import uuid

from elink.processes import native_environment
from scripts.package_windows import validate_self_test
from scripts.test_portable_update import close_app, verify_portable_update, wait_for


APP_ID = '{A4735807-684B-4757-9EE5-88AFA2E6E791}'
UNINSTALL_KEY = rf'Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1'


def registration():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY,
                            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return {name: winreg.QueryValueEx(key, name)[0]
                    for name in ('InstallLocation', 'DisplayVersion', 'UninstallString')}
    except FileNotFoundError:
        return None


def run_value():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
            return winreg.QueryValueEx(key, 'Elink')
    except FileNotFoundError:
        return None


def shortcut_target(path):
    environment = native_environment()
    environment['ELINK_SETUP_SHORTCUT'] = str(path)
    command = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; "
               "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ELINK_SETUP_SHORTCUT); "
               "$s.TargetPath | ConvertTo-Json -Compress")
    result = subprocess.check_output(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                     env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    return Path(json.loads(result.decode('utf-8-sig'))).resolve()


def verify_installer(setup, bundle, root, version, manifest):
    from scripts.package_installer import file_manifest
    if registration() is not None:
        raise RuntimeError('An Elink installation already exists; use an isolated Windows account for the installer test.')
    root.mkdir(parents=True, exist_ok=False)
    target = root / 'Installed Elink \u4e2d\u6587 [1]'
    group = 'Elink setup test ' + uuid.uuid4().hex
    shortcut = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs' / group / 'Elink.lnk'
    executable = target / 'app/Elink.exe'
    original_startup = run_value()
    flags = dict(env=native_environment(), creationflags=subprocess.CREATE_NO_WINDOW, check=True, timeout=180)

    def install(log, directory=target):
        subprocess.run([str(setup), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART',
                        '/TASKS=', '/LANG=chinesesimplified', f'/DIR={directory}', f'/GROUP={group}',
                        f'/LOG={root / log}'], **flags)
        saved = registration()
        if not saved or Path(saved['InstallLocation']).resolve() != target or saved['DisplayVersion'] != version:
            raise RuntimeError('Installer did not register the correct current-user path/version.')
        if file_manifest(target / 'app') != manifest:
            raise RuntimeError('Installed bytes differ from the published portable payload.')
        if not shortcut.is_file() or shortcut_target(shortcut) != executable:
            raise RuntimeError('Installed Start menu shortcut has an incorrect target.')

    def uninstall():
        saved = registration()
        if saved and Path(saved['InstallLocation']).resolve() == target:
            close_app(executable, wait=True)
            subprocess.run([str(target / 'unins000.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                            f'/LOG={root / "uninstall.log"}'], **flags)
            wait_for(lambda: not (target / 'unins000.exe').exists(), 'Uninstaller did not finish.', timeout=20)

    try:
        install('install.log')
        environment = native_environment()
        environment['ELINK_DATA_DIR'] = str(root / 'self-test-data')
        diagnostics = root / 'self-test'
        subprocess.run([str(executable), '--self-test', str(diagnostics)], env=environment,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=True, timeout=120)
        result = json.loads((diagnostics / 'result.json').read_text(encoding='utf-8'))
        validate_self_test(result)
        uninstaller_hash = file_manifest(target, exclude_app=True)
        update = verify_portable_update(bundle, root / 'update-check', installed_root=target)
        if file_manifest(target / 'app') != manifest or file_manifest(target, exclude_app=True) != uninstaller_hash:
            raise RuntimeError('ZIP update changed installer metadata or installed payload.')
        if shortcut_target(shortcut) != executable or registration()['DisplayVersion'] != version:
            raise RuntimeError('ZIP update broke installation registration/shortcut.')
        # Windows normally treats spelling variants of a path as the same folder.
        install('repair.log', Path(str(target).swapcase()))
        if not os.path.samefile(registration()['InstallLocation'], target):
            raise RuntimeError('Repair created another installation for a case-only path change.')
        # A ZIP update can add files which the original Inno uninstall manifest never listed.
        (target / 'app/added-by-update.txt').write_text('owned program file', encoding='ascii')
        userdata = root / 'update-check/userdata/preserved-user-data.txt'
        saved_data = userdata.read_bytes()
        uninstall()
        if registration() is not None or (target / 'app').exists() or (target / '.Elink-update-smoke').exists():
            raise RuntimeError('Uninstall left owned program files or registration behind.')
        if shortcut.exists() or userdata.read_bytes() != saved_data or run_value() != original_startup:
            raise RuntimeError('Uninstall failed shortcut removal or user-data/startup preservation.')
        return dict(ok=True, offline_payload_identical=True, current_user_install=True, shortcut=True,
                    self_test=result, repair=True, zip_update=update, uninstaller_preserved=True,
                    uninstall=True, added_files_removed=True, configuration_preserved=True,
                    unrelated_startup_preserved=True, unicode_path=True, case_insensitive_path=True,
                    note='Isolated same-version install/repair/update; manual two-machine acceptance pending.')
    finally:
        uninstall()
