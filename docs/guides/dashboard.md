# The dashboard

```bash
harnesslab serve                     # http://127.0.0.1:8000
harnesslab serve --host 0.0.0.0 --port 8080
```

The dashboard is a local FastAPI application with server-rendered pages and a small JSON API.
It reads the same database the CLI writes, so an experiment that is still running shows its
completed runs as they finish (refresh the page). There is no authentication: bind it to
localhost.

## Pages

| Page | What it shows |
| --- | --- |
| `/` | Every experiment: name, suite, variants, runs, passed, best score, status. Experiments that belong to a grow session carry a role chip linking to the session. |
| `/experiments/{id}` | The **task × variant matrix** (click a cell to open its run; with repetitions each cell shows `k/n`), the variants with their configuration and harness bundle, per-variant aggregates, and every run with its metrics. A sweep shows its recommendation report at the top. |
| `/experiments/{id}/compare?a=&b=` | Two variants side by side: pass rate, score, tokens, LLM calls, wall time, tool calls, cost, and per-task agreement (*both passed*, *both failed*, *A only*, *B only*, *mixed*, *unverified*). |
| `/runs/{id}` | One run: metric cards, the agent's final message (labelled as not evidence of success), the **timeline** of normalized events with collapsible tool and command calls, the git diff against the base commit, the verifier's output, the reproducibility record and the artifacts. |
| `/grow` | Every grow session with status, phase, iterations and accepted versions. |
| `/grow/{id}` | One session: summary cards (iterations, versions, gate pass rate and median `llm_calls` from the initial to the current version, deployed and optimizer cost), the versions table with links to each window and gate experiment and the optimizer's rationale, and the final holdout comparison when it exists. |

The matrix cell symbols are `✔` pass, `✘` fail, `◐` mixed across repetitions, `⚠` infrastructure
failure, `—` no run.

## JSON API

| Endpoint | Returns |
| --- | --- |
| `/api/experiments/{id}/export.json?events=true&artifacts=true` | the same document as `harnesslab experiment export` |
| `/api/experiments/{id}/runs.json` | run samples and per-variant aggregates |
| `/api/runs/{id}/events.json` | the normalized events of one run |
| `/api/grow/{id}.json` | the grow session report (`lineage.json`) |
| `/runs/{id}/artifacts/{kind}` | one artifact as text: `agent_diff`, `diff_stat`, `git_status`, `prompt`, `verifier_stdout`, `verifier_stderr`, `agent_stream`, `agent_stderr` |
| `/healthz` | `{"status": "ok"}` |

## What is deliberately absent

No composite "winner" score: raw metrics sit side by side so you compare on the Pareto frontier
of verified success, cost, latency, tokens and variance. No hidden reasoning: thinking content
is dropped at parse time and the header says so. No charts yet: tables only, consistent across
pages. Live streaming of events while a run executes is on the [roadmap](../project/roadmap.md).
