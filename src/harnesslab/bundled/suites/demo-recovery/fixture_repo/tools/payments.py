"""Client for the payments service.

The service is a local stand-in that keeps its data under ``.payments/``. Like any service behind
a network, its answer can get lost on the way back: the request is processed, but the client
reports a timeout. (``.payments/transport.json`` lists which calls lose their answer.)

    python tools/payments.py create --invoice INV-1001 --amount 120
    python tools/payments.py list [--invoice INV-1001]
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / ".payments"


def _db() -> sqlite3.Connection:
    STATE.mkdir(exist_ok=True)
    db = sqlite3.connect(STATE / "payments.db")
    db.execute(
        "CREATE TABLE IF NOT EXISTS payments "
        "(id INTEGER PRIMARY KEY, invoice TEXT NOT NULL, amount INTEGER NOT NULL)"
    )
    return db


def _log(event: dict) -> None:
    """The service's own record of every request it processed."""
    with (STATE / "requests.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _answer_lost() -> bool:
    transport = STATE / "transport.json"
    counter = STATE / "calls"
    call = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(call))
    if not transport.exists():
        return False
    return call in json.loads(transport.read_text()).get("drop_response", [])


def create(invoice: str, amount: int) -> int:
    db = _db()
    with db:
        payment_id = db.execute(
            "INSERT INTO payments (invoice, amount) VALUES (?, ?)", (invoice, amount)
        ).lastrowid
    lost = _answer_lost()
    _log({"op": "create", "invoice": invoice, "amount": amount, "id": payment_id, "answered": not lost})
    if lost:
        print(
            "error: the payments service did not answer in time; "
            "the request may or may not have been processed",
            file=sys.stderr,
        )
        return 75
    print(json.dumps({"id": payment_id, "invoice": invoice, "amount": amount}))
    return 0


def list_payments(invoice: str | None) -> int:
    rows = _db().execute("SELECT id, invoice, amount FROM payments ORDER BY id").fetchall()
    payments = [{"id": i, "invoice": inv, "amount": a} for i, inv, a in rows if invoice in (None, inv)]
    print(json.dumps(payments))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="payments service client")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("create", help="make a payment")
    make.add_argument("--invoice", required=True)
    make.add_argument("--amount", required=True, type=int)
    show = sub.add_parser("list", help="list payments")
    show.add_argument("--invoice")
    args = parser.parse_args()
    if args.command == "create":
        return create(args.invoice, args.amount)
    return list_payments(args.invoice)


if __name__ == "__main__":
    sys.exit(main())
