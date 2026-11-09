# Project Lead Handoff Prompt

Copy and provide the following prompt to the next developer lead:

```text
You are taking over technical leadership of the package-scanner project.

Repository and baseline:
- Path: /home/tjansto/github/natgo/package-scanner
- Branch: feature/conda-preflight-aws
- Last known clean commit: b152942
- Previous major workflow commit: d3143af
- First verify git status, git log, AWS identity, region, and deployed resource state.

Read first:
- README.md
- docs/handoff-runbook.md
- docs/operations-runbook.md
- docs/workflow.md
- docs/catalog-and-ui.md
- docs/architecture.md
- docs/troubleshooting.md
- docs/security-and-governance.md
- docs/testing-and-quality.md

Purpose:
This repository implements an AWS package-security scanner for Python and R.
It produces platform-specific scan results, SBOMs, vulnerability findings,
remediation CSVs, governance summaries, traceability metadata, and optional
deployable/offline environment bundles.

AWS context last verified:
- Account: 807497180525
- Region: us-east-1
- Profile: AdministratorAccess-807497180525
- Main stack: cyber-scanner-dev
- Webapp stack: package-scanner-webapp-dev
- Input bucket: package-scanner-dev-scan-input-807497180525-us-east-1
- Evidence bucket: package-scanner-dev-scan-evidence-807497180525-us-east-1
- Ephemeral bucket: package-scanner-dev-scan-ephemeral-807497180525-us-east-1

Operating model:

1. Requested Python packages:
   - Input is an unversioned package list, not a lockfile.
   - Resolve compatible versions for the target platform and Python version.
   - Temporarily materialize the environment for validation and scanning.
   - Generate the final installed/pinned inventory.
   - Include direct and transitive pip packages in the inventory and SBOM.
   - Generate SBOMs, findings, remediation CSVs, governance summaries, and scan summaries.
   - Do not require a relocatable bundle unless explicitly requested.

2. Locked Python requirements:
   - Input contains pinned versions such as package==version.
   - Scan supplied versions as provided.
   - Materialization is optional unless explicitly requested.
   - Produce the same final reporting artifacts as requested-package mode.

3. Python Conda blueprint:
   - Input is environment.yml.
   - Run Conda preflight against the plan.
   - If materialized, verify the installed inventory against the plan.

4. R:
   - Input is renv.lock.
   - Use Step Functions and ECS-based platform execution.
   - Preserve Linux/Windows and offline/enclave handoff behavior.

All Python paths converge on the resolved or installed inventory, CycloneDX
SBOM, scanner findings, vulnerability-findings.csv, remediation-required.csv,
remediation-exceptions.csv, remediation-spreadsheet.csv, governance summary,
traceability metadata, and scan summary.

Python ECS runtime last verified:
- ECR repository: 807497180525.dkr.ecr.us-east-1.amazonaws.com/package-scanner-dev/python-scan-linux
- Image tag: rhel8-py313-requested
- Digest: sha256:36e2932b4e292326e248e130ff5576d42bfa52352607e51828d7833e39bea315
- Task family: package-scanner-dev-python-linux-amd64
- Revision: 39
- Runtime: Rocky Linux 8 / glibc 2.28 / Python 3.13
- Verify the live image digest and task revision before relying on these values.

Recent implementation work:
- Added requested-package input mode and candidate submission in the GUI.
- Distinguished blueprint, requested-package, and locked-requirements modes.
- Forced requested-package runs to materialize temporarily.
- Ensured pip packages remain in final inventories and scanning.
- Added final pinned inventories and Python materialization summaries.
- Added browser-friendly HTML CSV viewing through /view-csv.
- Preserved separate CSV download links through /download.
- Added bulk cleanup for failed, aborted, and timed-out scan artifacts.
- Fixed cleanup handling for already-missing S3 objects.

Recent operational cleanup:
- Processed 63 failed/aborted Python executions.
- Deleted 483 catalog, orchestration, checkpoint, and run-evidence targets.
- No matching failed/aborted/timed-out R executions were found at that time.
- Successful scan artifacts and shared input objects were preserved.
- Step Functions execution history remains because AWS does not delete it.

GUI notes:
- GUI code is in webapp/app.py and webapp/templates/.
- CSV View links use /view-csv; CSV downloads use /download.
- If /view-csv is missing, the browser is connected to an older local process or hosted image.
- Restart the local GUI or rebuild/redeploy the hosted webapp image.
- Repository source changes do not automatically update the hosted GUI.

Initial checks:
```bash
cd /home/tjansto/github/natgo/package-scanner
git status
git log --oneline -10
aws sts get-caller-identity --profile AdministratorAccess-807497180525
python -m pytest -q
git diff --check
bash scripts/start-python-scan.sh --help
bash scripts/start-r-scan.sh --help
bash scripts/start-webapp.sh --help
```

Before infrastructure changes:
- Inspect CloudFormation outputs.
- Verify the live ECR digest and ECS task-definition revision.
- Verify Step Functions definitions.
- Confirm account and region.
- Identify whether the change is code-only, deployment-only, or both.
- Use dry runs/read-backs before deleting S3, ECS, ECR, or CloudFormation resources.

Priority validation:
1. Verify requested-package and locked-requirements flows remain distinct.
2. Prove transitive pip dependencies appear in final SBOMs and findings.
3. Run a requested-package scan on Rocky Linux 8/glibc 2.28/Python 3.13.
4. Verify all four CSV reports render and download in Firefox and Brave.
5. Confirm the hosted webapp image includes /view-csv.
6. Keep documentation and regression tests synchronized with behavior.
7. Use the approved cleanup utility for failed-artifact cleanup.
8. Keep report generation separate from optional deployable bundle creation.

Progress reports should include commit and working-tree state, AWS account and
region, image digest, task revision, execution ID, input mode, artifact
prefixes, test results, and any local-versus-deployed discrepancy.
```
