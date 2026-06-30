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
- Supports opt-in aliases for additional terminal-like tool names.
- Supports `head_tail`, `metadata`, and `off` modes.
- Leaves `read_file` results exact by returning `None` from the hook.
- Fails open: invalid JSON, unsupported tools, invalid options, and short outputs
  are left unchanged.

Compacted payloads stay valid JSON and include a `tokenjuice` object:

```json
{
  "tokenjuice": {
    "compacted": true,
    "original_chars": 2048,
    "mode": "head_tail",
    "fields": {
      "stdout": {
        "original_chars": 2048,
        "original_lines": 80,
        "omitted_lines": 75,
        "preview": "first bytes of the original field"
      }
    }
  }
}
```

The original terminal text is reduced to a short head/tail excerpt with an
omission marker such as:

```text
[tokenjuice-hermes: omitted 42 middle lines]
```

## Options

Hermes calls `transform_tool_result` with the tool result, tool name, and any
hook kwargs it provides. This plugin reads only flat kwargs prefixed with
`tokenjuice_`; it does not read environment variables, files, or Hermes runtime
config directly.

| Kwarg | Default | Description |
|---|---:|---|
| `tokenjuice_mode` | `head_tail` | `head_tail`, `metadata`, or `off`. |
| `tokenjuice_min_text_chars` | `240` | Minimum text length that can trigger processing. |
| `tokenjuice_head_lines` | `3` | Lines to keep from the start in `head_tail` mode. |
| `tokenjuice_tail_lines` | `2` | Lines to keep from the end in `head_tail` mode. |
| `tokenjuice_preview_chars` | `72` | Original text preview stored in metadata. |
| `tokenjuice_text_fields` | `stdout,stderr,output` | Comma-separated JSON string fields to inspect. |
| `tokenjuice_tool_aliases` | empty | Comma-separated extra terminal-like tool names. |

Example hook kwargs:

```python
{
    "tokenjuice_mode": "head_tail",
    "tokenjuice_min_text_chars": 1000,
    "tokenjuice_head_lines": 4,
    "tokenjuice_tail_lines": 4,
    "tokenjuice_preview_chars": 120,
    "tokenjuice_text_fields": "stdout,stderr,output,logs",
    "tokenjuice_tool_aliases": "shell,bash,run_command",
}
```

Invalid option values fail open by returning `None` from the hook. Protected
tools are checked before option parsing, so `read_file` cannot be made
compactable through aliases or modes.

## Modes

- `head_tail`: default. Rewrites long text fields to a head/tail excerpt and
  adds structured `tokenjuice` metadata.
- `metadata`: leaves text fields unchanged and adds `tokenjuice` metadata plus
  previews when a field is large enough to process.
- `off`: returns `None` without replacing the tool result.

`stdout`, `stderr`, and `output` remain strings when present. Metadata is kept
under `tokenjuice` so consumers that read the original terminal fields can keep
working.

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

The standalone repository also includes a GitHub Actions workflow that runs the
same checks on Python 3.11, 3.12, and 3.13.

## Non-Goals

- This package does not edit Hermes config.
- This package does not enable itself in any running profile.
- This package does not restart, recreate, or otherwise manage a Hermes runtime.
- This package does not rewrite exact file-content reads.
- This package does not make `read_file` configurable or compactable.

Deployment wrappers should keep activation as a separate operator decision.

## License

MIT. See `LICENSE`.
