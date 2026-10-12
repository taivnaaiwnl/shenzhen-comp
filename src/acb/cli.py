"""The ``acb`` command (spec 15.3 "CLI mounting").

Every package exposes ``register(subparsers) -> None`` in its ``cli.py``. ``register`` adds its
subcommands with ``subparsers.add_parser(...)`` and sets a handler with
``parser.set_defaults(func=handler)``; ``handler(args)`` returns an exit code (``None`` means 0).
A package that is not built yet (``ImportError``) is listed as "not built"; one that fails to load
for another reason is listed as "failed to load" so the rest of the CLI keeps working.

WP1 itself provides ``acb up``, ``acb down`` (:mod:`acb.launcher`) and ``acb keys`` (:mod:`acb.keys`).
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Sequence

# module -> the top-level commands it provides (used for the placeholders while it is missing)
PACKAGE_CLIS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("acb.chain.cli", ("chain", "deploy")),
    ("acb.bureau.cli", ("bureau",)),
    ("acb.autonomy.cli", ("autonomy",)),
    ("acb.signer.cli", ("signer",)),
    ("acb.agent.cli", ("agent", "chat")),
    ("acb.owner.cli", ("owner",)),
    ("acb.sim.cli", ("sim",)),
    ("acb.scenario.cli", ("scenario", "attacks", "evidence")),
)


def _placeholder(command: str, module: str, status: str):
    def handler(_args: argparse.Namespace) -> int:
        sys.stderr.write(f"acb {command}: {status} ({module})\n")
        return 2

    return handler


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, str]]:
    """The full parser and a map module -> load status (``ok``, ``not built``, ``failed to load: ...``)."""
    parser = argparse.ArgumentParser(
        prog="acb",
        description="Agent Credit Bureau: credit grades for AI agents and a wallet agent that earns its autonomy.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    from acb import keys, launcher

    launcher.register(subparsers)
    keys.register(subparsers)

    status: dict[str, str] = {}
    for module_name, commands in PACKAGE_CLIS:
        try:
            module = importlib.import_module(module_name)
            module.register(subparsers)
            status[module_name] = "ok"
            continue
        except ImportError as exc:
            missing = getattr(exc, "name", None) or ""
            if missing and (module_name == missing or module_name.startswith(missing + ".")):
                status[module_name] = "not built"
            else:
                status[module_name] = f"not built (import error: {exc})"
        except Exception as exc:  # noqa: BLE001 - a broken package must not take the whole CLI down
            status[module_name] = f"failed to load: {type(exc).__name__}: {exc}"
        existing = set(subparsers.choices)
        for command in commands:
            if command in existing:
                continue
            label = "not built" if status[module_name].startswith("not built") else "failed to load"
            p = subparsers.add_parser(command, help=f"({label})", add_help=True)
            p.set_defaults(func=_placeholder(command, module_name, status[module_name]))
    return parser, status


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``acb`` and ``python -m acb``."""
    if (os.environ.get("ACB_PROCESS_ROLE") or "").strip().lower() == "agent":
        from acb.keys import assert_no_private_keys

        assert_no_private_keys()
    parser, _ = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0 if args.command is None else 2
    result = func(args)
    return 0 if result is None else int(result)


if __name__ == "__main__":
    raise SystemExit(main())
