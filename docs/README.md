# package_scanner Documentation

This folder is the operator guide for `package_scanner`.

## Purpose

`package_scanner` provides a redeployable AWS control/data plane for package vulnerability scanning and governance evidence generation.

- Control plane: submit and track scan jobs (contract in `api/openapi.yaml`).
- Data plane: execute scans in CodeBuild or ECS across platform variants, write evidence/governance artifacts to S3.
- ECS-on-EC2 worker plane assets are present for R and Linux-only Python scanning.
- Ecosystems currently implemented:
 - Python (linux/amd64, linux/arm64, windows/amd64)
  - R (linux/amd64, windows/amd64)

## Workflow Summary

1. Deploy stack with guardrails (`scripts/deploy-cfn.sh`).
2. Optionally deploy ECS stacks for Linux-only Python or R (`scripts/deploy-python-ecs-cfn.sh`, `scripts/deploy-r-ecs-cfn.sh`).
3. Upload environment input file to input bucket.
4. Start scan (`scripts/start-python-scan.sh` or `scripts/start-r-scan.sh`).
4. Review outputs in S3 evidence paths.

Full procedures and examples are in:

- [handoff-runbook.md](./handoff-runbook.md)
- [ecs-cutover-runbook.md](./ecs-cutover-runbook.md)
- [workflow.md](./workflow.md)
- [cli-switch-reference.md](./cli-switch-reference.md)
- [troubleshooting.md](./troubleshooting.md)
- [architecture.md](./architecture.md)
- [catalog-and-ui.md](./catalog-and-ui.md)
- [operations-runbook.md](./operations-runbook.md)
- [security-and-governance.md](./security-and-governance.md)
- [testing-and-quality.md](./testing-and-quality.md)
