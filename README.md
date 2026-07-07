# tokenjuice-hermes

`tokenjuice-hermes` is a Hermes directory plugin for keeping noisy tool output
out of model context without losing recoverability.

## Status

Alpha. Requires a Hermes runtime with `transform_tool_result`; request pruning,
fetch, passref, and `tokenjuice_status` activate only when the host exposes their
registration hooks.

## Behavior

- Compacts verbose terminal-like JSON results (`terminal`, `execute_code`, and
  configured aliases) with `head_tail`, `metadata`, or `off` modes.
- Prunes older terminal-like results in outbound `llm_request` payloads when
  request history is under context pressure.
- Rescues oversized web/MCP/browser results into a session-scoped blob store and
  emits a preview plus an opaque `rescuer_fetch` handle. Deployment profiles can
  also opt terminal-like tools into rescue before omitted-middle compaction.
- Optionally expands rescued handles inside later tool requests through passref.
  Passref is disabled by default and requires an explicit allowlist.
- Exposes a read-only `tokenjuice_status` tool that returns aggregate counters
  and store statistics. It never returns raw blob content, raw session IDs,
  per-session rows, secrets, transcript snippets, or private paths.
- Keeps `read_file`, diagnostics, `stderr`, traceback-bearing output, malformed
  inputs, unsupported tools, missing sessions, and missing stores exact or
  unchanged.

## Documentation

- [Behavior](docs/behavior.md): compaction, request pruning, rescue/fetch,
  passref, status, and cleanup behavior.
- [Configuration](docs/configuration.md): kwargs, plugin layout, store lifecycle,
  thresholds, and deployment boundaries.
- [Security](docs/security.md): exactness guarantees, session isolation, sink
  protections, status redaction, and non-goals.

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
  compaction_options.py
  hermes_config.py
  json_types.py
  observability.py
  passref.py
  plugin.py
  plugin.yaml
  request_pruning.py
  py.typed
  rescue_excerpt.py
  rescue_fetch.py
  rescue_grep.py
  rescue_handles.py
  rescue_index.py
  rescue_store.py
  rescue_sweep.py
  rescue_transform.py
  rescue_types.py
```

The plugin uses only the Python standard library at runtime. Directory-plugin
wrappers can copy the files directly without adding third-party Python packages
to the Hermes runtime.

Activation is controlled by Hermes configuration, for example by adding the
plugin name to `plugins.enabled` in the target Hermes profile. The `rescuer_fetch`
tool and `tool_request` passref middleware are registered opportunistically only
when the host exposes the corresponding registration surfaces.

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

## Runtime Smoke

After the host is rebuilt, `scripts/runtime_smoke.py` can prove the mounted
plugin loads and exercises rescue/fetch/status through a temporary throwaway
store. It never sends messages to the agent/chat channel, writes to the live rescue store,
or reads secrets. Output is limited to safe booleans and aggregate counts.

```bash
docker exec -i -u 1000:100 <hermes-container> python < scripts/runtime_smoke.py
```

The smoke script is a post-`rebuild` operator verification step, not a substitute for
unit tests or source-level safety checks.

## Non-Goals

- This package does not edit Hermes config.
- This package does not enable itself in any running profile.
- This package does not enable passref by default or choose an allowlist.
- This package does not restart, recreate, or otherwise manage a Hermes runtime.
- This package does not rewrite exact file-content reads.
- This package does not make `read_file` configurable or compactable.

Deployment wrappers should keep activation, passref enablement, and allowlist
population as separate operator decisions.

## Credits

- Based on the official OpenClaw
  [Tokenjuice plugin](https://clawhub.ai/openclaw/plugins/tokenjuice)
  (`@openclaw/tokenjuice`).
- Rescue/fetch and pass-by-reference concepts are informed by
  [Toolaria](https://github.com/Sahil-SS9/Toolaria).

## License

MIT. See `LICENSE`.
