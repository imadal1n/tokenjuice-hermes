# Security and Non-Goals

## Guarantees

- `read_file` is exact. It is not compacted, rescued, or passref-expanded.
- Diagnostics stay exact. Error `stderr` and traceback-bearing `output` are
  preserved.
- Rescue handles are session-scoped. Authorization checks the per-session index,
  not only the handle shape.
- Passref is disabled by default and requires an explicit allowlist.
- Structured context pruning is disabled by default and only mutates the
  temporary provider-bound view returned to Hermes. It does not rewrite the saved
  transcript, canonical conversation history, memory, or tool registry.
- Sink tools never receive passref expansion. The sink denylist dominates the
  allowlist.
- Unauthorized passref expansion returns explicit markers instead of silently
  passing raw handles to tools.
- `tokenjuice_status` is aggregate-only and redacted. It never returns raw blob
  content, raw session IDs, per-session rows, secrets, transcript snippets, or
  private paths. Counters are in-memory for the process lifetime only.
- Structured pruning accounting is aggregate-only. It records counts, classes,
  and estimated savings, not raw contribution text, raw file paths, session IDs,
  provider payloads, or secret-bearing data.

## Sink-Denied Tools

These names are never passref-expanded, even if listed in the allowlist:

- `bash`
- `curl`
- `edit_file`
- `exec`
- `execute_code`
- `file_write`
- `fs_write`
- `http_post`
- `http_request`
- `run_command`
- `run_shell`
- `shell`
- `subprocess`
- `terminal`
- `upload`
- `write_file`

## Input Handling

- Invalid JSON and invalid options fail open for compaction/rescue by leaving the
  original result unchanged.
- Missing session IDs prevent rescue handle emission and produce passref denial
  markers when passref is enabled.
- Rescue handles must be 12 lowercase hex characters.
- Fetch `grep` is literal-only and bounded by match caps and timeout.
- Path traversal attempts are rejected.
- Invalid structured-pruning config or unknown contribution classes fail open by
  leaving the current-call view unchanged.
- `read_file`, diagnostics, system/developer/project instructions, memory,
  skills, tool schemas, unknown contributions, and recent protected messages are
  not structured-pruned.

## Non-Goals

- Editing Hermes config.
- Enabling the plugin in a running profile.
- Enabling passref by default or choosing an allowlist.
- Restarting, recreating, or managing a Hermes runtime.
- Rewriting exact file-content reads.
- Rewriting canonical conversation history through structured pruning.
- Using `llm_request` fallback as proof that Hermes compression was delayed.
- Making `read_file` configurable or compactable.
- Vendoring Toolaria or depending on it at runtime.
- Sending synthetic messages through the agent, chat channel, chat channel, or any real chat
  channel as a proof step.
- Writing to the live rescue store during runtime smoke or verification.

Deployment wrappers should keep activation, passref enablement, and allowlist
population as separate operator decisions.
