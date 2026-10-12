# Hermes Agent fixture streams

These streams were **constructed by hand, not recorded**: Hermes Agent could not be installed
where the adapter was written, and no provider key was available. They follow
`hermes chat --format stream-json` as the source of tag v2026.9.24 (commit 53bc70c) writes it:
`hermes_cli/stream_json.py` for the records, `tools/terminal_tool.py` and `tools/approval.py` for
what a tool result holds, and `agent/turn_finalizer.py` for the tokens of the `result` record.

- `chat_success.jsonl`: a line that is not JSON first, a read, a patch, a command the approval
  policy blocks, a test run and an answer whose text arrives in pieces.
- `chat_parallel.jsonl`: two shell commands without call ids (paired first in, first out), a
  write outside the allowed paths that Hermes refuses, and a failed `execute_code` call.
- `chat_failed_start.jsonl`: what Hermes writes when credentials or the agent cannot start.

The `SECRET_*` marker is an inline `<think>` span split across text pieces; the tests check that
it is never stored. `../cli_help/hermes-v2026.9.24-chat.reconstructed.txt` is **reconstructed**,
not recorded: the `hermes chat --help` that the argparse definitions of that tag
(`hermes_cli/_parser.py`) produce, written out by hand.

Replace these with recorded streams and help once Hermes runs here, and keep the marker check.
