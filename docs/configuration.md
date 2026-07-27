# Configuration

Hermes calls plugin hooks with flat keyword arguments. `tokenjuice-hermes` only
reads `tokenjuice_` kwargs passed by the host; it does not read environment
variables, files, or Hermes runtime config directly.

## Compaction Kwargs

| Kwarg | Default | Description |
|---|---:|---|
| `tokenjuice_mode` | `head_tail` | `head_tail`, `metadata`, or `off`. |
| `tokenjuice_min_text_chars` | `4000` | Minimum text length that can trigger processing. |
| `tokenjuice_head_lines` | `40` | Lines to keep from the start in `head_tail` mode. |
| `tokenjuice_tail_lines` | `20` | Lines to keep from the end in `head_tail` mode. |
| `tokenjuice_preview_chars` | `160` | Original text preview stored in metadata. |
| `tokenjuice_text_fields` | `stdout,stderr,output` | Comma-separated JSON string fields to inspect. |
| `tokenjuice_tool_aliases` | empty | Comma-separated extra terminal-like tool names. |

Protected tools are checked before option parsing, so `read_file` cannot be made
compactable through aliases or modes. Invalid option values fail open by leaving
the result unchanged.

## Rescue Kwargs

| Kwarg | Default | Description |
|---|---:|---|
| `tokenjuice_rescue_store_path` | `/opt/data/tokenjuice-hermes/rescue-blobs` | Base directory for the blob store. |
| `tokenjuice_rescue_min_text_chars` | `4000` | Minimum text length to trigger rescue. |
| `tokenjuice_rescue_tool_min_text_chars` | empty | Per-tool rescue threshold overrides as comma-separated `tool=threshold` pairs (e.g. `web_search=2000,browser_snapshot=8000`). Tools not listed fall back to `tokenjuice_rescue_min_text_chars`. Malformed maps fail open and disable rescue rather than producing dead handles. |
| `tokenjuice_rescue_tool_names` | `web_search,mcp_tool,browser_snapshot` | Comma-separated eligible rescue tools. |
| `tokenjuice_rescue_excluded_tools` | empty | Comma-separated tools to exclude from rescue. |
| `tokenjuice_rescue_text_fields` | `content,results,snapshot,output,stdout` | Comma-separated fields to inspect. |
| `tokenjuice_rescue_fetch_available` | `false` | Set by the plugin when the host supports `rescuer_fetch`. |
| `tokenjuice_rescue_ttl_hours` | `72` | Blob retention window. |
| `tokenjuice_rescue_tombstone_ttl_hours` | `720` | Tombstone retention after expiry. |
| `tokenjuice_rescue_max_store_mb` | `500` | Store size cap before oldest blobs are swept. |
| `tokenjuice_rescue_fetch_max_chars` | `4000` | Maximum chars returned by `range` mode. |
| `tokenjuice_rescue_full_fetch_max_chars` | `50000` | Maximum chars for `full` mode. |
| `tokenjuice_rescue_refuse_full_fetch` | `true` | Refuse `full` requests over the cap. |

Deployment profiles can opt terminal-like tools into recoverable rescue without
lowering the global threshold:

```yaml
tokenjuice_rescue_min_text_chars: 4000
tokenjuice_rescue_tool_names: web_search,mcp_tool,browser_snapshot,terminal,execute_code
tokenjuice_rescue_tool_min_text_chars: terminal=2500,execute_code=2500
tokenjuice_passref_enabled: false
```

With this shape, `terminal` and `execute_code` outputs at or above 2500 chars are
stored behind `rescuer_fetch` handles before omitted-middle compaction is used.
Tools not listed in `tokenjuice_rescue_tool_min_text_chars` still use the global
`tokenjuice_rescue_min_text_chars` value.

When `tokenjuice_rescue_refuse_full_fetch` is `true` and a `mode='full'` request
exceeds `tokenjuice_rescue_full_fetch_max_chars`, the refusal names both config
keys and suggests exact safe alternatives: `mode='range'` with `start`/`count`,
or `mode='grep'` with a literal `pattern`.

## Passref Kwargs

| Kwarg | Default | Description |
|---|---:|---|
| `tokenjuice_passref_enabled` | `false` | Enable passref expansion. |
| `tokenjuice_passref_allowed_tools` | empty | Comma-separated or list of tools allowed to expand handles. |
| `tokenjuice_passref_max_chars` | `500000` | Per-handle expansion cap. |
| `tokenjuice_passref_total_max_chars` | `2000000` | Per-call total expansion cap. |

Passref never expands without an explicit allowlist, and sink tools remain denied
even if listed.

## Structured Pruning Kwargs

Structured pruning only runs when the Hermes host exposes
`structured_context_prune`. It is disabled by default and reads only flat
`tokenjuice_` kwargs supplied by the host.

