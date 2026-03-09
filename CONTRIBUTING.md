# Contributing

## Scope

Contributions should preserve `package_scanner` goals:

- accurate, fail-closed scan/report generation
- clear environment guardrails
- reproducible infrastructure behavior

## Development Guidelines

1. Keep changes small and focused.
2. Update documentation for any user-visible behavior/switch changes.
3. Add or update tests for governance parser/report logic changes.
4. Preserve guardrails:
   - profile/account checks
   - deployment lock token checks
5. Maintain resource tagging strategy in template updates.

## Before Opening PR

Run at minimum:

1. `python -m unittest tests/test_generate_governance_artifacts.py`
2. `python -m py_compile scripts/*.py tests/*.py`
3. Script help checks for modified entrypoints.

## Commit Hygiene

- Use descriptive commit messages.
- Do not commit temp artifacts (`.tmp-tests`, `__pycache__`, `.pyc`).
- Keep API examples synchronized with `api/openapi.yaml`.

