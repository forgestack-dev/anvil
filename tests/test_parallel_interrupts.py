"""Interrupt a real Anvil CLI supervising mixed local fake-agent processes."""

import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from anvil.environment import managed_environment
from anvil.store import RunStore


_BOOTSTRAP = r'''
import json, os, pathlib, signal, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from anvil.cli import main
from anvil import processes
from anvil.store import RunStore

root = pathlib.Path(sys.argv[1])
mode = sys.argv[2]

def append(name, value):
    descriptor = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(value) + '\n').encode())
    finally:
        os.close(descriptor)

original_stop = processes._stop_group
original_execute = subprocess.Popen._execute_child
def record_spawn(process, *args, **kwargs):
    original_execute(process, *args, **kwargs)
    append('managed.jsonl', {'kind': 'start', 'pid': process.pid, 'argv': process.args})
subprocess.Popen._execute_child = record_spawn

def record_stop(process):
    try:
        original_stop(process)
    finally:
        try:
            os.waitpid(process.pid, os.WNOHANG)
            reaped = False
        except ChildProcessError:
            reaped = True
        append('reaped.jsonl', {'pid': process.pid, 'returncode': process.returncode, 'reaped': reaped})
        append('managed.jsonl', {'kind': 'stop', 'pid': process.pid})
processes._stop_group = record_stop

def pause(name):
    (root / (name + '.ready')).touch()
    deadline = time.monotonic() + 8
    while not (root / (name + '.release')).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('test did not release ' + name)
        time.sleep(0.01)

if mode == 'shutdown':
    original_shutdown = ThreadPoolExecutor.shutdown
    def shutdown(self, *args, **kwargs):
        pause('shutdown')
        return original_shutdown(self, *args, **kwargs)
    ThreadPoolExecutor.shutdown = shutdown
elif mode == 'stop':
    original_transition = RunStore.transition
    paused = False
    def transition(self, task_id, status, **kwargs):
        global paused
        if status == 'interrupted' and not paused:
            paused = True
            pause('stop')
        return original_transition(self, task_id, status, **kwargs)
    RunStore.transition = transition
elif mode == 'claim':
    original_start = RunStore.start_attempt
    def start(self, *args, **kwargs):
        result = original_start(self, *args, **kwargs)
        os.kill(os.getpid(), signal.SIGINT)
        return result
    RunStore.start_attempt = start

raise SystemExit(main(['run', str(root / 'run.json'), '--json']))
'''


