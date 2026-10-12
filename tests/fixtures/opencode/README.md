# OpenCode fixture streams

These streams were **constructed by hand, not recorded**: no provider key was available when the
OpenCode adapter was written. They follow what `opencode run --format json` writes, read from
the code of the two versions the adapter was built against: the run command embedded in the
OpenCode 1.19.0 binary (`opencode-ai`), and `packages/opencode/src/cli/cmd/run/` of 2.0.27
(`@opencode/cli`), which emits the same envelope from its v2 session events.

- `run_v1_success.jsonl` (1.x): a read, an edit, a shell command the default policy denies, a
  test run and an answer, over three model calls.
- `run_v1_error.jsonl` (1.x): one model call, then a provider error.
- `run_v2_success.jsonl` (2.x): a V4A patch, a test run and an answer whose last words arrive
  as a continuation of the text block.
- `run_v2_error.jsonl` (2.x): a refused web fetch, then a provider error.

`SECRET_*` markers stand for what must never be stored: an inline `<think>` span, and the
provider state 2.x attaches to tool parts. `../cli_help/opencode-1.19.0-run.txt` and
`../cli_help/opencode-2.0.27-run.txt` are recorded: `opencode run --help` from fresh installs
of those versions, with their own HOME and XDG directories (1.x prints it on stderr).

Replace these streams with recorded ones once a real run is possible, and keep the marker checks.
