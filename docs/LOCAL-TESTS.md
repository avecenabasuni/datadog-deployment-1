# Local test results

Last checked: 23 September 2026, using a Windows workspace, Python 3.14, and
Git Bash. These tests did not involve deployment, SSH access, production
credentials, or a Docker daemon.

## Results

- `python -m unittest discover -s tests -p 'test_*.py'`: 43 tests passed, including
  lab configuration generation, no credential rotation, dry-run, partial-state refusal,
  environment routing, and fixture safety checks.
- Bash syntax checks (`bash -n`): the entry point and both test scripts passed.
- Tests use temporary directories and a mocked command runner. Coverage includes
  input validation, secret escaping, dry-run, rerun, existing installation/object
  conflicts, timeouts, checksums, and installer/configuration-test failures.

## Not established by local tests

- Real SSI installation, kernel/OS/image compatibility, and injection into the HTTP SAPI.
- SQL/procedure execution on real MySQL 5.7/8.0/8.4 and production account grants.
- Real Agent health, eBPF/runtime security, and log/process data delivery.
- Downstream RUM installer behavior, module persistence after recreation, and browser HTML.
- Trace ingestion, RUM sessions, and RUM–APM/APM–DBM correlation in Datadog.
- Merging snippets into the team's Compose deployment and completing two maintenance/recreation cycles.
- Building/running the new lab Docker images and linting/executing PHP on the Ubuntu VM.

Use production-equivalent staging and the [validation checklist](VALIDATION.md)
before sign-off. Passing unit tests does not establish production deployment success.
