"""Read-only scaffold commands; this version does not execute tickets."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sys

from anvil import __version__
from anvil.adapters.codex import doctor
from anvil.contracts import ContractError
from anvil.planning import TaskGraph


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="anvil", description="Validate tickets and preview their dependency plan."
    )
    command.add_argument("--version", action="version", version=f"anvil {__version__}")
    subcommands = command.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("validate", "Check a JSON ticket document and its dependencies."),
        ("plan", "Preview dependency waves without running agents."),
    ):
        subcommand = subcommands.add_parser(name, help=help_text)
        subcommand.add_argument("tickets", type=Path)
        subcommand.add_argument("--json", action="store_true", help="Print JSON output.")
    check = subcommands.add_parser("doctor", help="Check Git and Codex CLI availability.")
    check.add_argument("--json", action="store_true", help="Print JSON output.")
    return command


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command == "doctor":
        codex = doctor(probe=True)
        git = shutil.which("git")
        result = {
            "version": __version__,
            "stage": "scaffold",
            "ticket_execution_available": False,
            "git": git,
            "codex": asdict(codex),
        }
        if arguments.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Anvil {__version__} — scaffold")
            print(f"Git: {git or 'not found'}")
            print(f"Codex: {codex.executable or 'not found'}")
            print(f"Codex flags: {'compatible' if codex.compatible else 'unavailable/incompatible'}")
            if codex.error:
                print(f"Codex detail: {codex.error}")
            print("Ticket execution is not implemented in this version.")
        return 0 if git and codex.compatible else 1

    try:
        graph = TaskGraph.load(arguments.tickets)
    except (ContractError, OSError, UnicodeError) as exc:
        if arguments.json:
            print(json.dumps({"valid": False, "error": str(exc)}))
        else:
            print(f"anvil: {exc}", file=sys.stderr)
        return 2

    plan = graph.to_dict()
    if arguments.command == "validate":
        if arguments.json:
            print(json.dumps({"valid": True, "task_count": len(graph.tasks)}))
        else:
            print(f"Valid ticket graph: {len(graph.tasks)} tasks.")
        return 0
    if arguments.json:
        print(json.dumps(plan, indent=2))
    else:
        print("Dependency plan — dry run; no agents or commands will run.")
        for number, wave in enumerate(graph.waves, start=1):
            names = ", ".join(f"{task.id}: {task.title}" for task in wave)
            print(f"Wave {number}: {names}")
        print("Waves show dependency parallelism; shared resources may require serialization.")
    return 0
