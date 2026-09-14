"""Explicit status reconciliation and offline routing policy tools."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
from .contracts import ContractError


def add_parsers(commands):
    route = commands.add_parser("route", help="Preview model routing without invoking agents.")
    route.add_argument("config", type=Path)
    route.add_argument("--json", action="store_true")
    tickets = commands.add_parser("tickets", help="Reconcile local ticket status.")
    actions = tickets.add_subparsers(dest="action", required=True)
    sync = actions.add_parser("sync", help="Publish committed run events without rerunning tickets.")
    sync.add_argument("run_dir", type=Path)
    sync.add_argument("--json", action="store_true")
    routing = commands.add_parser("routing", help="Inspect, train, and promote repository routing policies.")
    actions = routing.add_subparsers(dest="action", required=True)
    benchmark = actions.add_parser("benchmark", help="Run paid, bounded comparisons on low-risk fixtures.")
    benchmark.add_argument("config", type=Path)
    benchmark.add_argument("--profile", action="append", required=True)
    benchmark.add_argument("--json", action="store_true")
    for action in ("report", "import"):
        p = actions.add_parser(action)
        p.add_argument("run_dir", type=Path)
        p.add_argument("--json", action="store_true")
    for action in ("train", "promote", "rollback"):
        p = actions.add_parser(action)
        p.add_argument("config", type=Path)
        if action == "promote":
            p.add_argument("policy_id")
        p.add_argument("--json", action="store_true")


def dispatch(args):
    from .config import RunConfig, WorkerConfig
    from .planning import TaskGraph
    from .workspaces import Repository, WorkspaceError
    from .store import RunStore, StoreError
    from .routing import Policy, learning_catalog
    from . import learning
    try:
        if args.command == "tickets":
            from .ticket_status import sync
            result = sync(args.run_dir)
        elif args.command == "route":
            config = RunConfig.load(args.config)
            if config.adaptive is None:
                raise ContractError("routing requires an adaptive configuration")
            if not config.workers:
                config = replace(config, workers=(WorkerConfig("serial", config.agent, config.executable),), max_processes=1)
            graph, repo = TaskGraph.load(config.tickets), Repository(config.repo)
            policy = Policy(config, graph, repo)
            base = repo.head()
            result = {"mode":"preview", "base_sha":base, "decisions":[
                {"task_id":t.id, "worker":w.id, **policy.decision(t,w,base)}
                for t in graph.tasks for w in config.workers if policy.compatible(t,w)]}
        elif args.action == "benchmark":
            from .benchmark import compare
            result = compare(RunConfig.load(args.config), args.profile)
        elif args.action in ("report", "import"):
            from .adaptive_runtime import report
            result = RunStore.read(args.run_dir / "state.sqlite")
            result["run_dir"] = str(args.run_dir.resolve())
            report(result)
            if args.action == "import":
                result = learning.import_run(Repository(Path(result["repo"])), result)
            else:
                result = result.get("routing", {"status":"no routing telemetry"})
        else:
            config = RunConfig.load(args.config)
            if config.adaptive is None:
                raise ContractError("adaptive configuration required")
            repo = Repository(config.repo)
            if args.action == "train":
                options = config.adaptive.get("learning")
                if options is None:
                    raise ContractError("configure explicit learning gates before training")
                result = learning.train(repo, config, **{k:v for k,v in options.items() if k != "auto_promote"})
            elif args.action == "promote":
                result = learning.promote(repo, args.policy_id, learning_catalog(config))
            else:
                result = learning.rollback(repo)
        print(json.dumps(result, indent=2))
        return 1 if result.get("status") == "status_sync_pending" else 0
    except (OSError, ValueError, StoreError, WorkspaceError, sqlite3.Error) as exc:
        print(json.dumps({"error":str(exc)}) if args.json else f"anvil: {exc}", file=sys.stderr)
        return 2
