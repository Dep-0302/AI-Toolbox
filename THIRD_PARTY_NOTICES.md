# Third-Party Notices

This source repository does not commit `node_modules/` or compiled `dist/` output. Dependencies are installed from the npm registry according to `package-lock.json` and retain their own licenses.

Key runtime dependencies:

- React, React DOM, and Scheduler — MIT License.
- Lucide React — ISC License; some icons are derived from Feather Icons under the MIT License.

Development dependencies include Vite, Vitest, jsdom, and their transitive packages under the licenses recorded in `package-lock.json` and the packages' own license files. Some optional development-chain Lightning CSS packages use MPL-2.0.

Downstream distributors who publish compiled bundles should preserve all applicable third-party notices and provide source as required by the relevant licenses.
