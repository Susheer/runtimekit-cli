# RuntimeKit CLI

RuntimeKit CLI is a generic command-line tool for scaffolding and managing modular runtime applications.

It can generate modules, pages, stores, backend routes, descriptor wiring, dependency wiring, release commands, installer collection, and safe cleanup commands for a workspace that follows a modular runtime layout.

## Install

From PyPI after publishing:

```bash
python -m pip install runtimekit-cli
```

From GitHub before publishing:

```bash
python -m pip install "runtimekit-cli @ git+https://github.com/Susheer/runtimekit-cli.git"
```

## Configure

Run this once per machine or environment:

```bash
runtimekit config init --workspace /path/to/spec --name "My Runtime"
```

The configured name is only local CLI metadata. It does not need to be hardcoded into the package.

Show config:

```bash
runtimekit config show
```

## Common Commands

```bash
runtimekit doctor
runtimekit add module APP.WorkOrders --menu-parent Operations
runtimekit add page APP.WorkOrders WorkOrderDetails
runtimekit add store APP.WorkOrders WorkOrdersStore
runtimekit add dependency APP.WorkOrders OTHER.SharedFeature
runtimekit add include OTHER.SharedFeature --app APP
runtimekit prepare
runtimekit inspect runtime
runtimekit run browser --open
runtimekit run electron
runtimekit release make --target mac-dmg
runtimekit clean --target build --target installer --dry-run
runtimekit remove module APP.WorkOrders --dry-run
```

Destructive commands require `--yes` unless `--dry-run` is used.

## Design-Aware Scaffolding

`runtimekit add module` now creates a full starter module instead of a blank shell:

- Three visible pages in the same navigation group.
- A module-scoped frontend store with fake data for every generated page.
- Backend route and service files returning sample data.
- `module.json` page, menu, navigation, icon, restore-state, and keep-alive metadata.
- MUI/platform UI page templates using cards, metrics, status pills, and data tables.

`runtimekit add page` adds the new page under the module's existing navigation group by default,
updates store page metadata, refreshes sample data, and keeps the generated frontend module export
aligned with `module.json`.

## Release Artifacts

Release builds can collect generated installer files into a root-level installer folder:

```bash
runtimekit release make --target mac-dmg
```

The collected output defaults to:

```text
installer/
```

Override it with:

```bash
runtimekit release make --target mac-dmg --installer-dir published-installers
```

## Development

```bash
python -m unittest discover -s tests
PYTHONPYCACHEPREFIX=/tmp/runtimekit_pycache python -m py_compile runtimekit_cli/cli.py tests/test_cli.py
python -m build
```

## Publish

The recommended publish flow is PyPI Trusted Publishing from GitHub Actions. Create a PyPI project for `runtimekit-cli`, configure a trusted publisher for this repository, then publish by creating a GitHub release.
