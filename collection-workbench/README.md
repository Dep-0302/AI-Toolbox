# Collection workbench

This directory contains the legacy static collection indexer used by AI-Toolbox's candidate view.

- `check-inventory.py` performs bounded, read-only inventory.
- `workbench.py` creates ignored `data.json` and `data.js` files.
- `check_data.py` validates the generated contract.
- `rules.json` contains public starter grouping rules and no personal overrides.
- `index.html` reads local generated data and keeps decisions in the browser.

The public repository ships no personal `zh_metadata.json`, baseline inventory, or generated decision files. Configure a source with `AI_TOOLBOX_COLLECTION_ROOT` or use the main application's folder picker.
