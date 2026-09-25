# Python package validation failure handling

The Linux scanner must stop when package validation fails, before materialization begins.
Successful package validation publishes its plan, SBOM, and vulnerability evidence before
materialization. Failed package validation publishes any evidence available and marks the
checkpoint failed; incomplete evidence is not a successful assessment.

The previous ERR handler disabled errexit while publishing diagnostics and then
returned, allowing the scan to continue. The handler now disables recursive ERR
handling, collects diagnostics, and explicitly exits with the original failure
status. ERR inheritance also covers failures inside functions. Python ECS task
failures no longer trigger the orchestrator's generic task-failure retries.

## Deployment on September 16, 2026

- Region: `us-east-1`
- Python ECS stack: `cyber-scanner-dev-python-ecs` (`UPDATE_COMPLETE`)
- Python ECS task family: `package-scanner-dev-python-linux-amd64` (orchestrator resolves the latest revision)
- ECR repository: `807497180525.dkr.ecr.us-east-1.amazonaws.com/package-scanner-dev/python-scan-linux`
- Image tag: `preflight-failfast-20260916T214000Z` (also refreshed `latest`)
- Image digest: `sha256:15f8f772d08d72e20e3a0e7c28c4d8f8196b2c7886e1e7584e2cc87f585c5eab`
- Base digest: `sha256:69cecf4bbf72d2d44a9eef1b71fb98c7fb973d78af11399deccef19beb008ad9`

The deployed image includes the Conda package-validation helper and the fail-fast
diagnostic handler. The orchestrator references the task family without a
revision, and the task definition resolves the worker repository's `latest`
tag. TaxTriage was not restarted; this change prevents continuation after
failure and does not increase memory.

The package-validation runner and Python helper were recovered from the deployed image
into this checkout so future full builds preserve package-validation behavior. The normal
Linux Dockerfile now includes the package-validation helper.

Validation: package-validation unit tests, shell syntax, and failure-handler regression
tests passed. The failure-handler tests also passed inside the patched image,
covering exit codes 1, 2, 3, and 137 with successful and failed diagnostics.

## Hosted operator GUI

- Stack: `package-scanner-webapp-dev` (`UPDATE_COMPLETE`)
- Image tag: `preflight-failfast-20260916T214000Z` (also refreshed `latest`)
- Image digest: `sha256:89660c60222dcc8be5b353b319393beba435ff8261ceba265e63cef9c1d12fc4`
- URL: `http://package-scanner-dev-webapp-1111678420.us-east-1.elb.amazonaws.com`
- ECS service: one desired task, one running task
- Health check: `/healthz` returned `{"status":"ok"}`
