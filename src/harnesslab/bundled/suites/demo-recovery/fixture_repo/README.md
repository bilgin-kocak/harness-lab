# billing

Outstanding invoices are listed in `invoices.csv`. Payments go through the payments service; its
command-line client is `tools/payments.py`:

```bash
python tools/payments.py create --invoice INV-1001 --amount 120   # make a payment
python tools/payments.py list                                      # every payment
python tools/payments.py list --invoice INV-1001                   # payments for one invoice
```

`create` prints the new payment as JSON. The service is reached over the network, so a call can
time out; a call that times out may or may not have been processed.
