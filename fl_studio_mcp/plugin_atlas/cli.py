"""Small bundled-only command-line API for inspecting Plugin Atlas data."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from .loader import AtlasLoadError
from .registry import AtlasRegistry, load_bundled_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="postfader-plugin-atlas",
        description="Inspect the installed local Plugin Atlas bundle.",
    )
    subparsers = parser.add_subparsers(dest="command")

    digest = subparsers.add_parser("digest", help="print the Atlas content digest")
    digest.add_argument("--json", action="store_true", dest="as_json")

    search = subparsers.add_parser("search", help="search product knowledge")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--vendor-id")
    search.add_argument("--kind")
    search.add_argument("--stock-only", action="store_true")
    search.add_argument("--limit", type=int, default=16)
    search.add_argument("--json", action="store_true", dest="as_json")

    show = subparsers.add_parser("show", help="show one product by ID")
    show.add_argument("product_id")
    show.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _product_payload(registry: AtlasRegistry, product_id: str) -> dict[str, Any]:
    product = registry.product(product_id)
    if product is None:
        raise KeyError(product_id)
    return product.model_dump(mode="json")


def run(
    argv: Sequence[str] | None = None,
    *,
    registry: AtlasRegistry | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the CLI against the bundled registry or an injected test registry."""

    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        selected = registry or load_bundled_registry()
        if args.command == "digest":
            if args.as_json:
                print(json.dumps({"digest": selected.digest()}), file=output)
            else:
                print(selected.digest(), file=output)
            return 0
        if args.command == "show":
            payload = _product_payload(selected, args.product_id)
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=output)
            return 0
        if args.command == "search":
            hits = selected.search_hits(
                args.query,
                vendor_id=args.vendor_id,
                kind=args.kind,
                stock_only=args.stock_only,
                limit=args.limit,
            )
            if args.as_json:
                payload = [
                    {
                        "product": hit.product.model_dump(mode="json"),
                        "score": hit.score,
                        "matched_fields": hit.matched_fields,
                    }
                    for hit in hits
                ]
                print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=output)
            else:
                for hit in hits:
                    print(f"{hit.product.product_id}\t{hit.score:.6f}\t{hit.product.name}", file=output)
            return 0
        parser.print_help(output)
        return 0
    except (AtlasLoadError, KeyError, TypeError, ValueError) as exc:
        print(f"postfader-plugin-atlas: {exc}", file=errors)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main", "run"]
