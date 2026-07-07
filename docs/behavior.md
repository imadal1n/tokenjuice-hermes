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

## Observability and `tokenjuice_status`

When the host exposes `register_tool`, the plugin registers a read-only
`tokenjuice_status` tool. The response is a JSON object with aggregate counters
and store statistics. Counters are kept in memory for the process lifetime only;
store statistics are computed on demand by scanning the rescue store directories
without creating them.

Status fields include:

| Field | Meaning |
|---|---|
| `version` | Plugin version. |
| `passref_enabled` | Whether passref expansion is enabled in this process. |
| `compaction_count` | Number of terminal-like results compacted. |
| `compaction_chars_saved` | Estimated chars removed by compaction. |
| `rescue_count` | Number of oversized results rescued. |
| `rescue_chars_saved` | Estimated chars removed by rescue. |
| `fetch_count` | Number of `rescuer_fetch` invocations. |
| `fetch_modes` | Counts per mode (`stat`, `range`, `grep`, `full`, `invalid`). |
| `passref_expansion_count` | Number of passref expansions performed. |
| `passref_denied_count` | Number of passref requests denied. |
| `passref_truncated_count` | Number of expansions truncated to a budget. |
| `passref_budget_exceeded_count` | Number of calls that hit the total budget marker. |
| `passref_chars_expanded` | Total chars inserted by passref. |
| `store.live_blob_count` | Live blobs across all session indexes. |
| `store.tombstone_count` | Swept blobs kept as tombstones. |
| `store.total_blob_bytes` | Total bytes of blob files on disk. |
| `store.blob_file_count` | Number of blob files on disk. |

`tokenjuice_status` never returns raw blob content, raw session IDs, per-session
rows, secrets, transcript snippets, or private paths. Unknown `rescuer_fetch`
modes are counted as `invalid` rather than creating new buckets.
