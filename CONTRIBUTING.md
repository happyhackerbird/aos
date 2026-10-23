# Contributing

## Setup

Install development dependencies with `python -m pip install -e '.[dev]'` in a virtual environment. The isolated regression
image uses Python 3.12, matching the harness; Node.js is required
for image-helper/extension contract tests. Real OpenShell/model credentials are
not required for the ordinary regression suite.

## Run checks safely

From the checkout:

```sh
sh scripts/test-isolated.sh
```

To run a focused behavioral test:

```sh
sh scripts/test-isolated.sh tests/test_openshell_runner.py -q
```

The script builds the checked-out source into a disposable test image, compares its
PID namespace against Docker's host namespace, checks UID 1000 and runs pytest in
the isolated container. It never mounts the Docker socket or a credential store.
Do not add `--pid=host`, `--privileged`, host credential mounts or root execution.
The host-namespace probe only reads its namespace identifier; it never runs tests.
On macOS Docker runs these checks in its Linux VM; this is not macOS runtime proof.

Keep the full pytest output, including skips, warnings and deselections. A skip is
not a passing test. Do not bypass a safety failure or silently exclude a failing
check. Model-driven/OpenShell release checks are separate and use disposable buses,
repositories and explicitly owned resources; see [release validation](docs/release-validation.md).

## Changes

- Keep changes focused and update every caller of changed APIs.
- Add regression tests for consumer-visible behavior, boundaries, precedence and
  failure transitions. Avoid tests that merely pin source text or wording.
- Exercise the changed runtime path; unit tests alone are not live integration proof.
- Update affected guides and record verification scope, including what did not run.
- Never commit `.env`, worker logins, run credentials, raw personal transcripts or
  production data. Build public evidence from deliberately selected safe fields.
- Report security defects privately as described in [SECURITY.md](SECURITY.md).

A pull request should state the problem, change, executed checks and qualifications.
AI-assisted contributions must disclose that assistance. Publishing, pushing release
tags and deploying production are separate maintainer decisions.
