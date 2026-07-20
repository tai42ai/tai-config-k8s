# Contributing to tai-config-k8s

`tai-config-k8s` is the Kubernetes `ConfigManager` provider plugin. The hard
rule (the plugin rule): **it depends on `tai-contract` + `tai-kit` only and never
imports the skeleton.** It implements the contract's `ConfigManager` ABC and is
loaded by the skeleton's config seam through the `build_config_manager()` factory
convention by dynamic import — there is no import edge to the skeleton in either
direction.

## Ground rules

- **No skeleton import — ever.** The plugin is reached only by the factory's
  string module name:
  ```bash
  grep -rn "tai_skeleton" src/   # must be empty — the plugin imports no application package
  ```
- **The `kubernetes` client stays optional.** It is the `[k8s]` extra, imported
  lazily inside `K8sConfigManager`, which raises a copy-pasteable install hint
  (`tai-config-k8s[k8s]`) if it is absent — so importing the package without the
  cluster client never fails.
- **Typed package** (`py.typed`). Pyright runs clean; a missing optional
  `kubernetes` client is a warning, not an error.

## Layout

- `manager.py` — `K8sConfigManager` (the `ConfigManager` impl) + `K8sConfigError`
  + the `build_config_manager()` factory.
- `settings.py` — `K8sConfigSettings` + the cached `k8s_config_settings()`
  accessor (tai-kit `TaiBaseSettings` + `@settings_cache`).
- `_kubernetes_optional.py` — the `require_kubernetes()` guard for the `[k8s]`
  extra.

## Dev

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

For local cross-repo work, `make dev` editable-installs the sibling `tai-*`
checkouts this package builds on into the venv. While `[tool.uv.sources]` pins
those siblings to local paths, `uv sync` already installs them editable and
`make dev` changes nothing; once the lock resolves them from the registry,
`uv sync` / `uv run` installs the published builds instead, so re-run
`make dev` afterward to restore the editable links.

Before any commit, run a secret scan over `src/` and `tests/` (e.g.
`detect-secrets scan`).

## License

By contributing you agree your contributions are licensed under Apache-2.0.
