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
from anvil.adapters import AGENT_NAMES, EXECUTION_AGENTS, probe_agent
from anvil.contracts import ContractError
from anvil.planning import TaskGraph
from anvil.processes import ProcessError


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
    check.add_argument("--config", type=Path, help="Also probe explicit model/effort profile controls in a run configuration.")
    check.add_argument("--agent", choices=EXECUTION_AGENTS, default="codex")
    check.add_argument("--agent-binary", help="Trusted agent executable name or path.")
    check.add_argument("--json", action="store_true", help="Print JSON output.")
    run = subcommands.add_parser("run", help="Execute a trusted serial or worker-pool configuration.")
    run.add_argument("config", type=Path)
    run.add_argument("--json", action="store_true", help="Print the final run report as JSON.")
    resume = subcommands.add_parser("resume", help="Continue an interrupted run from verified saved evidence.")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--json", action="store_true")
    status = subcommands.add_parser("status", help="Read a saved run directory without resuming it.")
    status.add_argument("run_dir", type=Path)
    status.add_argument("--json", action="store_true", help="Print the saved state as JSON.")
    prepare = subcommands.add_parser("prepare", help="Generate validated JSON tickets from a committed Markdown specification.")
    prepare.add_argument("source", type=Path)
    prepare.add_argument("--output", "-o", type=Path, required=True)
    prepare.add_argument("--repo", type=Path, default=Path.cwd())
    prepare.add_argument("--agent", choices=EXECUTION_AGENTS, default="codex")
    prepare.add_argument("--agent-binary", help="Trusted agent executable name or path.")
    prepare.add_argument("--gate-agent", choices=(*EXECUTION_AGENTS, "none"),
                         help="Adversarial readiness gate; defaults to the execution agent "
                              "--agent did not select. 'none' disables it and is recorded.")
    prepare.add_argument("--gate-binary", help="Trusted gate executable name or path.")
    prepare.add_argument("--timeout", type=float, default=900)
    prepare.add_argument("--artifact-root", type=Path)
    prepare.add_argument("--json", action="store_true", help="Print JSON output.")
    serve = subcommands.add_parser("serve", help="Read saved and in-progress runs over local HTTP.")
    serve.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/anvil",
                       help="Run state root to read; defaults to ~/.local/state/anvil.")
    serve.add_argument("--host", default="127.0.0.1",
                       help="Loopback address to bind; non-loopback binding is refused.")
    serve.add_argument("--port", type=int, default=8787, help="Port to bind; 0 takes any free port.")
    serve.add_argument("--max-streams", type=int, default=8,
                       help="Concurrent event streams to allow; further streams get 503.")
    skills = subcommands.add_parser("skills", help="Install, update, or inspect managed agent skills.")
    skill_actions = skills.add_subparsers(dest="skill_action", required=True)
    for action, help_text in (
        ("install", "Install AI Hero skills for Codex and Claude Code."),
        ("update", "Update a recorded AI Hero skill installation."),
        ("status", "Inspect a recorded AI Hero skill installation without changing it."),
    ):
        skill_action = skill_actions.add_parser(action, help=help_text)
        skill_action.add_argument("source", choices=("aihero",))
        scope = skill_action.add_mutually_exclusive_group()
        scope.add_argument("--repo", type=Path, help="Target repository; defaults to the current directory.")
        scope.add_argument("--global", dest="global_scope", action="store_true",
                           help="Use the user's global agent skill directories.")
        skill_action.add_argument("--json", action="store_true", help="Print JSON output.")
        if action in ("install", "update"):
            skill_action.add_argument("--ref", default="main", help="Upstream Git revision; defaults to main.")
            skill_action.add_argument("--dry-run", action="store_true",
                                      help="Preview the installation changes without applying them.")
        if action == "install":
            skill_action.add_argument("--agent", choices=("both", *AGENT_NAMES), default="both",
                                      help="Agent skill directories to install into; defaults to both.")
            skill_action.add_argument("--skill", action="append", default=[], metavar="NAME",
                                      help="Select an upstream skill; repeat to select multiple skills.")
            skill_action.add_argument("--include-experimental", action="store_true",
                                      help="Include skills from the upstream experimental directory.")
    from .adaptive_cli import add_parsers
    add_parsers(subcommands)
    return command


