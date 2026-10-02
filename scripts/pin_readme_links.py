#!/usr/bin/env python3
"""Pin README.md's relative links and images to the release tag for PyPI.

The README uses relative paths so it renders on GitHub from any branch,
including a pull request. PyPI renders the same file as the package's long
description but cannot resolve relative paths, so the release workflow runs
this script before building: images point at raw files under ``v<version>``
and other links at the tagged GitHub pages. Absolute URLs and in-page anchors
are left alone.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from build_release_bundles import project_version


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "synopsys0/postfader-fl-studio-mcp"
IMAGE_SUFFIXES = (".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp")

MARKDOWN_TARGET = re.compile(r"\]\(([^)\s]+)\)")
HTML_TARGET = re.compile(r'\b(src|href)="([^"]+)"')


def _is_relative(target: str) -> bool:
    return not re.match(r"^(?:[a-z][a-z0-9+.-]*:|#|/)", target, re.IGNORECASE)


def _pinned(target: str, ref: str) -> str:
    if not _is_relative(target):
        return target
    path, _, anchor = target.partition("#")
    if path.lower().endswith(IMAGE_SUFFIXES):
        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{ref}/{path}"
    else:
        kind = "tree" if path.endswith("/") else "blob"
        url = f"https://github.com/{REPOSITORY}/{kind}/{ref}/{path}"
    return f"{url}#{anchor}" if anchor else url


def pin_links(text: str, ref: str) -> str:
    """Return ``text`` with every relative link and image pinned to ``ref``."""

    text = MARKDOWN_TARGET.sub(lambda match: f"]({_pinned(match.group(1), ref)})", text)
    return HTML_TARGET.sub(
        lambda match: f'{match.group(1)}="{_pinned(match.group(2), ref)}"', text
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref",
        help="git ref to pin to (default: v<pyproject version>)",
    )
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    args = parser.parse_args(argv)
    ref = args.ref or f"v{project_version()}"
    original = args.readme.read_text(encoding="utf-8")
    pinned = pin_links(original, ref)
    args.readme.write_text(pinned, encoding="utf-8")
    print(f"pinned README links to {ref}" if pinned != original else "README has no relative links")
    return 0


if __name__ == "__main__":
    sys.exit(main())
