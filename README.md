# CSIT321 transfer service and gateway

The implementation is in [kat-transfer](kat-transfer/README.md).
It now follows this path:

`KAT SQLite -> transfer client -> NGINX -> authenticated API -> PostgreSQL`

The client and its diagnostic tools use HTTP; only the API server has database
credentials. Start with the local demonstration in the README, then use the
[deployment guide](kat-transfer/docs/deployment.md) for the BoredOwl server.
