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

## Passref Kwargs

| Kwarg | Default | Description |
|---|---:|---|
| `tokenjuice_passref_enabled` | `false` | Enable passref expansion. |
| `tokenjuice_passref_allowed_tools` | empty | Comma-separated or list of tools allowed to expand handles. |
| `tokenjuice_passref_max_chars` | `500000` | Per-handle expansion cap. |
| `tokenjuice_passref_total_max_chars` | `2000000` | Per-call total expansion cap. |

Passref never expands without an explicit allowlist, and sink tools remain denied
even if listed.

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

The plugin uses only the Python standard library at runtime.

## Deployment Boundary

This package does not activate itself. Live Hermes behavior depends on the target
profile enabling the plugin, mounting a writable rescue store, and optionally
enabling passref with an allowlist.

Activation requires `rebuild`/rebuild and/or container recreate in deployments that
derive runtime state from Nix. Those operator steps are outside this package.