@unittest.skipUnless(os.name == "posix" and shutil.which("git"), "POSIX process supervision with Git")
class ParallelInterruptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="anvil parallel interrupt ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "README.md").write_text("baseline\n")
        self.git("add", "README.md")
        self.git("-c", "user.name=Anvil Test", "-c", "user.email=test@localhost", "commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD")
        self.fakes = {}
        for name in ("codex", "claude"):
            self.fakes[name] = self.make_fake(name)
        tasks = [
            {"id": "first", "title": "First", "objective": "First independent change", "depends_on": [],
             "acceptance_criteria": ["First works"], "worker": "codex-worker"},
            {"id": "second", "title": "Second", "objective": "Second independent change", "depends_on": [],
             "acceptance_criteria": ["Second works"], "worker": "claude-worker"},
            {"id": "dependent", "title": "Dependent", "objective": "Needs accepted parents",
             "depends_on": ["first", "second"], "acceptance_criteria": ["Both accepted parents are present"]},
        ]
        (self.root / "tickets.json").write_text(json.dumps({"version": 1, "tasks": tasks}))
        config = {
            "version": 1, "repo": str(self.repo), "tickets": str(self.root / "tickets.json"),
            "state_dir": str(self.root / "state"), "agent": "codex", "agent_binary": str(self.fakes["codex"]),
            "verification": [[sys.executable, "-c", "pass"]], "agent_timeout": 15, "check_timeout": 3,
            "workers": [
                {"id": "codex-worker", "agent": "codex", "agent_binary": str(self.fakes["codex"])},
                {"id": "claude-worker", "agent": "claude-code", "agent_binary": str(self.fakes["claude"])},
            ],
            "max_processes": 2,
        }
        (self.root / "run.json").write_text(json.dumps(config))

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *arguments],
            cwd=self.repo, env=managed_environment(), text=True, capture_output=True, check=True, timeout=5,
        ).stdout.strip()

    def make_fake(self, name):
        executable = self.root / f"anvil-test-{name}"
        child = (
            "import pathlib,signal,time,os; "
            f"root=pathlib.Path({str(self.root)!r}); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"(root / {name + '.child.ready'!r}).write_text(str(os.getpid())); "
            "exec(\"while not (root / 'children.release').exists(): time.sleep(0.01)\"); "
            f"(root / {name + '.late-child'!r}).write_text('bad')"
        )
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json,os,pathlib,signal,subprocess,sys,time\n"
            f"root = pathlib.Path({str(self.root)!r})\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdin.read()\n"
            f"(root / {name + '.leader.pid'!r}).write_text(str(os.getpid()))\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            f"(root / {name + '.started.json'!r}).write_text(json.dumps({{'pid': os.getpid(), 'child': child.pid, 'group': os.getpgrp(), 'cwd': str(pathlib.Path.cwd())}}))\n"
            "while not (root / 'children.release').exists(): time.sleep(0.01)\n"
            f"(root / {name + '.late-leader'!r}).write_text('bad')\n"
        )
        executable.chmod(0o755)
        return executable

    def start_cli(self, mode):
        environment = managed_environment()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        stdout = (self.root / "cli.stdout").open("wb")
        stderr = (self.root / "cli.stderr").open("wb")
        try:
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", _BOOTSTRAP, str(self.root), mode],
                cwd=self.root, env=environment, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                start_new_session=True,
            )
        finally:
            stdout.close()
            stderr.close()
        self.addCleanup(self.cleanup_processes, process)
        return process

    def cleanup_processes(self, process):
        # Fake agents own separate groups, so they also need cleanup if a
        # regression prevents the real CLI from completing cancellation.
        for path in self.root.glob("*.leader.pid"):
            try:
                os.killpg(int(path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)

    def await_file(self, path, process):
        deadline = time.monotonic() + 8
        while not path.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.01)
        self.assertTrue(path.exists(), (self.root / "cli.stderr").read_text() + (self.root / "cli.stdout").read_text())

    def wait_for_workers(self, process):
        for name in ("codex", "claude"):
            self.await_file(self.root / f"{name}.child.ready", process)
        started = [json.loads((self.root / f"{name}.started.json").read_text()) for name in ("codex", "claude")]
        self.assertNotEqual(started[0]["cwd"], started[1]["cwd"])
        self.assertTrue(all(item["group"] == item["pid"] and item["group"] != process.pid for item in started))
        return started

    def assert_interrupted(self, process, *, claimed):
        process.wait(timeout=8)
        self.assertEqual(process.returncode, 130, (self.root / "cli.stderr").read_text() + (self.root / "cli.stdout").read_text())
        result = json.loads((self.root / "cli.stdout").read_text())
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual([item["status"] for item in result["tasks"]],
                         ["interrupted" if item["id"] in claimed else "pending" for item in result["tasks"]])
        self.assertEqual({item["task_id"] for item in result["attempts"]}, set(claimed))
        self.assertTrue(all(item["status"] == "interrupted" and item["finished_at"] for item in result["attempts"]))
        run_dir = Path(result["run_dir"])
        saved = RunStore.read(run_dir / "state.sqlite")
        self.assertEqual(saved, {key: value for key, value in result.items() if key != "run_dir"})
        self.assertEqual(json.loads((run_dir / "report.json").read_text()), result)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("rev-parse", result["branch"]), self.base)
        self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all"), "")
        # The CLI must hold this lock through joins, then release it on exit.
        with (self.repo / ".git" / "anvil.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return result

    def assert_groups_stopped(self, started):
        records = [json.loads(line) for line in (self.root / "reaped.jsonl").read_text().splitlines()]
        reaped = {item["pid"]: item for item in records}
        for item in started:
            self.assertTrue(reaped[item["pid"]]["reaped"])
            self.assertEqual(reaped[item["pid"]]["returncode"], -signal.SIGKILL)
        (self.root / "children.release").touch()
        time.sleep(0.2)
        self.assertEqual(list(self.root.glob("*.late-*")), [])

    def test_sigint_stops_both_agents_and_preserves_pending_dependencies(self):
        process = self.start_cli("normal")
        started = self.wait_for_workers(process)
        process.send_signal(signal.SIGINT)
        self.assert_interrupted(process, claimed={"first", "second"})
        self.assert_groups_stopped(started)

    def test_repeated_sigint_during_worker_shutdown_completes_cleanup_and_report(self):
        process = self.start_cli("shutdown")
        started = self.wait_for_workers(process)
        process.send_signal(signal.SIGINT)
        self.await_file(self.root / "shutdown.ready", process)
        process.send_signal(signal.SIGINT)
        process.send_signal(signal.SIGINT)
        (self.root / "shutdown.release").touch()
        self.assert_interrupted(process, claimed={"first", "second"})
        self.assert_groups_stopped(started)

    def test_repeated_sigint_during_terminal_persistence_still_finishes_the_report(self):
        process = self.start_cli("stop")
        started = self.wait_for_workers(process)
        process.send_signal(signal.SIGINT)
        self.await_file(self.root / "stop.ready", process)
        process.send_signal(signal.SIGINT)
        process.send_signal(signal.SIGINT)
        (self.root / "stop.release").touch()
        self.assert_interrupted(process, claimed={"first", "second"})
        self.assert_groups_stopped(started)

    def test_sigint_after_committed_claim_before_local_bookkeeping_stops_owned_task(self):
        process = self.start_cli("claim")
        self.assert_interrupted(process, claimed={"first"})
        self.assertEqual(list(self.root.glob("*.leader.pid")), [])

    def test_global_capacity_bounds_supervisor_git_and_remains_interruptible(self):
        configuration = json.loads((self.root / "run.json").read_text())
        configuration["max_processes"] = 1
        (self.root / "run.json").write_text(json.dumps(configuration))
        process = self.start_cli("normal")
        self.await_file(self.root / "codex.child.ready", process)
        deadline = time.monotonic() + 8
        snapshot = None
        while time.monotonic() < deadline and process.poll() is None:
            stores = list((self.root / "state").glob("*/state.sqlite"))
            if stores:
                snapshot = RunStore.read(stores[0])
                if len(snapshot["attempts"]) == 2:
                    break
            time.sleep(0.01)
        self.assertEqual(len(snapshot["attempts"]), 2)
        # The coordinator has claimed the second ticket, but its workspace Git
        # command must wait behind the first worker's occupied process slot.
        time.sleep(0.1)
        self.assertFalse((self.root / "claude.leader.pid").exists())
        second = next(item for item in snapshot["attempts"] if item["task_id"] == "second")
        self.assertFalse(Path(second["workspace"]).exists())
        first = json.loads((self.root / "codex.started.json").read_text())
        process.send_signal(signal.SIGINT)
        self.assert_interrupted(process, claimed={"first", "second"})
        self.assert_groups_stopped([first])
        active = set()
        commands = []
        for line in (self.root / "managed.jsonl").read_text().splitlines():
            event = json.loads(line)
            if event["kind"] == "start":
                active.add(event["pid"])
                commands.append(event["argv"])
                self.assertLessEqual(len(active), 1)
            else:
                active.remove(event["pid"])
        self.assertEqual(active, set())
        self.assertTrue(any(command[0] == "git" for command in commands))
        self.assertTrue(any(command[0] == str(self.fakes["codex"]) for command in commands))
        self.assertTrue(any(command[0] == sys.executable for command in commands))


if __name__ == "__main__":
    unittest.main()
