# Changelog

All notable public changes to AI-Toolbox are documented in this file.

## 0.3.0 - 2026-08-13

### Added

- A native, local-only Project Skill observation-root selector; the public build starts unconfigured instead of assuming one user's Documents layout.
- Persistent local monitoring for explicitly selected projects, including safe removal by stable project ID.
- A build-and-source identity handshake so the macOS launcher does not reuse a stale service from another checkout.

### Changed

- One selected project is shown at a time, while registered projects, containers, saved projects, and evidence remain separate.
- Selecting an already registered project now reuses that registration instead of adding a duplicate saved-project entry.
- Saved projects display their selected project-directory name, persist across service restarts, and may live outside the configured observation root without expanding root discovery.

### Privacy and safety

- Observation-root and saved-project state is written only to ignored `0600` files under `generated/project-skills/`.
- Removing monitoring never deletes or modifies the observed project.
- The public project and association registries remain empty; release artifacts contain no local root, saved-project state, personal path, or generated snapshot.

## 0.2.2 - 2026-08-12

### Fixed

- Project folder selection now opens from the macOS Documents folder with project-specific guidance.
- Rejected folders return safe, actionable Chinese messages instead of exposing internal error codes or leaving the dialog in a stale waiting state.
- Project folder selection uses the dedicated Project Skill lock, so collection startup checks no longer produce false `refresh_in_progress` conflicts.

## 0.2.1 - 2026-08-12

### Security

- Updated development-only transitive dependencies to patched `nanoid` and `undici` releases. This clears the package audit and GitHub Dependabot alerts without changing the application runtime boundary.

## 0.2.0 - 2026-08-12

### Added

- An observe-only Project Skill workbench that keeps file discovery, project binding, host availability, invocation declarations, and actual use as separate evidence layers.
- Manual registered-project observation under the public `~/Documents` boundary.
- Native temporary folder selection for another collection source or one specific project under `~/Documents`.
- Empty public project and association Registry templates, plus synthetic Project Skill fixtures and contracts.

### Changed

- The public package and macOS launcher release identity are now `0.2.0` and `0.2.0-public`.
- Temporary collection selection is scoped to the current local service session; temporary Project Skill selection remains unregistered and in memory.
- Release documentation now describes source-session behavior, Project Skill metadata projection, persistence, and fail-closed folder boundaries.

### Privacy and safety

- Project Skill observation does not read manifest bodies, scripts, references, credentials, host configuration bodies, sessions, or logs.
- Temporary Project Skill selection does not write to the selected project, project registries, or `generated/project-skills/`.
- Public release artifacts exclude generated snapshots, local decisions, personal paths, real project names, and internal review material.

## 0.1.0

- Initial public observe-only AI capability inventory.
