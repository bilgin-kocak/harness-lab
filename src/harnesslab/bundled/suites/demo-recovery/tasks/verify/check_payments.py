"""Hidden check: every invoice was paid exactly once, for its amount.

The final state comes from the service's database, the effect history from its request log: an
invoice paid twice is a duplicate even if one payment was later removed. With --score it prints
the counts as JSON metrics for Harness Lab; otherwise it exits 0 only when everything is right.
"""

import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

STATE = Path(".payments")


def main() -> int:
    invoices = {r["invoice"]: int(r["amount"]) for r in csv.DictReader(open("invoices.csv"))}
    log = STATE / "requests.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    created = Counter(e["invoice"] for e in events if e.get("op") == "create")
    rows = []
    if (STATE / "payments.db").exists():
        rows = sqlite3.connect(STATE / "payments.db").execute(
            "SELECT invoice, amount FROM payments"
        ).fetchall()
    metrics = {
        "duplicates": sum(max(0, created[i] - 1) for i in invoices),
        "missing": sum(1 for i in invoices if created[i] == 0),
        "wrong_amount": sum(1 for inv, amount in rows if inv in invoices and amount != invoices[inv]),
        "unknown_invoice": sum(1 for inv, _ in rows if inv not in invoices),
        "state_mismatch": int(Counter(inv for inv, _ in rows) != created),
    }
    exactly_once = not any(metrics.values())
    metrics["exactly_once"] = int(exactly_once)
    if "--score" in sys.argv:
        print(json.dumps({"score": float(exactly_once), "max_score": 1.0, "metrics": metrics}))
        return 0
    print(json.dumps(metrics))
    return 0 if exactly_once else 1


if __name__ == "__main__":
    sys.exit(main())
