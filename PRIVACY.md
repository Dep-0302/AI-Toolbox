# Privacy

AI-Toolbox is a local, observe-only application.

## What it reads

- Allowlisted entry files and bounded metadata under configured local Skill and plugin roots.
- File metadata and safe manifest fields needed to identify local capabilities.
- The default collection root `~/AI-Toolbox-Collection`, or one collection directory the user explicitly configures or chooses.
- On a manual Project Skill refresh, top-level directory names under `~/Documents` and the approved entry points of projects explicitly listed in the local project Registry.
- For a temporarily selected project under `~/Documents`, only its approved Skill entry points and allowlisted metadata projection.

Project Skill metadata is limited to allowlisted `SKILL.md` frontmatter fields (`name`, `description`, `version`, `author`, `license`, and `agent_created`) and, when present, the direct `agents/openai.yaml` field `policy.allow_implicit_invocation`. The latter is treated as a declaration, not proof of availability or use.

## What it does not read

- API keys, authentication files, browser profiles, chat sessions, logs, databases, or arbitrary host configuration bodies.
- Project Skill manifest bodies, scripts, references, credential files, session history, or host configuration bodies.
- Targets of symbolic links found inside a selected collection.
- Project directories classified only as unclassified candidates; only their top-level names are listed.
- Executable or binary contents merely because an entry was discovered.

## What it writes

- Rebuildable snapshots and locks under this repository's ignored `generated/` directory.
- Rebuildable Project Skill snapshots, attempt receipts, and reports under ignored `generated/project-skills/`, only after a manual registered-project refresh.
- Browser favorites and view decisions in namespaced `localStorage`.
- Frontend dependencies under `node_modules/` and build output under `dist/` when requested.

Temporary collection selection is held only for the current local service session. A temporary Project Skill selection is not added to either Registry, is not written to `generated/project-skills/`, and is not written back to the selected folder. Short-lived folder-selection tokens are single-use and remain in server memory.

## Local transport and safety

- The HTTP service listens only on loopback and checks Host, Origin, CSRF tokens, and bounded request sizes.
- The application performs no model calls, telemetry, background watching, host mutation, project mutation, or application-level external network requests.
- Broad roots, sensitive locations, symbolic-link roots or ancestors, out-of-scope project folders, and path identity changes are rejected fail-closed.
- Installing dependencies with npm or cloning this repository is outside the running application's network boundary.

## Public release data

The public project Registry and project-association Registry are empty templates. Published source archives do not include generated snapshots, local decision exports, personal paths, real project names, or private curation data.

Do not commit generated snapshots, local decision exports, or metadata overlays that contain personal paths.
