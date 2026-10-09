"""Pay every invoice once. A call that times out may have gone through, so check before retrying."""

import csv
import json
import subprocess
import sys


def payments(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "tools/payments.py", *args], capture_output=True, text=True
    )


def paid(invoice: str) -> bool:
    listed = payments("list", "--invoice", invoice)
    return listed.returncode == 0 and bool(json.loads(listed.stdout or "[]"))


for row in csv.DictReader(open("invoices.csv")):
    for _ in range(3):
        if paid(row["invoice"]):
            break
        if payments("create", "--invoice", row["invoice"], "--amount", row["amount"]).returncode == 0:
            break