def _skill_command(arguments: argparse.Namespace) -> int:
    from . import skill_management

    try:
        scope = skill_management.scope_for(arguments.repo, global_scope=arguments.global_scope)
        if arguments.skill_action == "install":
            agents = AGENT_NAMES if arguments.agent == "both" else (arguments.agent,)
            result = skill_management.install(
                scope, agents=agents, ref=arguments.ref, names=tuple(arguments.skill),
                include_experimental=arguments.include_experimental, dry_run=arguments.dry_run,
            )
        elif arguments.skill_action == "update":
            result = skill_management.update(scope, ref=arguments.ref, dry_run=arguments.dry_run)
        else:
            result = skill_management.status(scope)
    except (ValueError, OSError) as exc:
        if arguments.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"anvil: {exc}", file=sys.stderr)
        return 2
    if arguments.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"AI Hero skills: {result['status']}")
        if result.get("revision"):
            print(f"Revision: {result['revision']}")
        print(f"Skills: {len(result.get('skills', []))}")
        for agent, root in result.get("agents", {}).items():
            print(f"  {agent}: {root}")
        if result.get("manifest"):
            print(f"Manifest: {result['manifest']}")
        if result.get("changes"):
            changes = result["changes"]
            print(f"Changes: {len(changes.get('added', []))} added, "
                  f"{len(changes.get('updated', []))} updated, "
                  f"{len(changes.get('removed', []))} removed")
        for conflict in result.get("conflicts", []):
            print(f"Conflict: {conflict}")
    return 1 if result["status"] == "modified" else 0


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command in ("route", "routing", "tickets"):
        from .adaptive_cli import dispatch
        return dispatch(arguments)
    if arguments.command == "skills":
        return _skill_command(arguments)
    if arguments.command == "prepare":
        try:
            from .preparation import prepare
            from .workspaces import WorkspaceError
            result = prepare(arguments.source, arguments.output, repo=arguments.repo,
                             agent=arguments.agent, executable=arguments.agent_binary,
                             timeout=arguments.timeout, artifact_root=arguments.artifact_root,
                             gate_agent=arguments.gate_agent,
                             gate_executable=arguments.gate_binary)
        except (ContractError, WorkspaceError, ProcessError, OSError) as exc:
            if arguments.json:
                print(json.dumps({"error": str(exc)}))
            else:
                print(f"anvil: {exc}", file=sys.stderr)
            return 2
        if result.get("status") == "needs_clarification":
            if arguments.json:
                print(json.dumps(result, indent=2))
            else:
                raised = "the readiness gate" if result["raised_by"] == "gate" else "preparation"
                print(f"No tickets written: {raised} needs the specification to answer "
                      f"{len(result['questions'])} question(s).", file=sys.stderr)
                for question in result["questions"]:
                    print(f"  [{question['id']}] rule {question['rule']} "
                          f"({question['kind']}): {question['question']}", file=sys.stderr)
                    for reference in question["source_refs"]:
                        print(f"      cites: {reference}", file=sys.stderr)
                    for option in question.get("options", []):
                        print(f"      option: {option}", file=sys.stderr)
                print("Answer them in the specification, commit it, and prepare again.",
                      file=sys.stderr)
            return 3
        if arguments.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Prepared {result['task_count']} tickets in {result['wave_count']} waves.")
            print(f"Output: {result['output']}")
            print(f"Evidence: {result['artifact_dir']}")
            gate = result.get("provenance", {}).get("gate")
            print(f"Gate: {gate['agent']}" if gate else "Gate: disabled")
            if result["unclassified_skills"]:
                print("Unclassified installed skills omitted: " + ", ".join(result["unclassified_skills"]))
        return 0
    if arguments.command == "doctor":
        try:
            profiles = None
            if arguments.config:
                from .config import RunConfig
                from .routing import preflight
                config = RunConfig.load(arguments.config)
                arguments.agent, arguments.agent_binary = config.agent, config.executable
                if config.adaptive:
                    entries = {(worker.agent, worker.executable) for worker in config.workers}
                    entries.add((config.agent, config.executable))
                    profiles = []
                    for selected, binary in sorted(entries):
                        profile = next((p for p in config.adaptive["profiles"].values()
                                        if p["agent"] == selected and "max_budget_usd" in p), None)
                        profiles.append({"agent":selected, "executable":binary,
                                         "version":preflight(selected, binary, profile,
                                                             exclude=config.credential_exclusion),
                                         "model_access":"not probed"})
            agent = probe_agent(arguments.agent, arguments.agent_binary)
        except (ContractError, ValueError, OSError, ProcessError) as exc:
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
            "ticket_skills_available": os.name == "posix",
            "spec_preparation_available": os.name == "posix",
            "git": git,
            "agent": arguments.agent,
            arguments.agent: asdict(agent),
        }
        if profiles is not None:
            result["profile_controls"] = profiles
        if arguments.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Anvil {__version__} — serial and coordinated worker execution")
            print(f"Git: {git or 'not found'}")
            label = {"codex": "Codex", "claude-code": "Claude Code", "muse": "Muse"}[arguments.agent]
            if arguments.agent == "muse":
                print(f"{label}: operator-fulfilled turns (no local CLI)")
            else:
                print(f"{label}: {agent.executable or 'not found'}")
            print(f"{label} compatibility: {'compatible' if agent.compatible else 'unavailable/incompatible'}")
            if agent.error:
                print(f"{label} detail: {agent.error}")
            print("Execution requires macOS or Linux. Native resume and ticket-selected text skill contexts are available for new runs.")
        return 0 if git and agent.compatible and os.name == "posix" else 1

    if arguments.command == "serve":
        from .serve import serve as serve_runs
        try:
            return serve_runs(arguments.state_dir, host=arguments.host, port=arguments.port,
                              max_streams=arguments.max_streams,
                              announce=lambda message: print(message, file=sys.stderr, flush=True))
        except (ContractError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if arguments.command in ("run", "resume", "status"):
        from .config import RunConfig
        from .store import RunStore, StoreError
        try:
            if arguments.command in ("run", "resume"):
                if os.name != "posix":
                    raise ContractError("execution currently requires macOS or Linux")
                from .execution import run
                from .workspaces import WorkspaceError
                try:
                    progress = lambda message: print(message, file=sys.stderr, flush=True)
                    if arguments.command == "resume":
                        from .recovery import resume
                        result = resume(arguments.run_dir, progress=progress)
                    else:
                        result = run(RunConfig.load(arguments.config), progress=progress)
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
