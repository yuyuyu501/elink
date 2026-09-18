# Project Workflow

The user-approved default is: local changes -> automated tests -> Git sync.

- Complete the requested changes, run appropriate automated tests, then commit
  and push the task's changes to the configured upstream when tests pass.
- Check the worktree and remote state first. Preserve unrelated user changes;
  stage explicit task paths. Never force-push, discard changes, or auto-merge a
  diverged remote just to complete synchronization. Report a failed sync.
- `python -m scripts.check_and_sync --message "..."` tests and synchronizes an
  already reviewed/staged worktree. It refuses unstaged/untracked source changes
  so the committed code matches the code tested. It never builds binaries.
- Packaging is separate and manual. Run `python -m scripts.package_windows`
  only when the user explicitly asks for packaging or a release in that task.
- Routine edits, test success and Git pushes do not authorize packaging,
  version bumps, tags, GitHub Releases or driver installation.
- Never commit `.artifacts`, `.runtime`, `dist`, `build`, credentials, pairing
  identities, private keys or machine-specific test configuration.
- Keep preview limitations accurate. Passing unit tests does not establish
  stable game streaming, virtual driver compatibility or release signing.
