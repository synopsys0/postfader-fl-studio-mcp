"""Small product CLI for guided PostFader workflows."""

from __future__ import annotations

import sys

from . import setup_wizard


HELP = """usage: postfader <command> [options]

PostFader command-line workflows.

commands:
  setup    safely prepare the bridge, MIDI endpoint, client configuration,
           and connection evidence for a first FL Studio session

Run `postfader setup --help` for setup options.
"""


def main(argv: list[str] | None = None) -> int:
    """Dispatch a stable top-level command without hiding setup's own help."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print(HELP, end="")
        return 0 if arguments else 2
    command = arguments.pop(0)
    if command == "setup":
        return setup_wizard.main(arguments)
    print("error: unknown PostFader command %r" % command, file=sys.stderr)
    print(HELP, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
