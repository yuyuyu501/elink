# Elink project workflow

Apply the global task-routing rules first. Questions, research and planning do not
trigger source edits, Git, tests, packaging or deployment by themselves.

```yaml
workflow:
  local_change: 1
  test: 1
  git_sync: 1
  server_update: 0
  windows_package: 1
  github_release: 1
```

## Current development cycle

- Default for code/configuration tasks: local changes -> automated tests -> Git
  sync. As authorized on 2026-10-16, continue to Windows packaging and Preview
  publication when a concrete problem has been clearly fixed and validated.
- Investigation, incomplete experiments and documentation-only changes do not
  trigger a release. Record evidence for the resolved problem and disclose any
  remaining limitations; a passing unit suite alone does not prove a performance
  problem is resolved. No further permission is needed for a verified fix under
  this standing authorization, unless the user marks the task local-only.
- Development-only native compilation and rendering benchmarks are allowed when
  required to validate the requested implementation; they are not app packaging.
- No server deployment is enabled. Git synchronization is not deployment.
- Before editing, read applicable instructions, check branch/worktree and remote
  state, preserve unrelated work. For replacement/refactoring, inspect current
  code and Git history, identify related tests/configuration/docs and search for
  stale references after editing. Do not spawn sub-agents by default.

## Tests and Git synchronization

- Use the project environment: `.venv/Scripts/python.exe` on Windows. Run relevant
  checks while developing, then the full suite before committing/pushing.
- Review the final diff, stage explicit task paths and run
  `.venv/Scripts/python.exe -m scripts.check_and_sync --message "..."`.
  It tests the reviewed/staged source, checks the remote, commits and pushes.
  It refuses remaining unstaged/untracked source and never packages binaries.
- Verify local HEAD and the upstream remote branch reference the same commit.
  Preserve failed-sync commits and report failures. Never force-push, discard
  changes or auto-merge a diverged remote merely to complete synchronization.
- Never commit `.artifacts`, `.runtime`, `dist`, `build`, credentials, environment
  files, pairing identities, keys, personal media or machine-specific test data.
- Routine verification must not invoke paid providers or install drivers.
- Record limitations accurately: tests and local benchmarks do not establish
  stable two-machine gaming, all driver compatibility or release signing.

## Packaging and publication after a verified fix

- Prepare a new unused version, update both declarations and write release notes.
- Use `.venv/Scripts/python.exe -m scripts.release_windows --message "..." --notes <notes.md>`.
  It tests/syncs, packages/self-tests and publishes, stopping on failure. Retry
  individual failed stages without rebuilding already verified artifacts.
- Publish only a clean synchronized commit whose Windows CI passed. Upload to a
  draft, verify asset sizes/hashes, then publish a Windows Preview Release to
  yuyuyu501/elink. Never overwrite a published version or report failed builds
  or uploads as successful releases.
- User manual acceptance follows installation and is separate from automated
  tests. Preview signing limitations remain explicit. Driver installation is
  not authorized.
