# Reference-file scanning

Scans data files bound for the MIP (CSV, Excel, PDF, archives, genomic formats
and anything else a team submits) for content risks. Data files have no CVEs;
the risks are malware, hidden executables and macros, booby-trapped archives,
secrets, and content that executes when opened.

Status: engine and CLI, run locally. Not yet wired into the AWS pipeline. The
default policy is provisional until Cyber's acceptance criteria are agreed.

## Run it

```bash
python3 scripts/scan-reference-files.py <folder-or-file> --out <evidence-dir> [--policy policy.json] [--workdir <scratch>]
```

Exit status: `0` PASS, `3` REVIEW (a person must look), `4` FAIL (blocked),
`2` usage error. `--workdir` must have room for the unpacked contents of any
archives.

## How it decides

1. **Inventory.** Every file, including files inside archives, gets a manifest
   row with size and SHA-256. Members are named `archive.zip!/inner/file`.
2. **Safe unpacking.** zip, tar (plain, gz, bz2, xz) and single gz/bz2/xz files
   are unpacked recursively. Members are streamed with a running byte count, so
   archives that lie about their sizes still stop at the limits (depth 5,
   200,000 members, ratio 200:1, 2 TiB total by default). Absolute or `../`
   paths, links, devices and encrypted members are refused and reported.
   7z, RAR and zstd are not unpacked and are reported as unchecked.
3. **Identification by content**, never by extension: about 40 signatures
   covering archives, OOXML and legacy Office, PDF, ELF/PE/Mach-O executables,
   scripts, Python pickle and PyTorch checkpoints, FASTA, FASTQ, VCF, BAM (also
   inside BGZF), CRAM, HDF5, Parquet, SQLite, images, JSON and CSV/TSV. A known
   extension on different content is a `TYPE_MISMATCH`.
4. **Checks by format.**

| Check | Applies to | Finding codes |
|---|---|---|
| Malware (ClamAV, one pass over everything unpacked) | all files | `MALWARE_DETECTED`; `MALWARE_NOT_SCANNED` when ClamAV is missing or a file exceeds its limits |
| Code-bearing types | executables, scripts, pickle, PyTorch checkpoints | `EXECUTABLE`, `SCRIPT`, `PICKLE` |
| Office (OOXML) | xlsx/xlsm, docx, pptx | `OFFICE_MACROS`, `OFFICE_EXTERNAL_LINK`, `OFFICE_EMBEDDED_OBJECT`, `OFFICE_REMOTE_TEMPLATE`, `OFFICE_DDE` |
| Office (legacy) | xls, doc, ppt (needs `oletools`) | `OFFICE_MACROS`; `LEGACY_OFFICE_UNCHECKED` without oletools |
| PDF active content | pdf | `PDF_ACTIVE_CONTENT` (JavaScript, OpenAction, Launch, EmbeddedFile ...), `PDF_ENCRYPTED` |
| Formula injection | csv, tsv (also gzipped) | `CSV_FORMULA` (cells starting `=`, `+`, `-`, `@` that are not numbers) |
| Secrets | text, tabular and code files up to 20 GiB; not genomic data | `SECRET` (private keys, AWS keys, tokens, password assignments) |
| Allowed types | every file | `TYPE_NOT_ALLOWED` for kinds not on the allow list |
| Archives | every archive | `ARCHIVE_TRAVERSAL`, `ARCHIVE_LINK`, `ARCHIVE_SPECIAL`, `ARCHIVE_ENCRYPTED`, `ARCHIVE_LIMIT`, `ARCHIVE_CORRUPT`, `ARCHIVE_UNSUPPORTED` |

5. **Policy.** Each finding code maps to `block`, `review` or `info`
   (`package_scanner/reference_files/policy.py`). Unknown codes default to
   `review`; a file kind not on the allow list needs review. A JSON file
   overrides either:

```json
{"name": "example monthly feed", "actions": {"CSV_FORMULA": "info"}, "allowed_kinds": ["csv", "tsv", "vcf", "bam"]}
```

6. **Verdict.** A file is FAIL if any finding blocks, REVIEW if any needs
   review, otherwise PASS; the submission takes the worst file verdict.

## Evidence

| File | Contents |
|---|---|
| `manifest.csv` | Every file: path, parent archive, size, SHA-256, kind, family |
| `findings.csv` | Every finding with its policy action |
| `coverage.csv` | Every file: verdict, checks run, checks not run and why |
| `summary.json` | Verdict, counts by verdict, code and kind, files not fully checked, malware scanner version, the policy used |

Nothing passes silently: a missing scanner, a file too large for ClamAV, an
unreadable file or an unrecognised type all appear as findings and in
`coverage.csv`.

## Large files

Every step streams in 8 MB chunks; a 2 GB CSV peaked at 61 MB of memory.
Throughput was about 23 MB/s on a laptop, dominated by the formula and secrets
checks, so a 10 GB file takes minutes and a 170 GB file about two hours.
ClamAV cannot scan files above roughly 4 GB; such files are reported as
`MALWARE_NOT_SCANNED`, and how to treat them is a policy decision for Cyber.

## Open items

- Cyber's acceptance criteria (which findings block, which types may pass).
- The full list of formats submitters send; add handlers as needed.
- ClamAV and `oletools` in the scan image; offline signature database updates
  for the MIP.
- Wiring into the pipeline: its own container image and ECS task with large
  ephemeral storage, input from S3, evidence to the evidence bucket.
- Faster formula and secrets checks if large-file throughput matters.