| Kwarg | Default | Description |
|---|---:|---|
| `tokenjuice_prompt_pruning_enabled` | `false` | Enable structured context pruning before Hermes compression gates. |
| `tokenjuice_prompt_pruning_threshold_tokens` | host threshold | Absolute pressure threshold when the host does not pass one. Invalid or missing values fail open. |
| `tokenjuice_prompt_pruning_trigger_ratio` | `80` | Percent of threshold where soft pruning can start. |
| `tokenjuice_prompt_pruning_target_ratio` | `75` | Default target percent used when soft/hard targets are absent. |
| `tokenjuice_prompt_pruning_soft_target_ratio` | inherits `target_ratio` | Target percent for soft-pressure pruning when explicitly set. |
| `tokenjuice_prompt_pruning_hard_target_ratio` | inherits `target_ratio` | Target percent for hard-pressure pruning when explicitly set. |
| `tokenjuice_prompt_pruning_min_saved_tokens` | `256` | Minimum estimated savings before a candidate is worth mutating. |
| `tokenjuice_prompt_pruning_cache_ttl_seconds` | `3600` | Protects young cache-prefix material unless hard pressure leaves no safe alternative. |
| `tokenjuice_prompt_pruning_protect_recent_messages` | `8` | Recent message window protected from structured pruning. |
| `tokenjuice_prompt_pruning_protect_recent_tool_interactions` | `2` | Recent assistant/tool interaction window protected from structured pruning. |
| `tokenjuice_prompt_pruning_classes` | `terminal_tool_output` | Comma-separated disposable classes. Unknown classes are ignored. |
| `tokenjuice_prompt_pruning_accounting_enabled` | `true` | Emit aggregate redacted counters for status/telemetry. |

Candidate selection first preserves provider-cache locality: if multiple safe
candidate sets can meet the target, TokenJuice chooses the set whose earliest
mutated contribution is latest in provider serialization order. Fewer mutated
groups and larger savings are tie-breakers after the target is met.

Invalid structured-pruning config fails open to the unpruned current-call view.
It does not disable existing terminal-result compaction or `llm_request` fallback
behavior.

## Status Tool

`tokenjuice_status` is registered automatically when the host exposes
`register_tool`. It accepts no arguments and returns a JSON string with aggregate
counters and store statistics. The tool is read-only and never returns raw blob
content, raw session IDs, per-session rows, secrets, transcript snippets, or
private paths. Counters are in-memory for the process lifetime only; store
statistics are computed on demand.

## Store Layout

The rescue store keeps content and indexes under `tokenjuice_rescue_store_path`:

```text
<store_path>/
  blobs/      # content-addressed blob files (12-hex handles)
  sessions/   # per-session JSON indexes mapping handles to metadata
```

Writes are atomic. `lazy_sweep` tombstones expired blobs, removes unreferenced
content, and enforces the size cap by deleting oldest content first. Tombstones
remain for the secondary TTL so fetch can return a clear swept marker.

The `tokenjuice_status` store report contains:

| Field | Meaning |
|---|---|
| `live_blob_count` | Live blobs across all session indexes. |
| `tombstone_count` | Swept blobs kept as tombstones. |
| `total_blob_bytes` | Total bytes of blob files on disk. |
| `blob_file_count` | Number of blob files on disk. |

These values are aggregate and computed on demand; they do not change GC/sweep
policy.

## Hermes Plugin Layout

Install the plugin directory so Hermes can discover it as:

```text
$HERMES_HOME/plugins/tokenjuice-hermes/
  __init__.py
  compaction.py
  compaction_options.py
  hermes_config.py
  json_types.py
  passref.py
  plugin.py
  plugin.yaml
  request_pruning.py
  structured_pruning.py
  structured_pruning_config.py
  structured_pruning_groups.py
  structured_pruning_types.py
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

The plugin uses only the Python standard library at runtime.

## Runtime Smoke

`scripts/runtime_smoke.py` is a post-`rebuild` operator verification script. It loads
the mounted plugin from `/opt/data/plugins/tokenjuice-hermes` (override with
`TOKENJUICE_SMOKE_PLUGIN_PATH`), verifies that `register()` is callable, creates
a temporary throwaway `BlobStore`, exercises rescue/fetch/status through the
plugin, and removes the temporary store.

The smoke script:

- uses a temporary directory, never `/opt/data/tokenjuice-hermes/rescue-blobs`;
- does not send messages to the agent, chat channel, chat channel, or any real chat channel;
- does not read secrets, edit live config, or mutate live store contents;
- prints only safe booleans and aggregate counts.

It is not a substitute for unit tests or source-level safety checks.

## Deployment Boundary

This package does not activate itself. Live Hermes behavior depends on the target
profile enabling the plugin, mounting a writable rescue store for rescue, and
optionally enabling passref or structured pruning with explicit kwargs.

Activation requires `rebuild`/rebuild and/or container recreate in deployments that
derive runtime state from Nix. Those operator steps are outside this package.
