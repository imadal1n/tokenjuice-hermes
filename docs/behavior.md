# Behavior

`tokenjuice-hermes` has four independent paths. Each path is guarded by the host
surface that supports it.

## Compaction

The `transform_tool_result` hook compacts verbose terminal-like JSON results from
tools such as `terminal`, `execute_code`, and configured aliases.

Modes:

- `head_tail`: replace long text fields with a head/tail excerpt and a
  `[tokenjuice-hermes: omitted N middle lines]` marker.
- `metadata`: keep text unchanged and add preview metadata.
- `off`: return `None` and leave the result unchanged.

Compacted payloads remain JSON and add a `tokenjuice` object with the original
size, mode, field names, line counts, omitted count, and preview. Existing fields
such as `command`, `exit`, `status`, and `cwd` are preserved.

## Request-History Pruning

When the host supports `register_middleware`, the plugin registers an
`llm_request` middleware. It shortens old terminal-like results in the outbound
provider request only. It does not rewrite the saved transcript or change tool
execution.

The middleware uses host pressure metadata when available:

- `request_pressure_tokens`
- `threshold_tokens`
- `compression_enabled`

Older hosts fall back to request-size thresholds. Recent tail results and every
`read_file` result remain exact.

Error results preserve `stderr` exactly. If a tool puts stderr or traceback text
inside `output`, that `output` is also preserved exactly.

## Additive Rescue

Eligible oversized web/MCP/browser results can be stored in a per-session blob
store. The model receives a short preview and an opaque `rescuer_fetch` handle.

Rescue only runs when all of these are true:

- the host registered `rescuer_fetch`;
- a stable `session_id` is present;
- the store path is available;
- the result is larger than the configured threshold.

Default eligible tools:

- `web_search`
- `mcp_tool`
- `browser_snapshot`

Rescue never applies to `read_file`, terminal-like tools, error payloads, or
tools whose names start with `tokenjuice`, `rescuer`, `memory`, `delegate`, or
`session`.

Preview shape:

```text
[tokenjuice-hermes: tool result rescued. type=text; size=12345 chars; lines=1200]
Preview only - this is NOT the full content. Use rescuer_fetch(id='...', mode='full') for the complete result.
--- preview ---
...
```

## `rescuer_fetch`

Fetch modes:

| Mode | Description |
|---|---|
| `stat` | Metadata: handle, size, stored time, and source tool. |
| `range` | Line slice from `start` for `count` lines. |
| `grep` | Literal substring search with match caps and timeout. |
| `full` | Full decoded text, refused when over the configured full-fetch cap. |

Every fetch requires a valid handle and matching `session_id`. Cross-session
access is denied even when the blob file still exists.

## Passref

When the host exposes `tool_request` middleware, the plugin can expand
`tla:<12-hex-handle>` references inside tool arguments before the tool runs.

Passref is disabled by default. When enabled, it still requires:

- a stable `session_id`;
- ownership of the blob by that session;
- an explicit `tokenjuice_passref_allowed_tools` entry for the target tool;
- a target tool that is not sink-denied;
- per-handle and per-call size budgets.

Denied, missing, cross-session, no-session, and truncated expansions return clear
markers. `read_file` and `diagnostics` are hard-exempt.
