# Security Policy

## Supported version

The latest commit on `main` is the supported public version.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting flow:

<https://github.com/Dep-0302/AI-Toolbox/security/advisories/new>

Do not include credentials, private filesystem paths, or real user inventory in a public issue. Include a minimal synthetic reproduction, affected commit, expected boundary, and observed result.

## Security model

The application is designed to run only on loopback, scan only allowlisted local roots, reject symbolic-link escapes, avoid executing discovered assets, and write only rebuildable project-local snapshots. A report that crosses any of these boundaries is considered security-relevant.
