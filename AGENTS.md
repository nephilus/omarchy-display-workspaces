# Agent instructions

## Scope and product

Omarchy Display Workspaces is a third-party Quickshell bar widget and compact display/workspace arrangement panel. Its public ID, QML module/IPC identity, and settings key are **`display.workspaces`**. Never use a developer username or homelab name as product identity. The `omarchy.*` namespace is first-party; `omarchy.workspaces` references that remain here identify the stock widget or upstream provenance.

Keep the product narrow: multi-display workspace visibility, straightforward session arrangement, immediate presentation preferences, and deliberate configuration cleanup. Do not add profiles, resolution/HDR controls, dependencies, telemetry, or background services without a request.

Read `README.md`, `docs/INSTALLATION.md`, and `docs/USAGE.md` before behavior changes. Follow `docs/AGENTS.md` when editing documentation or screenshots.

## Architecture

- `manifest.json`: native Omarchy package identity and entry point; must remain at repository root for URL installation.
- `Workspaces.qml`: per-display bar groups, elected cursor polling, identity matching, native shell preference persistence.
- `ArrangePanel.qml`: draft layout/workspace state, catalog, confirmation, validation generations, worker responses.
- `ArrangePopup.qml`, `DisplayMap.qml`: native popup and display geometry UI.
- `apply.py`: discovery, validation, independent systemd preview/Forget workers, and runtime transaction state.
- `configuration.py`: nonexecuting Lua scanner, configuration/preference catalog, revisions, exact backups and guarded restoration.
- `test_apply.py`, `test_configuration.py`: isolated Python behavioral regression coverage.

There is no build step. Reuse Omarchy's host QML components and Python's standard library.

## Safety invariants

- **Preview/Apply is session-only.** Keep the 20-second confirmation deadline and independent rollback worker. Never silently persist layout changes.
- Names/icons/order save separately through native shell APIs. Preserve disconnected display preferences and unrelated settings.
- **Forget is destructive.** Require immutable key/revision confirmation; back up changed files before atomic writes. Validate reload/configuration errors. Preserve concurrent external edits during recovery.
- Keep preview and Forget mutually exclusive. Worker lifetime must not depend on the panel remaining alive.
- Never execute user Lua merely to discover monitor rules. Dynamic/broad/ambiguous declarations fail closed; do not invent selector matches.
- Bad geometry must not hide healthy outputs or workspace cards. Never fabricate dimensions to enable an unsafe operation.
- Honor supported configuration paths and ownership/symlink checks. Do not weaken them to make a test pass.

## Working on a live desktop

The plugin may be linked into a real user's running shell. Do not use real monitor-rule deletion, layout changes, or preference resets as tests without explicit authorization. Never modify packaged `/usr/share/omarchy` files; reading them to confirm APIs is appropriate.

Use temporary HOME/configuration/runtime directories and controlled compositor command fixtures for mutations. Ensure detached workers inherit the isolated paths and cannot fall back to real configuration. Keep screenshots free of user windows, serial numbers, personal labels and file paths. Finish/revert active operations before moving code or changing runtime identity.

An ID change is a breaking migration: update manifest, QML module/IPC IDs, backend settings lookup, worker namespace, tests, docs, installed directory and the existing shell entry together. Preserve the rest of that entry; do not create a duplicate or retain compatibility aliases by default. Document the migration and choose an appropriate version.

## Verification and delivery

From the repository root:

```bash
omarchy plugin validate .
python3 -m unittest test_apply test_configuration
```

For a backend behavioral change, exercise the changed path with isolated files, including relevant failure/recovery cases. A detached-worker change needs an actual worker-lifetime smoke, not only mocks. QML changes require opening the real components in a native harness or compatible Omarchy session; compilation alone is insufficient. Do not install extra tools merely to claim validation.

Test externally visible behavior and risky boundaries, not source text or mock forwarding. Keep permanent tests for plausible regressions; remove disposable fixtures afterward. Report what was actually exercised and any verification limitations.

Native QML caching may require `omarchy restart shell`; do not claim a reload occurred without checking the running shell. Never change a user's personal configuration just to make documentation examples match.

Before publication, inspect the exact staged files for credentials, local paths, backups, runtime state and private captures. Update behavior/version/install docs as needed. Preserve MIT attribution and the GPT Astra AI-generation caution. Do not commit, push, publish releases, or rewrite Git history unless the current user request authorizes it.

## GitHub releases

When the user requests a release, use the manifest version for the `v<version>` tag and release title. Tag the exact reviewed, pushed commit; do not tag an uncommitted worktree or invent older release history. Treat identity/settings migrations as breaking changes. Include installation, migration, verification, AI-generation caution and recovery limitations in release notes. Mark a stable release as GitHub's latest only when requested, and verify its tag/commit, published state and latest-release API afterward. Native Omarchy updates follow the repository's default branch, not GitHub's latest-release marker.
