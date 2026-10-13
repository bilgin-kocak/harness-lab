# Hermes Agent fixture streams

These streams were **constructed by hand, not recorded**: Hermes Agent could not be installed
where the adapter was written, and no provider key was available. They follow
`hermes chat --format stream-json` as Hermes Agent's source writes it (its main branch on
2026-10-12, commit 53bc70c, whose `hermes_cli/stream_json.py` matches the v0.21.5 release, tag
v2026.9.24, commit f97608f): `hermes_cli/stream_json.py` for the records,
`tools/terminal_tool.py` and `tools/approval.py` for what a tool result holds, and
`agent/turn_finalizer.py` for the tokens of the `result` record.

- `chat_success.jsonl`: a line that is not JSON first, a read, a patch, a command the approval
  policy blocks, a test run and an answer whose text arrives in pieces.
- `chat_parallel.jsonl`: two shell commands without call ids (paired first in, first out), a
  write outside the allowed paths that Hermes refuses, and a failed `execute_code` call.
- `chat_failed_start.jsonl`: what Hermes writes when credentials or the agent cannot start.

The `SECRET_*` marker is an inline `<think>` span split across text pieces; the tests check that
it is never stored. `../cli_help/hermes-0.21.5-chat.txt` is recorded: `hermes chat --help` of
Hermes Agent v0.21.5 (tag v2026.9.24, commit f97608f) installed from that tag with
`uv sync --locked --extra anthropic`, run with a fresh `HERMES_HOME`, whose temporary path is
replaced by `$HERMES_HOME`.

Replace these with recorded streams once Hermes runs against a model here, and keep the marker
check.
