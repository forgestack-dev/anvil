"""Planning, supervised serial execution, and durable run inspection."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys

from anvil import __version__
from anvil.adapters.codex import doctor
from anvil.contracts import ContractError
from anvil.planning import TaskGraph


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="anvil", description="Plan tickets, execute them serially, and inspect saved runs."
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
    run = subcommands.add_parser("run", help="Execute a trusted run configuration serially.")
    run.add_argument("config", type=Path)
    run.add_argument("--json", action="store_true", help="Print the final run report as JSON.")
    status = subcommands.add_parser("status", help="Read a saved run directory without resuming it.")
    status.add_argument("run_dir", type=Path)
    status.add_argument("--json", action="store_true", help="Print the saved state as JSON.")
    return command


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command == "doctor":
        codex = doctor(probe=True)
        git = shutil.which("git")
        result = {
            "version": __version__,
            "stage": "serial-execution",
            "ticket_execution_available": os.name == "posix",
            "git": git,
            "codex": asdict(codex),
        }
        if arguments.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Anvil {__version__} — serial execution")
            print(f"Git: {git or 'not found'}")
            print(f"Codex: {codex.executable or 'not found'}")
            print(f"Codex flags: {'compatible' if codex.compatible else 'unavailable/incompatible'}")
            if codex.error:
                print(f"Codex detail: {codex.error}")
            print("Serial execution requires macOS or Linux. Skill loading and recovery are planned.")
        return 0 if git and codex.compatible else 1

    if arguments.command in ("run", "status"):
        from .config import RunConfig
        from .store import RunStore, StoreError
        try:
            if arguments.command == "run":
                if os.name != "posix":
                    raise ContractError("serial execution currently requires macOS or Linux")
                from .execution import run_serial
                from .workspaces import WorkspaceError
                try:
                    result = run_serial(RunConfig.load(arguments.config),
                                        progress=lambda message: print(message, file=sys.stderr, flush=True))
                except WorkspaceError as exc:
                    raise ContractError(str(exc)) from exc
            else:
                result = RunStore.read(arguments.run_dir / "state.sqlite")
                result["run_dir"] = str(arguments.run_dir.resolve())
            if arguments.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"Run {result['run_id']}: {result['status']}")
                for task in result["tasks"]:
                    print(f"  {task['id']}: {task['status']}")
                print(f"Integration branch: {result['branch']}")
                print(f"Saved state: {result['run_dir']}")
                if result.get("error"):
                    print(f"Detail: {result['error']}")
            if arguments.command == "status":
                return 0
            return {"success": 0, "blocked": 3, "interrupted": 130}.get(result["status"], 1)
        except (ContractError, StoreError, OSError) as exc:
            if arguments.json:
                print(json.dumps({"error": str(exc)}))
            else:
                print(f"anvil: {exc}", file=sys.stderr)
            return 2

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
