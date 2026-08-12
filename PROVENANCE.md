# Public Release Provenance

This repository is a clean-history public snapshot of the AI-Toolbox application code. Release `0.2.0` adds the observe-only Project Skill workbench and temporary native folder selection while keeping local inventory data out of the public source.

The export process intentionally removed:

- author-machine capability metadata and collection curation;
- real project names, project registrations, project associations, and personal paths;
- generated snapshots, dependency trees, and build output;
- local browser decision exports and other generated decision data;
- internal handoff, audit, and historical project documents;
- screenshots containing real inventory counts or names;
- third-party product logos without a documented redistribution license.

The committed tests use synthetic fixtures. Public collection metadata, project Registry, and project-association Registry files are empty templates. They are not evidence that any capability or project is installed, registered, bound, enabled, callable, or used.

The public defaults are `~/AI-Toolbox-Collection` for collection observation and `~/Documents` for the Project Skill observation boundary. These are portable home-relative defaults, not exported author-machine paths. Runtime output under `generated/` and browser-local decisions are intentionally not part of the release.
