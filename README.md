# tokenjuice-hermes

`tokenjuice-hermes` is a generic Hermes directory plugin that compacts verbose
terminal-like tool results through the `transform_tool_result` hook.

The plugin is intentionally runtime-agnostic. It does not assume a specific host,
workspace, identity, chat bridge, or deployment layout.

## Status

This is an alpha plugin for Hermes runtimes that support the
`transform_tool_result` hook. It is packaged as normal Python code and can also
be copied into Hermes' directory-plugin layout.

## Behavior

- Compacts verbose JSON results from terminal-like tools such as `terminal` and
  `execute_code`.
- Preserves structured metadata such as `command`, `exit`, `status`, `cwd`, and
  other fields already present in the tool result.
- Leaves `read_file` results exact by returning `None` from the hook.
- Fails open: invalid JSON, unsupported tools, and short outputs are left
  unchanged.

Compacted payloads stay valid JSON and include a `tokenjuice` object:

```json
{
  "tokenjuice": {
    "compacted": true,
    "original_chars": 2048
  }
}
```

The original terminal text is reduced to a short head/tail excerpt with an
omission marker such as:

```text
[tokenjuice-hermes: omitted 42 middle lines]
```

## Install

For package-based use:

```bash
uv add tokenjuice-hermes
```

For direct source installs while testing:

```bash
uv pip install .
```

## Hermes Plugin Layout

Install the plugin directory so Hermes can discover it as:

```text
$HERMES_HOME/plugins/tokenjuice-hermes/
  __init__.py
  compaction.py
  plugin.py
  plugin.yaml
  py.typed
```

The plugin depends on Pydantic v2 for typed JSON boundary parsing. Package-based
installs should install `tokenjuice-hermes` normally; directory-plugin wrappers
must ensure Pydantic is importable in the Hermes Python runtime before enabling
the plugin.

Activation is controlled by Hermes configuration, for example by adding the
plugin name to `plugins.enabled` in the target Hermes profile.

## Validate

Run the local checks with `uv`:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
```

Build the Python package artifacts with:

```bash
uv build
```

## Non-Goals

- This package does not edit Hermes config.
- This package does not enable itself in any running profile.
- This package does not restart, recreate, or otherwise manage a Hermes runtime.
- This package does not rewrite exact file-content reads.

Deployment wrappers should keep activation as a separate operator decision.

## License

MIT. See `LICENSE`.
