# pi fixture streams

These streams were **constructed by hand, not recorded**: no provider key was available when the
pi adapter was written. Each record follows pi 1.1.0's JSON mode as its package documents it
(`docs/json.md`, `docs/message-types.md`) and as its code builds it (`dist/modes/json-event.js`,
`dist/core/tools/*.js`, and `dist/agent-loop.js` of `@earendil-works/pi-agent-core`).

- `stream_success.jsonl`: a read, then an edit and a write in parallel, a test run and an answer.
- `stream_no_answer.jsonl`: every model call fails (overloaded) and the retries run out.
- `stream_recovered.jsonl`: a failed call, a retry that works, a compaction and an answer.

`SECRET_*` markers sit everywhere a model's reasoning can appear: thinking blocks and their
deltas, thinking, text and thought signatures, and inline `<think>` spans. The tests check that
none of them is stored. `../cli_help/pi-1.1.0.txt` is recorded: the output of
`pi --help --no-extensions` from a fresh install of `@earendil-works/pi-coding-agent@1.1.0`, with
its own HOME and agent directory.

Replace these streams with recorded ones once a real run is possible, and keep the marker checks.
