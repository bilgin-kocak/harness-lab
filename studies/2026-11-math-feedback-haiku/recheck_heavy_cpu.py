"""How much computation do F's proofs of mathd_numbertheory_451 need? (memory limit lifted by swap)

EXPLORATORY (NOT REGISTERED). Written after the held-out phase, 2026-11-26, to settle a question
`RESULTS.md` left open: would any of F's killed proofs of `mathd_numbertheory_451` check within
the registered 300 s if Lean had enough memory? No machine with more than 16 GB was available, so
swap is added and Lean runs with no memory limit but very slowly. Wall-clock time then means
nothing, but CPU time still measures the computation:

* a thread cannot use more CPU time than wall-clock time passes, so if one Lean thread uses more
  than 300 s of CPU before the check ends, the check would take more than 300 s of wall-clock
  time on any machine, and the registered verifier would time it out;
* if the check ends, its verdict (judged by the registered rule) and its CPU time are reported.

Each run stops when one thread's CPU time passes ``--thread-cpu-cap`` (default 330 s) without
the check ending, or after ``--wall-cap`` seconds. The candidate file is rebuilt with the
registered verifier's code, and its SHA-256 must equal the one in the record. No model is called.

Usage (repository root, pinned toolchain on PATH, swap enabled)::

    python studies/2026-11-math-feedback-haiku/recheck_heavy_cpu.py [--cells F:0,F:2,F:3,F:4]

Writes ``recheck_heavy_cpu.json`` in the study directory, adding to it after each cell.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

HEAVY = "mathd_numbertheory_451"
TICK = os.sysconf("SC_CLK_TCK")


def _proc_children(pid: int) -> list[int]:
    out = []
    for t in Path(f"/proc/{pid}/task").glob("*"):
        try:
            out += [int(x) for x in (t / "children").read_text().split()]
        except OSError:
            pass
    return out


def _descendants(pid: int) -> list[int]:
    seen, todo = [], [pid]
    while todo:
        p = todo.pop()
        for c in _proc_children(p):
            if c not in seen:
                seen.append(c)
                todo.append(c)
    return seen


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _thread_cpu(pid: int) -> list[float]:
    """CPU seconds (user + system) of each thread of ``pid``."""
    out = []
    for t in Path(f"/proc/{pid}/task").glob("*"):
        try:
            fields = (t / "stat").read_text().rsplit(")", 1)[1].split()
            out.append((int(fields[11]) + int(fields[12])) / TICK)
        except (OSError, IndexError, ValueError):
            pass
    return out


def _memory_bytes(pid: int) -> tuple[int, int]:
    rss = swap = 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
            elif line.startswith("VmSwap:"):
                swap = int(line.split()[1]) * 1024
    except OSError:
        pass
    return rss, swap


def measure(cmd: list[str], cwd: Path, env: dict, thread_cap: float, wall_cap: float) -> dict:
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True,
    )
    peak_rss = peak_total = 0
    max_thread_cpu = total_cpu = 0.0
    threads_busy = 0
    stopped = None
    samples = []
    out_chunks: list[bytes] = []
    os.set_blocking(proc.stdout.fileno(), False)
    while proc.poll() is None:
        try:
            chunk = proc.stdout.read()
            if chunk:
                out_chunks.append(chunk)
        except (BlockingIOError, TypeError):
            pass
        for p in _descendants(proc.pid):
            if _comm(p) != "lean":
                continue
            cpus = _thread_cpu(p)
            if cpus:
                max_thread_cpu = max(max_thread_cpu, max(cpus))
                total_cpu = max(total_cpu, sum(cpus))
                threads_busy = max(threads_busy, sum(1 for c in cpus if c > 5))
            rss, swap = _memory_bytes(p)
            peak_rss, peak_total = max(peak_rss, rss), max(peak_total, rss + swap)
        wall = time.monotonic() - start
        if not samples or wall - samples[-1][0] >= 60:
            samples.append((round(wall, 1), round(max_thread_cpu, 1), round(peak_total / 1e9, 2)))
        if max_thread_cpu > thread_cap:
            stopped = f"one Lean thread passed {thread_cap:.0f} s of CPU without the check ending"
        elif wall > wall_cap:
            stopped = f"wall-clock cap of {wall_cap:.0f} s"
        if stopped:
            os.killpg(proc.pid, signal.SIGKILL)
            break
        time.sleep(2)
    out, err = proc.communicate()
    out = b"".join(out_chunks) + (out or b"")
    return {
        "returncode": proc.returncode,
        "stopped": stopped,
        "wall_s": round(time.monotonic() - start, 1),
        "max_thread_cpu_s": round(max_thread_cpu, 1),
        "total_cpu_s": round(total_cpu, 1),
        "threads_over_5s_cpu": threads_busy,
        "peak_rss_gb": round(peak_rss / 1e9, 2),
        "peak_rss_plus_swap_gb": round(peak_total / 1e9, 2),
        "samples_wall_s_threadcpu_s_mem_gb": samples,
        "stdout": out.decode("utf-8", errors="replace"),
        "stderr_tail": err.decode("utf-8", errors="replace")[-2000:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cells", default="F:0,F:2,F:3,F:4")
    ap.add_argument("--thread-cpu-cap", type=float, default=330.0)
    ap.add_argument("--wall-cap", type=float, default=7200.0)
    ap.add_argument("--out", type=Path, default=HERE / "recheck_heavy_cpu.json")
    args = ap.parse_args()

    from harnesslab.domains import ensure_builtin_domains
    from harnesslab.domains.lean.toolchain import LeanRun, find_lake, lean_environment, parse_lean_json
    from harnesslab.domains.lean.verifier import build_candidate, judge, parse_problem, sha256_hex
    from harnesslab.study.registry import create_domain
    from harnesslab.study.spec import load_study_spec

    spec = load_study_spec(HERE / "study.yaml")
    ensure_builtin_domains()
    adapter = create_domain(spec.adapter, spec.adapter_options, spec.study_dir)
    task = adapter.load_tasks([HEAVY])[0]
    problem = parse_problem(task.data["source"])
    lake = find_lake()
    env = lean_environment(lake)
    with gzip.open(HERE / "held_out" / "attempts.jsonl.gz", "rt", encoding="utf-8") as f:
        rows = [json.loads(x) for x in f if x.strip()]
    cells = [(c.split(":")[0], int(c.split(":")[1])) for c in args.cells.split(",")]
    results = json.loads(args.out.read_text()) if args.out.exists() else {"rows": []}
    results["label"] = "exploratory (not registered); written after the held-out phase ended"
    results["method"] = __doc__.split("Usage")[0].strip()
    done = {(r["arm"], r["repetition"]) for r in results["rows"]}
    for arm, rep in cells:
        if (arm, rep) in done:
            continue
        rec = next(
            r for r in rows
            if r["task_id"] == HEAVY and r["config_label"] == arm and int(r["repetition"]) == rep
        )
        proof = rec["attempt"]["output"]["proof"]
        cand = build_candidate(problem, proof)
        recorded_sha = rec["verdict"]["details"].get("candidate_sha256")
        same = sha256_hex(cand.text) == recorded_sha
        print(f"{arm} rep {rep}: candidate sha matches the record: {same}", flush=True)
        if not same:
            raise SystemExit("the rebuilt candidate differs from the recorded one")
        with tempfile.TemporaryDirectory(prefix="harnesslab-heavy-") as tmp:
            path = Path(tmp) / "Candidate.lean"
            path.write_text(cand.text, encoding="utf-8")
            m = measure(
                [str(lake), "env", "lean", "--json", str(path)],
                adapter.project_dir, env, args.thread_cpu_cap, args.wall_cap,
            )
        verdict = None
        if m["stopped"] is None:
            messages, unparsed = parse_lean_json(m["stdout"])
            j = judge(problem, cand, LeanRun(returncode=m["returncode"], messages=messages,
                                             duration_s=m["wall_s"], unparsed=unparsed))
            verdict = {"status": getattr(j.status, "value", str(j.status)), "reason": j.reason}
        row = {
            "arm": arm, "repetition": rep, "proof_sha256": rec["attempt"]["output"]["proof_sha256"],
            "candidate_sha256": recorded_sha, "recorded_reason": rec["verdict"]["reason"],
            "recorded_lean_s": rec["verdict"]["details"]["durations"]["lean_s"],
            "verdict_if_ended": verdict,
            **{k: v for k, v in m.items() if k != "stdout"},
        }
        results["rows"].append(row)
        args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in row.items() if k not in ("stderr_tail",)}), flush=True)
    close = getattr(adapter, "close", None)
    if close is not None:
        import asyncio
        asyncio.run(close())
    return 0


if __name__ == "__main__":
    sys.exit(main())
