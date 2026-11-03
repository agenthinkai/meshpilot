# Backend tests

Unit tests need no services and don't touch your data. End-to-end tests start
their own API process against a separate `meshpilot_test` database, and use the
running llama.cpp containers and a fake cloud provider. The fake provider means
no data leaves the machine and no API key is needed.

Run from the repository root, with the stack up:

```bash
./backend/tests/run.sh          # unit tests
./backend/tests/run.sh --e2e    # unit and end-to-end tests
```
