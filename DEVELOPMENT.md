# Local Python unit tests

Use Python 3.12 and an isolated virtual environment from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test-local.txt
source .venv/bin/activate
python -m unittest tests.unit.test_results -v
```

`requirements-test-local.txt` installs the application's pinned dependencies and
the helpers used by Python unit tests without installing browser automation,
documentation builders or JavaScript development tools. The complete upstream
development dependency list remains in `requirements-dev.txt`.

The result-container tests use synthetic results and do not query live search
engines. No search API credentials or running database service are needed for
these tests. Keep additional result identity and merge tests independent of
live engine responses as well.

Store local credentials outside the repository. Do not change user-level
Claude configuration or unrelated services to run these tests.
