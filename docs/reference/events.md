# Events

Every harness adapter converts its native stream (Codex JSONL, Claude Code stream-json, the
generic JSONL protocol, or direct calls from a Python adapter) into one provider-neutral event
schema. The dashboard timeline, metrics and aggregation only ever see this schema.

## Event

| Field | Meaning |
| --- | --- |
| `event_id` | Time-sortable id. |
| `run_id` | The run. |
| `sequence` | 0-based position in the run; unique per run. |
| `timestamp` | UTC. |
| `kind` | One of the kinds below. |
| `source` | `runner`, `codex`, `claude`, `generic`, `harnesslab`, … |
| `name` | Tool or command name, or a system event name. |
| `duration_ms` | Set on finished events and some system events. |
| `payload` | Kind-specific JSON, redacted before persistence. |
| `call_id` | Pairs `*_started` with `*_finished`. |
| `parent_call_id` | Set for subagent activity. |
| `raw_metadata` | Optional provider metadata, redacted. |

## Kinds and payload conventions

| Kind | Payload |
| --- | --- |
| `run_started` | `task`, `variant`, `runner`, `model`, `repetition`, `base_commit`, `worktree` |
| `run_finished` | `status`, `outcome`, `error`, `exit_code`; `duration_ms` is the wall time |
| `assistant_message` | `text` (final messages are recorded but are not evidence of success) |
| `tool_started` | `tool`, `input` |
| `tool_finished` | `tool`, `status` (`completed`, `error`, `interrupted`), `output` |
| `command_started` | `command`, optional `description` |
| `command_finished` | `command`, `exit_code`, `output`, optional `stderr`, `timed_out`, `status` |
| `file_change` | `path`, `kind` (`add`, `update`, `delete`, …), `tool` |
| `usage` | `input_tokens`, `cached_input_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_output_tokens`, `model` |
| `error` | `message`, plus provider fields |
| `reasoning_event` | `count` only. Thinking text and signatures are dropped at parse time and never persisted. |
| `system` | Named events: `harness_launch` (argv, CLI version, harness hash, applied components), `setup_command`, `verification`, `api_retry`, `compact_boundary`, `permission_denied`, `harness_components_ignored`, `non_json_output`, `unknown_record`, … |

Started and finished events with the same `call_id` are paired in the timeline; a started event
without a matching finish is closed as `interrupted` when the run ends and counts as
`tool_calls_unfinished`.

## Where events go

Events are buffered by the emitter, redacted (provider API keys, GitHub tokens, bearer tokens,
AWS, Slack and Google keys, `NAME=value` assignments for secret-looking names, private-key blocks
and the literal values of secret-looking variables in Harness Lab's own environment), and flushed
to the `events` table in batches while the run is still executing. Real harness runs also keep a
sanitized copy of the provider stream as the `agent_stream` artifact and the CLI's stderr as
`agent_stderr`, both redacted. See [Storage and data layout](storage.md).

## The generic JSONL protocol

A custom harness can emit events directly on stdout with `output_format: jsonl`:

```json
{"kind": "command_started", "call_id": "c1", "payload": {"command": "pytest -q"}}
{"kind": "command_finished", "call_id": "c1", "duration_ms": 1200, "payload": {"exit_code": 0, "output": "5 passed"}}
{"kind": "usage", "payload": {"input_tokens": 1200, "output_tokens": 300}}
```

`kind`, `name`, `call_id`, `duration_ms` and `payload` are honoured; when `payload` is absent the
remaining keys form it. Every `usage` line adds to the totals and counts as one LLM call.
