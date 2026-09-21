# Project Workflow

The user-approved default is: local changes -> automated tests -> Git sync ->
Windows packaging -> GitHub Release. The user manually tests the installed update.

- Complete the requested changes, run appropriate automated tests, then commit
  and push the task's changes to the configured upstream when tests pass.
- Check the worktree and remote state first. Preserve unrelated user changes;
  stage explicit task paths. Never force-push, discard changes, or auto-merge a
  diverged remote just to complete synchronization. Report a failed sync.
- `python -m scripts.check_and_sync --message "..."` tests and synchronizes an
  already reviewed/staged worktree. It refuses unstaged/untracked source changes
  so the committed code matches the code tested. It never builds binaries.
- For completed application updates, prepare a new unused version (patch bump by
  default), update both version declarations and write release notes. The standing
  authorization includes packaging, version bumps, tags and publishing preview
  Releases to yuyuyu501/elink; do not ask for permission again for these steps.
- Run `python -m scripts.release_windows --message "..." --notes <notes.md>`
  after reviewing/staging task changes. It runs check_and_sync, package_windows
  and publish_release in order, stopping on failure. Its individual scripts remain
  available for retrying a failed stage without rebuilding successful artifacts.
- Publish only a clean, synchronized commit whose Windows CI passed. Upload to a
  draft, verify asset sizes and hashes, then publish. Never overwrite a published
  version. Failed packaging or upload must not be reported as a successful release.
- User manual acceptance happens after release and is not an automated-test result.
  Record pending user acceptance accurately. Driver installation is not authorized.
- Never commit `.artifacts`, `.runtime`, `dist`, `build`, credentials, pairing
  identities, private keys or machine-specific test configuration.
- Keep preview limitations accurate. Passing unit tests does not establish
  stable game streaming, virtual driver compatibility or release signing.
