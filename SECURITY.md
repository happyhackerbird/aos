# Security

## Report privately

Use [GitHub private vulnerability reporting](https://github.com/happyhackerbird/aos/security/advisories/new)
for vulnerabilities. Include the affected revision, environment, impact and a
minimal reproduction using synthetic data. Do not include live bearer tokens,
worker authentication stores or another person's private repository contents.

If the private-report form is unavailable, do not post vulnerability details in a
public issue. Ask the maintainer to enable a private reporting channel without
including exploit details. Private reporting availability is a publication gate,
not a service already proven by this document.

## Supported versions

This is a pre-release project. No maintained security-release series or response
SLA is promised. The [validation record](docs/release-validation.md) identifies the
candidate and its open gates; historical passing tests do not qualify a new build.

## Trust boundaries

- The bus is single-process and single-operator, not a multi-tenant service.
  Protect its operator bearer token and keep HTTP on a trusted interface or behind
  an authenticated encrypted transport. `/health` is intentionally unauthenticated.
- Task and integration execution use OpenShell with a committed repository policy.
  A worktree alone, or process-group cleanup alone, is not isolation.
- Use dedicated worker credentials, not personal credential stores. A run credential
  authorizes a narrower set of bus operations and must not be published in output.
- Fresh acceptance excludes the job's worker/run credentials. Disabling fresh
  acceptance weakens that separation. Review operator plugins as executable code.
- Repository exclusions do not remove secrets from historical Git objects. Keep
  credentials outside repository history and review result branches/evidence.
- Model instructions are not authorization boundaries. Acceptance and human review
  remain necessary even when an agent reports success.

## Safe verification

Never run the Linux test suite in the host PID namespace. Use the
[isolated check procedure](CONTRIBUTING.md), which checks namespace separation and
runs tests as an unprivileged user. Tests of refused signal targets must mock the
signal operation; never demonstrate them by signaling host process groups.
