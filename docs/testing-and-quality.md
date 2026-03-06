# Testing And Quality

## Current Checks

- Python governance unit tests:
  - `python -m unittest tests/test_generate_governance_artifacts.py`
- Python syntax checks:
  - `python -m py_compile scripts/*.py tests/*.py`
- PowerShell wrapper parse checks (in local shell):
  - parse scriptblocks from `*.ps1`

## Recommended Local Validation Before Merge

1. Run unit tests.
2. Run Python compile checks.
3. Run shell syntax checks for bash scripts (when environment supports it).
4. Dry-run command help output:
   - `./scripts/deploy-cfn.sh --help`
   - `./scripts/start-python-scan.sh --help`
   - `./scripts/start-r-scan.sh --help`

## Suggested CI Quality Gates

- Lint:
  - shellcheck for `*.sh`
  - yamllint/cfn-lint for CloudFormation template
- Unit tests:
  - governance scripts
- Static checks:
  - reject plaintext secrets
  - validate required guardrail arguments in script usage/docs

## Regression Test Ideas

- Python:
  - known vulnerable package fixture (gate fail expected)
  - clean fixture (gate pass expected)
- R:
  - `renv.lock` fixture for representative packages
  - Trivy report schema edge cases

## Definition Of Done (Change Set)

- docs updated for any behavior/switch change
- tests added/updated for parser/gov logic changes
- example payloads updated for API contract changes
- no unintended temporary artifacts committed (`.tmp-tests`, `__pycache__`, `*.pyc`)

