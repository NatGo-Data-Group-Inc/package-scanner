#!/usr/bin/env python3
"""Reject a requirements-lock input that carries any requirement without an exact pin.

A requirements-lock input is meant to be scanned exactly as supplied. A line
such as ``numpy`` or ``numpy>=1.26`` is not a lock: pip resolves it to whatever
is current, so the environment that is scanned and approved is not the one that
was submitted. The pipeline used to accept such files silently.

Accepted requirement forms:
  name==1.2.3            name===1.2.3          name[extra]==1.2.3
  name==1.2.3 ; marker   name==1.2.3 --hash=…  name @ https://…/file.whl

Blank lines, comments and global pip options (--index-url, --hash continuation
lines and so on) are ignored. ``-r``/``-c``/``-e`` are rejected because what
they pull in cannot be checked here.

Exit status: 0 when every requirement is pinned, 2 otherwise.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
EXTRAS = r"(?:\s*\[[^\]]*\])?"
EXACT_PIN = re.compile(rf"^{NAME}{EXTRAS}\s*===?\s*[^\s,;*<>=!~]+$")
DIRECT_REFERENCE = re.compile(rf"^{NAME}{EXTRAS}\s*@\s*\S+$")
UNVERIFIABLE_OPTIONS = {"-r", "--requirement", "-c", "--constraint", "-e", "--editable"}
MAX_REPORTED = 20


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Return (first physical line number, line) with continuations joined and comments removed."""
    result: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if not buffer:
            start = number
        if raw.rstrip().endswith("\\"):
            buffer += raw.rstrip()[:-1] + " "
            continue
        line = buffer + raw
        buffer = ""
        # pip treats '#' at the start of a line, or after whitespace, as a comment.
        line = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()
        if line:
            result.append((start, line))
    if buffer.strip():
        result.append((start, buffer.strip()))
    return result


def unpinned_requirements(text: str) -> tuple[int, list[tuple[int, str]]]:
    """Return (number of requirements seen, [(line number, line) for each one not exactly pinned])."""
    seen = 0
    problems: list[tuple[int, str]] = []
    for number, line in logical_lines(text):
        if line.startswith("-"):
            option = line.split()[0].split("=", 1)[0]
            short = option[:2] if not option.startswith("--") else option
            if option in UNVERIFIABLE_OPTIONS or short in UNVERIFIABLE_OPTIONS:
                seen += 1
                problems.append((number, line))
            continue
        seen += 1
        spec = re.split(r"\s*;", line, maxsplit=1)[0]
        spec = re.split(r"\s+--", spec, maxsplit=1)[0].strip()
        if not (EXACT_PIN.match(spec) or DIRECT_REFERENCE.match(spec)):
            problems.append((number, line))
    return seen, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("requirements_file", type=Path)
    args = parser.parse_args(argv)

    try:
        text = args.requirements_file.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        print(f"Guardrail: cannot read requirements-lock input: {error}", file=sys.stderr)
        return 2
    seen, problems = unpinned_requirements(text)

    if seen == 0:
        print(
            f"Guardrail: requirements-lock input {args.requirements_file} contains no requirements.",
            file=sys.stderr,
        )
        return 2
    if not problems:
        print(f"requirements-lock input check passed: {seen} requirements, all exactly pinned.")
        return 0

    print(
        f"Guardrail: requirements-lock input {args.requirements_file} has "
        f"{len(problems)} of {seen} requirements without an exact '==' pin:",
        file=sys.stderr,
    )
    for number, line in problems[:MAX_REPORTED]:
        print(f"  line {number}: {line}", file=sys.stderr)
    if len(problems) > MAX_REPORTED:
        print(f"  ... and {len(problems) - MAX_REPORTED} more", file=sys.stderr)
    print(
        "A requirements-lock input is scanned exactly as supplied, so every package must be "
        "pinned as name==version. Unpinned names resolve to whatever is current, and the "
        "environment approved would not be the one submitted. If current versions are "
        "genuinely intended, use --input-type requested-packages instead.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
