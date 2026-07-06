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
- Rescues oversized results from eligible web/MCP/browser tools by storing the
  full content in a session-scoped blob store and emitting a short preview plus
  an opaque `rescuer_fetch` handle.
- Prunes older terminal-like tool results in outbound LLM requests through
  Hermes' `llm_request` middleware when request history is under context
  pressure.
- Optionally expands rescued handles inside subsequent tool requests through a
  `tool_request` middleware (`passref`). This is **disabled by default** and
  requires an explicit allowlist.
- Preserves structured metadata such as `command`, `exit`, `status`, `cwd`, and
  other fields already present in the tool result.
- Supports opt-in aliases for additional terminal-like tool names.
- Supports `head_tail`, `metadata`, and `off` modes.
- Leaves `read_file` results exact by returning `None` from the hook.
- Preserves `read_file` results during request-history pruning and refuses to
  expand rescued handles into `read_file` or `diagnostics` tools.
- Keeps error diagnostics exact, including `stderr` and traceback-bearing
  `output` fields, while still compacting other large text fields; rescue does
  not apply to error payloads.
- Fails open: invalid JSON, unsupported tools, invalid options, short outputs,
  missing sessions, and missing stores are left unchanged.

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
| `tokenjuice_min_text_chars` | `4000` | Minimum text length that can trigger processing. |
| `tokenjuice_head_lines` | `40` | Lines to keep from the start in `head_tail` mode. |
| `tokenjuice_tail_lines` | `20` | Lines to keep from the end in `head_tail` mode. |
| `tokenjuice_preview_chars` | `160` | Original text preview stored in metadata. |
| `tokenjuice_text_fields` | `stdout,stderr,output` | Comma-separated JSON string fields to inspect. |
| `tokenjuice_tool_aliases` | empty | Comma-separated extra terminal-like tool names. |

Example hook kwargs:

