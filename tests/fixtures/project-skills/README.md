# Project Skill synthetic fixtures

This directory contains synthetic, data-driven fixtures for the project Skill observation contract. It does not contain material copied from an observed project, credentials, user names, or machine-specific paths.

## Safety rules

- Every path in these fixtures is relative to a temporary test root.
- The checked-in tree contains no symbolic links.
- Tests must materialize `dynamic_symlinks`, root-link cases, permission failures, and `runtime_events` only inside a newly created temporary directory.
- A test must remove that temporary directory after the scenario finishes.
- Fixture metadata is inert data. A test must never execute a tag, constructor, command, script, or referenced file.
- `SKILL.md` processing stops after the bounded frontmatter projection. Body sentinels listed in `index.json` must never appear in a result, error, fingerprint input, log, Markdown export, or UI model.
- A path marked `must_not_read` is an assertion target, not an alternate discovery source.

## Layout and materialization

`index.json` is the entry point. Each item points to one scenario JSON file. `expected.json` provides the compact cross-scenario acceptance matrix; detailed outcomes remain beside their construction recipe in each scenario file.

Scenario nodes use the following declarative forms:

- `directory`: create a regular directory.
- `file`: create a regular file from `source`, `content_utf8`, `content_base64`, `content_template`, or `content_recipe`.
- `dynamic_symlinks`: create links after every regular directory and file is present. Never copy these links into the repository.
- `runtime_events`: execute the named test hook at the exact phase shown. The TOCTOU scenarios intentionally replace a checked path only inside the temporary root.
- `io_faults`: inject the declared error in the test double; do not weaken local file permissions to imitate it.
- `node_matrix`: expand each listed excluded segment into one regular bait file under `base_path`; it is a compact assertion that every listed boundary remains unread.

For `content_recipe`, concatenate `prefix`, `repeat.text` repeated `repeat.count` times, and `suffix`. The recipe keeps the repository small while testing an exact boundary overrun.

Materializers should reject absolute paths and `..` path components in node destinations. Symlink targets may contain `..` because they are the object under test, but must never be resolved or read before the production boundary check permits it.

## Frozen calibration expectations

- `empty-entry`: an existing empty approved entry is successful with zero Skills.
- `five-entities`: five copied entity manifests produce five file observations and five binding candidates.
- `marketplace-bait`: neither `marketplace.json` nor the ordinary `plugins/` tree is an approved projection; expected reads are zero.
- `multi-evidence`: four entity manifests plus six project links plus six plugin-source observations produce 10 logical Skills, 16 file observations, and 10 binding evidences.
- `human-only`: machine observation remains zero while a separate human association exists.
- `codex-path-only`: seven `.codex/skills` manifests are observed, but binding remains `observed_path_only` and host availability remains `unverified`.
- `container-no-descent`: a container is not recursively scanned.

`safe-six-frontmatter/static/safe-six/SKILL.md` contains the canonical body sentinel. Its only purpose is to prove that body bytes are not projected or fingerprinted.
