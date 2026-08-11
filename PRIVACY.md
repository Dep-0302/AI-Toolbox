# Privacy

AI-Toolbox is a local, observe-only application.

## What it reads

- Allowlisted entry files and bounded metadata under configured local Skill and plugin roots.
- File metadata and safe manifest fields needed to identify local capabilities.
- A collection directory that the user explicitly configures or chooses.

## What it does not read

- API keys, authentication files, browser profiles, chat sessions, logs, databases, or arbitrary host configuration bodies.
- Targets of symbolic links found inside a selected collection.
- Executable or binary contents merely because an entry was discovered.

## What it writes

- Rebuildable snapshots and locks under this repository's ignored `generated/` directory.
- Browser favorites and view decisions in namespaced `localStorage`.
- Frontend dependencies under `node_modules/` and build output under `dist/` when requested.

AI-Toolbox has no telemetry and makes no application-level external network requests. Installing dependencies with npm or cloning this repository is outside the running application's network boundary.

Do not commit generated snapshots, local decision exports, or metadata overlays that contain personal paths.