```python
{
    "tokenjuice_mode": "head_tail",
    "tokenjuice_min_text_chars": 4000,
    "tokenjuice_head_lines": 40,
    "tokenjuice_tail_lines": 20,
    "tokenjuice_preview_chars": 160,
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

## Request-History Pruning

On Hermes versions that support middleware registration, `tokenjuice-hermes`
also registers an `llm_request` middleware. This middleware works on the
provider-request copy only: it can shorten old terminal-like tool results before
the next model call, but it does not rewrite the persisted session transcript or
change tool execution.

The middleware uses Hermes context-pressure metadata when available, including
`request_pressure_tokens`, `threshold_tokens`, and `compression_enabled`. On
older hosts that do not provide those fields, it falls back to a request-size
threshold. Recent tail tool results and all `read_file` results remain exact.

For error results, detected by non-zero `exit` or a status such as `failed` or
`error`, `stderr` is preserved exactly. If a tool embeds stderr/traceback text in
`output`, that `output` is also preserved exactly. Other large fields, such as
`stdout`, can still be compacted so failure context is not lost.

## Additive Rescue

For eligible oversized results, `tokenjuice-hermes` can replace the full content
with a short preview plus an opaque handle. The full content is stored in a
per-session blob store and can be retrieved later through the `rescuer_fetch`
tool. Rescue is **additive**: it only runs when the host registers the
`rescuer_fetch` tool, a stable `session_id` is present, the store path exists,
and the result is larger than the configured threshold.

Eligible source tools are: `web_search`, `mcp_tool`, and `browser_snapshot`.
Rescue never applies to `read_file`, terminal-like tools, error payloads, or any
tool whose name starts with `tokenjuice`, `rescuer`, `memory`, `delegate`, or
`session`.

The emitted preview explicitly states that it is not the full content and
includes the handle:

```text
[tokenjuice-hermes: tool result rescued. type=text; size=12345 chars; lines=1200]
Preview only — this is NOT the full content. Use rescuer_fetch(id='...', mode='full') for the complete result.
--- preview ---
...
```

## `rescuer_fetch` Modes

The registered `rescuer_fetch` tool accepts:

| Mode | Description |
|---|---|
| `stat` | Metadata: handle, size, stored time, and source tool. |
| `full` | Full decoded text, refused if over `full_fetch_max_chars` and `refuse_full_fetch` is true. |
| `range` | Line slice from `start` for `count` lines. |
| `grep` | Literal substring search with ReDoS bounds, match cap, and timeout. |

All fetch modes require a valid handle and a matching `session_id`. Cross-session
access is denied even if the blob still exists on disk.

## Passref (Optional Tool-Request Expansion)

On Hermes versions that expose a `tool_request` middleware, `tokenjuice-hermes`
can register a passref middleware that expands `tla:<12-hex-handle>` references
inside tool arguments before the tool runs.

**Passref is disabled by default.** When enabled, expansion only happens for
tools explicitly named in `tokenjuice_passref_allowed_tools`. A built-in sink
denylist dominates the allowlist: `bash`, `curl`, `edit_file`, `exec`,
`execute_code`, `file_write`, `fs_write`, `http_post`, `http_request`,
`run_command`, `run_shell`, `shell`, `subprocess`, `terminal`, `upload`, and
`write_file` are never expanded, even if listed in the allowlist.

`read_file` and `diagnostics` are hard-exempt from passref expansion to preserve
exactness. If a handle appears in arguments for a denied tool, the raw handle is
replaced by a clear marker so the model is not silently passed an unredeemable
reference.

Passref refuses to expand handles unless the calling `session_id` owns the blob.
Missing blobs, cross-session handles, and missing session IDs all fail closed
with explicit markers. Expansion honors per-handle (`tokenjuice_passref_max_chars`)
and per-call (`tokenjuice_passref_total_max_chars`) budgets.

## Security Model

- **Session isolation**: blob authorization is checked against the per-session
  index, not the handle shape. A handle is not redeemable cross-session simply
  because the blob file exists.
- **Sink denylist dominance**: even with a permissive allowlist, execution,
  write, and network sink tools never receive silent passref expansion.
- **Exactness exemptions**: `read_file` and `diagnostics` are hard-exempt from
  both rescue replacement and passref expansion.
- **Literal-only grep**: `rescuer_fetch` grep mode refuses regex metacharacters
  and bounds wall-clock time to mitigate ReDoS.
- **Path validation**: rescue handles must be 12-character lowercase hex; path
  traversal attempts are rejected.
- **Fail open for compaction, fail closed for passref**: malformed inputs are
  left unchanged; unauthorized passref expansion is replaced by a marker rather
  than silently dropped.

## Config Flags

In addition to the compaction kwargs above, rescue and passref read flat kwargs
prefixed with `tokenjuice_`:

| Kwarg | Default | Description |
|---|---:|---|
| `tokenjuice_rescue_store_path` | `/opt/data/tokenjuice-hermes/rescue-blobs` | Base directory for the blob store. |
| `tokenjuice_rescue_min_text_chars` | `4000` | Minimum text length to trigger rescue. |
| `tokenjuice_rescue_tool_names` | `web_search,mcp_tool,browser_snapshot` | Comma-separated eligible rescue tools. |
| `tokenjuice_rescue_excluded_tools` | empty | Comma-separated tools to exclude from rescue. |
| `tokenjuice_rescue_text_fields` | `content,results,snapshot,output,stdout` | Comma-separated fields to inspect. |
| `tokenjuice_rescue_fetch_available` | `false` | Set by the plugin when the host supports `rescuer_fetch`. |
| `tokenjuice_rescue_ttl_hours` | `72` | Blob retention window. |
| `tokenjuice_rescue_tombstone_ttl_hours` | `720` | How long tombstones remain after expiry. |
| `tokenjuice_rescue_max_store_mb` | `500` | Maximum store size before oldest blobs are swept. |
| `tokenjuice_rescue_fetch_max_chars` | `4000` | Maximum chars returned by `range` mode. |
| `tokenjuice_rescue_full_fetch_max_chars` | `50000` | Maximum chars for `full` mode. |
| `tokenjuice_rescue_refuse_full_fetch` | `true` | Refuse `full` requests over the cap. |
| `tokenjuice_passref_enabled` | `false` | Enable passref expansion. |
| `tokenjuice_passref_allowed_tools` | empty | Comma-separated or list of tools allowed to expand handles. |
| `tokenjuice_passref_max_chars` | `500000` | Per-handle expansion cap. |
| `tokenjuice_passref_total_max_chars` | `2000000` | Per-call total expansion cap. |

## Store Lifecycle and Path

The store is rooted at `tokenjuice_rescue_store_path`. Under that path it keeps:

```text
<store_path>/
  blobs/      # content-addressed blob files (12-hex handles)
  sessions/   # per-session JSON indexes mapping handles to metadata
```

Blobs are written atomically. The index is updated under a per-process lock.
`lazy_sweep` tombstones expired blobs, removes blobs with no live references,
and enforces the size cap by deleting oldest content first. Tombstones remain
for the configured secondary TTL so that fetch requests return a clear
"[Swept]" message instead of a silent miss.

## Deferred Live Activation

The source implementation in this repository is complete and persistent. Actual
live behavior inside a Hermes runtime depends on:

- the Hermes profile enabling the `tokenjuice-hermes` plugin;
- the runtime exposing `transform_tool_result`, `llm_request`, `register_tool`,
  and optionally `tool_request` middleware registration;
- a writable rescue store path mounted into the runtime;
- an operator decision to enable passref and populate its allowlist.

Activating the plugin in a running system requires `rebuild`/rebuild and/or a
container recreate; those steps are intentionally deferred to the operator/operator
and are not performed by this task.

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
  json_types.py
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

## Non-Goals

- This package does not edit Hermes config.
- This package does not enable itself in any running profile.
- This package does not enable passref by default or choose an allowlist.
- This package does not restart, recreate, or otherwise manage a Hermes runtime.
- This package does not rewrite exact file-content reads.
- This package does not make `read_file` configurable or compactable.

Deployment wrappers should keep activation, passref enablement, and allowlist
population as separate operator decisions.

## License

MIT. See `LICENSE`.
