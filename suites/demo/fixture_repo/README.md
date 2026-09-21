# ledgerlite

A tiny personal-ledger library (Python standard library only).

Modules:

- `ledgerlite/money.py`  – parsing and formatting of currency amounts (`Decimal`, two places, half-up rounding)
- `ledgerlite/ledger.py` – `Entry` and `Ledger`: adding entries, balances, tag filters, date ranges
- `ledgerlite/report.py` – monthly summaries and text rendering

Conventions:

- Amounts are `decimal.Decimal` quantized to cents. Expenses are negative, income positive.
- Dates are `datetime.date`. Date ranges in the public API are **inclusive on both ends**.
- Run the test-suite with `python -m unittest discover -s tests -v`.
- The files under `tests/` are the project's contract; do not edit them when implementing changes.
