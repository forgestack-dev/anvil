"""Planning, supervised execution, and durable run inspection."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys

from anvil import __version__
from anvil.adapters import AGENT_NAMES, probe_agent
from anvil.contracts import ContractError
from anvil.planning import TaskGraph


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="anvil", description="Plan tickets, coordinate coding agents, and inspect saved runs."
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
    check = subcommands.add_parser("doctor", help="Check Git and the selected agent CLI.")
    check.add_argument("--agent", choices=AGENT_NAMES, default="codex")
    check.add_argument("--agent-binary", help="Trusted agent executable name or path.")
    check.add_argument("--json", action="store_true", help="Print JSON output.")
    run = subcommands.add_parser("run", help="Execute a trusted serial or worker-pool configuration.")
    run.add_argument("config", type=Path)
    run.add_argument("--json", action="store_true", help="Print the final run report as JSON.")
    status = subcommands.add_parser("status", help="Read a saved run directory without resuming it.")
    status.add_argument("run_dir", type=Path)
    status.add_argument("--json", action="store_true", help="Print the saved state as JSON.")
    return command


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command == "doctor":
        try:
            agent = probe_agent(arguments.agent, arguments.agent_binary)
        except (ContractError, ValueError, OSError) as exc:
            if arguments.json:
                print(json.dumps({"error": str(exc)}))
            else:
                print(f"anvil: {exc}", file=sys.stderr)
            return 2
        git = shutil.which("git")
        result = {
            "version": __version__,
            "stage": "coordinated-execution",
            "ticket_execution_available": os.name == "posix",
            "worker_pools_available": os.name == "posix",
            "git": git,
            "agent": arguments.agent,
            arguments.agent: asdict(agent),
        }
        if arguments.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Anvil {__version__} — serial and coordinated worker execution")
            print(f"Git: {git or 'not found'}")
            label = "Codex" if arguments.agent == "codex" else "Claude Code"
            print(f"{label}: {agent.executable or 'not found'}")
            print(f"{label} compatibility: {'compatible' if agent.compatible else 'unavailable/incompatible'}")
            if agent.error:
                print(f"{label} detail: {agent.error}")
            print("Execution requires macOS or Linux. Skill loading and recovery are planned.")
        return 0 if git and agent.compatible and os.name == "posix" else 1

    if arguments.command in ("run", "status"):
        from .config import RunConfig
        from .store import RunStore, StoreError
        try:
            if arguments.command == "run":
                if os.name != "posix":
                    raise ContractError("execution currently requires macOS or Linux")
                from .execution import run
                from .workspaces import WorkspaceError
                try:
                    result = run(RunConfig.load(arguments.config),
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
