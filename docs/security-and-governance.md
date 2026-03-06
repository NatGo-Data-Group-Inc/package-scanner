# Security And Governance

## Security Model

## Identity and Access

- AWS profile usage is explicit by default.
- Optional `--expected-account-id` verifies account targeting.
- Deployment lock token prevents accidental cross-environment operations.

## IAM

- CodeBuild role is scoped to required S3 and logs actions.
- Principle: least privilege for input/evidence/ephemeral bucket paths.

## Artifact Integrity

- Governance script hash is computed at deploy time.
- Build verifies script integrity before execution.
- Governance output manifest records artifact hashes and sizes.

## Data Separation

- Evidence bucket for long-term records.
- Ephemeral bucket for transient build artifacts.
- Traceability metadata per run includes timestamp/platform.

## Governance Outputs

Key records for cyber review:

- vulnerability findings
- remediation required/exceptions
- governance summary
- artifact manifest

## Required Operational Controls

- Keep `--deployment-lock-token` protected and environment-specific.
- Use dedicated AWS profiles per environment.
- Enforce account matching in all production invocations.
- Restrict who can deploy stack updates.

## Recommended Hardening

- Enable CloudTrail and central log aggregation.
- Use KMS-managed encryption keys where required by policy.
- Add S3 bucket policies to constrain principals and source networks.
- Add CI policy checks for template/script changes.

