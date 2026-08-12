# Changelog

All notable public changes to AI-Toolbox are documented in this file.

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
