import subprocess

import pytest

from scripts import check_and_sync, package_windows


def sync_fixture(monkeypatch, *, remote_changed=False):
    responses = {
        ('symbolic-ref', '--quiet', '--short', 'HEAD'): 'main',
        ('config', 'branch.main.remote'): 'origin',
        ('config', 'branch.main.merge'): 'refs/heads/main',
        ('diff', '--cached', '--name-only'): 'elink/app.py',
        ('rev-parse', '--short', 'HEAD'): '1234567',
    }
    monkeypatch.setattr(check_and_sync, 'git', lambda *args: responses[args])
    monkeypatch.setattr(check_and_sync, 'reviewed_state', lambda: ('head', 'tree'))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if remote_changed and 'merge-base' in command else 0)

    monkeypatch.setattr(check_and_sync.subprocess, 'run', run)
    return calls


def test_failed_tests_never_commit_push_or_build(monkeypatch):
    calls = sync_fixture(monkeypatch)

    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(check_and_sync.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        check_and_sync.synchronize('test')
    assert len(calls) == 1 and calls[0][1:] == ['-m', 'pytest', '-q']


def test_remote_changes_stop_sync_without_commit(monkeypatch):
    calls = sync_fixture(monkeypatch, remote_changed=True)
    with pytest.raises(RuntimeError, match='Remote has new commits'):
        check_and_sync.synchronize('test')
    assert not any('commit' in c or 'push' in c for c in calls)


def test_source_changes_during_tests_stop_sync(monkeypatch):
    calls = sync_fixture(monkeypatch)
    states = iter([('head', 'before'), ('head', 'after')])
    monkeypatch.setattr(check_and_sync, 'reviewed_state', lambda: next(states))
    with pytest.raises(RuntimeError, match='changed while testing'):
        check_and_sync.synchronize('test')
    assert len(calls) == 1


def test_sync_only_tests_commits_and_pushes(monkeypatch):
    calls = sync_fixture(monkeypatch)
    check_and_sync.synchronize('Reviewed change')
    assert calls[-2:] == [['git', 'commit', '-m', 'Reviewed change'], ['git', 'push', 'origin', 'HEAD:refs/heads/main']]
    assert not any('build' in str(c) or 'package_windows' in str(c) or 'tag' in c for c in calls)


@pytest.mark.parametrize('report', [None, {}, {'ok': False}, {'ok': True, 'runtime_stopped': False},
                                   {'ok': True, 'runtime_stopped': True, 'video_frames': 1, 'audio_frames': 0}])
def test_package_rejects_failed_or_incomplete_exe_self_test(report):
    with pytest.raises(RuntimeError):
        package_windows.validate_self_test(report)


def test_packaging_stops_on_failed_tests_before_build(monkeypatch):
    monkeypatch.setattr(package_windows, 'source_snapshot', lambda: {})
    monkeypatch.setattr(package_windows.subprocess, 'check_output', lambda *a, **k: 'head')
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(package_windows.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        package_windows.package()
    assert len(calls) == 1 and calls[0][1:] == ['-m', 'pytest', '-q']
