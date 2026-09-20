# Agent instructions

## Scope and product

Omarchy Display Workspaces is a third-party Quickshell bar widget and compact display/workspace arrangement panel. Its public ID, QML module/IPC identity, and settings key are **`display.workspaces`**. Never use a developer username or homelab name as product identity. The `omarchy.*` namespace is first-party; `omarchy.workspaces` references that remain here identify the stock widget or upstream provenance.

Keep the product narrow: multi-display workspace visibility, straightforward session arrangement with advertised resolution/refresh and scale controls, named live-arrangement profiles with optional strong-identity reconnect restoration, immediate presentation preferences, and deliberate configuration cleanup. Do not add automatic layout learning, individual-window tracking, rotation editing, HDR controls, dependencies, telemetry, or persistent background services without a request.

An on-demand, event-driven, session-lived output owner is explicitly approved for manual new-connector arrangements. It retains only runtime geometry through the native output-management protocol; do not expand it into a startup service, layout learning, or continual enforcement.

Read `README.md`, `docs/INSTALLATION.md`, and `docs/USAGE.md` before behavior changes. Follow `docs/AGENTS.md` when editing documentation or screenshots.

## Architecture

- `manifest.json`: native Omarchy package identity and entry point; must remain at repository root for URL installation.
- `Workspaces.qml`: per-display bar groups, elected cursor polling/connection observation, identity matching, native shell preference persistence.
- `HyprlandEventStream.qml`: elected reconnecting focus-event transport; reconnects resynchronize focus without replaying profile automation.
- `ArrangePanel.qml`: draft layout/workspace state, catalog, confirmation, validation generations, worker responses.
- `ArrangePopup.qml`, `DisplayMap.qml`: native popup and display geometry UI.
- `apply.py`: discovery, validation, independent systemd preview/Forget workers, and runtime transaction state.
- `output_management.py`: standard-library Wayland geometry transport, native head/mode validation and policy-preserving overrides.
- `output_owner.py`: generation-guarded session ownership, runtime journal, restart adoption and explicit release.
- `configuration.py`: nonexecuting Lua scanner, configuration/preference catalog, revisions, exact backups and guarded restoration.
- `profiles.py`: versioned private profile store, guarded CRUD, hardware matching, and draft-only loading.
- `automation.py`: per-session settled connection episodes, guarded automatic selection, and immutable unattended authorization.
- `test_apply.py`, `test_configuration.py`, `test_profiles.py`, `test_output_management.py`, `test_output_owner.py`: isolated Python behavioral regression coverage.

There is no build step. Reuse Omarchy's host QML components and Python's standard library.

## Safety invariants

- **Display/workspace changes are session-only.** Manual Preview requires Apply within 20 seconds. Explicitly opted-in automatic restoration keeps only after its full 20-second health observation and authorization recheck. Both use the independent rollback worker; never silently persist Hyprland layout rules.
- Mode/scale edits belong to the same complete draft transaction as positions. Validate advertised modes and whole logical pixels; preserve the original timing for an unchanged custom mode. Rollback may restore original geometry only when the live geometry still matches the original or intended target; preserve unrelated external changes.
- Manual new-connector previews may use native geometry-only overrides without creating Lua rules. Keep the owner independent from the watchdog; an initial Revert releases it, later Revert preserves prior ownership, and Apply retains it for the session. Never infer absent ICC/HDR policy from monitor JSON. Preflight native target and rollback representation, preserve unchanged custom timings, and do not widen automatic restoration's exact-rule authorization.
- Profiles save current live state, never untested draft edits. Manual loading only prepares a draft. Match all enabled displays one-to-one; conflicting nonempty identities must never fall back to the same connector. Weak matches require an explicit warning and are never automatic. Restoration must create and retain all saved positive workspace IDs, retire only atomically verified empty extras, and preserve populated/unknown-count extras and special workspaces. Runtime persistence and empty cleanup belong to the independent rollback transaction; preserve unrelated rule fields and external edits. Keep presentation settings separate.
- Profile replacement/deletion and automation opt-in require immutable ID/revision confirmation and exact backups before atomic writes. New/replaced profiles are manual. Read schema 1 without writing, migrate only on guarded writes, and never infer consent. Refuse corrupt/unknown schemas and unsafe paths; never reset the store silently. Serialize storage across compositor sessions and block profile operations during preview/automatic restoration/Forget.
- Automation needs a unique complete strong identity set, one enabled profile per combination, and a currently-live saved arrangement at opt-in. Settle actual connection changes; consume attempts before launch and suppress episodes for open panels/manual operations. No continual enforcement, idle polling, or guessing on weak/no/multiple matches. Guard store authorization before mutation and unattended keep; restart recovers rather than reapplies. Runtime lock precedes profile-store lock.
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
python3 -m unittest test_apply test_configuration test_profiles test_output_management test_output_owner
```

For a backend behavioral change, exercise the changed path with isolated files, including relevant failure/recovery cases. A detached-worker change needs an actual worker-lifetime smoke, not only mocks. QML changes require opening the real components in a native harness or compatible Omarchy session; compilation alone is insufficient. Do not install extra tools merely to claim validation.

Test externally visible behavior and risky boundaries, not source text or mock forwarding. Keep permanent tests for plausible regressions; remove disposable fixtures afterward. Report what was actually exercised and any verification limitations.

Native QML caching may require `omarchy restart shell`; do not claim a reload occurred without checking the running shell. Never change a user's personal configuration just to make documentation examples match.

Before publication, inspect the exact staged files for credentials, local paths, backups, runtime state and private captures. Update behavior/version/install docs as needed. Preserve MIT attribution and the GPT Astra AI-generation caution. Do not commit, push, publish releases, or rewrite Git history unless the current user request authorizes it.

## GitHub releases

When the user requests a release, use the manifest version for the `v<version>` tag and release title. Tag the exact reviewed, pushed commit; do not tag an uncommitted worktree or invent older release history. Treat identity/settings migrations as breaking changes. Include installation, migration, verification, AI-generation caution and recovery limitations in release notes. Mark a stable release as GitHub's latest only when requested, and verify its tag/commit, published state and latest-release API afterward. Native Omarchy updates follow the repository's default branch, not GitHub's latest-release marker.
