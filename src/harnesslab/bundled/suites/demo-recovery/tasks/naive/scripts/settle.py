"""Pay every invoice, retrying a call that fails."""

import csv
import subprocess
import sys

for row in csv.DictReader(open("invoices.csv")):
    for _ in range(3):
        made = subprocess.run(
            [sys.executable, "tools/payments.py", "create", "--invoice", row["invoice"],
             "--amount", row["amount"]],
            capture_output=True,
            text=True,
        )
        if made.returncode == 0:
            break
