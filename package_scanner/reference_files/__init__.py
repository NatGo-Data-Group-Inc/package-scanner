"""Reference-file scanning: data files (CSV, Excel, archives, genomic formats ...) going into the MIP.

Identify every file by content, unpack archives safely, run the checks for its
format, apply a policy, and report what was and was not checked. See
docs/reference-file-scanning.md.
"""
from .scan import scan_submission  # noqa: F401
