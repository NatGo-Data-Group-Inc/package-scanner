# Security Policy

## Reporting

If you identify a security issue in this project, report it privately through your organization’s approved security channel. Do not open a public issue with sensitive details.

## Security Expectations

- Use environment-specific AWS profiles.
- Enforce account guardrails with `--expected-account-id`.
- Enforce deployment lock token usage.
- Avoid bypassing scan guardrails in production flows.

## Sensitive Data Handling

- Do not place secrets in:
  - repository files
  - scan input manifests
  - command-line arguments where logs may persist
- Prefer secret managers and scoped IAM access.

## Hardening Recommendations

- CloudTrail enabled for all target accounts.
- Bucket encryption and access controls reviewed periodically.
- IAM least privilege reviewed on change cadence.
- CI scanning/linting enabled for scripts/templates.

