# Catalog And UI

The catalog layer turns raw S3 artifact prefixes into per-run records and stable
pointer documents.

## S3 Layout

- `evidence/catalog/r/runs/<execution-id>.json`
- `evidence/catalog/python/runs/<execution-id>.json`
- `evidence/catalog/r/pointers/latest-run.json`
- `evidence/catalog/python/pointers/latest-run.json`
- `evidence/catalog/r/pointers/latest-successful.json`
- `evidence/catalog/python/pointers/latest-successful.json`
- `evidence/catalog/r/pointers/current-approved.json`
- `evidence/catalog/python/pointers/current-approved.json`

## Catalog Utilities

Backfill:

```bash
python scripts/backfill-scan-catalog.py \
  --bucket <evidence-bucket> \
  --ecosystem r \
  --profile <aws-profile> \
  --write
```

List:

```bash
python scripts/list-scan-catalog.py \
  --bucket <evidence-bucket> \
  --ecosystem python \
  --profile <aws-profile>
```

Promote:

```bash
python scripts/promote-scan-run.py \
  --bucket <evidence-bucket> \
  --ecosystem r \
  --execution-id <execution-id> \
  --approved-by <operator> \
  --profile <aws-profile>
```

## Flask Browser

The Flask browser is a read-only view over the catalog:

```bash
export FLASK_APP=webapp/app.py
export AWS_REGION=us-east-1
export CATALOG_BUCKET=<evidence-bucket>
export CATALOG_PREFIX=evidence
flask run --debug
```

Routes:

- `/`
- `/runs/r`
- `/runs/python`
- `/runs/r/<execution-id>`
- `/runs/python/<execution-id>`
