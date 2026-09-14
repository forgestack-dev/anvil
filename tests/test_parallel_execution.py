"""Mixed-agent scheduling against real fake executables and bounded runners."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from textwrap import dedent
import threading
import time
import unittest

from anvil.config import RunConfig, WorkerConfig
from anvil.parallel import run_parallel
from anvil.store import RunStore


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def ticket(task_id, **fields):
    return {"id": task_id, "title": f"Implement {task_id}", "objective": f"Deliver {task_id}",
            "depends_on": [], "acceptance_criteria": [f"{task_id} works"]} | fields


def prompt_ticket(prompt):
    return json.JSONDecoder().raw_decode(prompt.split("Ticket:\n", 1)[1])[0]


def result_for(task_id, *, review=False):
    if review:
        return {"verdict": "approve", "summary": f"Inspected {task_id}", "findings": [],
                "acceptance": [{"criterion": 1, "satisfied": True, "evidence": f"{task_id} inspected"}]}
    return {"status": "completed", "summary": f"Implemented {task_id}", "blockers": [],
            "acceptance": [{"criterion": 1, "evidence": f"{task_id} implemented"}]}


class RecordingRunner:
    def __init__(self, name, events, lock, callback=None):
        self.name, self.events, self.lock, self.callback = name, events, lock, callback

    def run(self, **kwargs):
        task = prompt_ticket(kwargs["prompt"])
        task_id = task["id"]
        review = kwargs.get("read_only", False)
        with self.lock:
            self.events.append(("review" if review else "worker", task_id, self.name))
        if self.callback is not None:
            self.callback(task_id, review, kwargs)
        if not review:
            (kwargs["repo"] / f"{task_id}.txt").write_text(task_id + "\n")
        return result_for(task_id, review=review)


class ParallelExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("Parallel execution fixture\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "Initial fixture")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.tickets = self.root / "tickets.json"
        self.config = RunConfig(
            self.repo, self.tickets, ((sys.executable, "-c", "pass"),), self.root / "state",
            agent_timeout=15, check_timeout=10,
            workers=(WorkerConfig("codex-slot"), WorkerConfig("claude-slot", "claude-code")),
        )

    def write_tickets(self, *tasks):
        self.tickets.write_text(json.dumps({"version": 1, "tasks": list(tasks)}))

    def assert_clean_original(self):
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(sorted(path.name for path in self.repo.glob("*.txt")), [])

    def fake_binary(self, agent, markers):
        # Absolute, private executables are the only configured model entrypoints.
        # No lookup can fall back to an installed Codex or Claude executable.
        binary = self.root / f"anvil-fixture-{agent}"
        binary.write_text(f"#!{sys.executable}\nAGENT = {agent!r}\nMARKERS = {str(markers)!r}\n" + dedent("""\
            import json
            from pathlib import Path
            import subprocess
            import sys
            import time

            markers = Path(MARKERS)
            args = sys.argv[1:]
            prompt = sys.stdin.read()
            task = json.JSONDecoder().raw_decode(prompt.split('Ticket:\\n', 1)[1])[0]
            task_id = task['id']
            if AGENT == 'codex':
                assert args[0] == 'exec'
                schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
                review = args[args.index('--sandbox') + 1] == 'read-only'
            else:
                schema = json.loads(args[args.index('--json-schema') + 1])
                review = 'verdict' in schema['required']
                tools = set(args[args.index('--tools') + 1].split(','))
                assert tools == ({'Read', 'Glob', 'Grep'} if review else {'Read', 'Glob', 'Grep', 'Edit', 'Write'})
                assert 'Bash' in args[args.index('--disallowedTools') + 1].split(',')
            role = 'review' if review else 'worker'
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
            observed = {name: Path(name).read_text() for name in ('alpha.txt', 'beta.txt', 'combined.txt') if Path(name).exists()}
            started = time.monotonic()
            if review:
                assert Path(task_id + '.txt').exists()
                if task_id == 'alpha':
                    (markers / 'alpha-reviewed').write_text('reviewed')
                result = {'verdict': 'approve', 'summary': 'Inspected ' + task_id, 'findings': [],
                          'acceptance': [{'criterion': 1, 'satisfied': True, 'evidence': task_id + ' inspected'}]}
            else:
                if task_id in ('alpha', 'beta'):
                    (markers / (task_id + '-started')).write_text(str(started))
                    peer = 'beta' if task_id == 'alpha' else 'alpha'
                    deadline = time.monotonic() + 8
                    while not (markers / (peer + '-started')).exists():
                        assert time.monotonic() < deadline, 'peer never overlapped'
                        time.sleep(0.01)
                    if task_id == 'alpha':
                        time.sleep(1.2)
                    else:
                        while not (markers / 'alpha-reviewed').exists():
                            assert time.monotonic() < deadline, 'first review did not finish'
                            time.sleep(0.01)
                if task_id == 'combined':
                    assert observed == {'alpha.txt': 'alpha\\n', 'beta.txt': 'beta\\n'}, observed
                    assert 'Implemented alpha' in prompt and 'Implemented beta' in prompt
                Path(task_id + '.txt').write_text(task_id + '\\n')
                result = {'status': 'completed', 'summary': 'Implemented ' + task_id, 'blockers': [],
                          'acceptance': [{'criterion': 1, 'evidence': task_id + ' implemented'}]}
            trace = {'agent': AGENT, 'role': role, 'task': task_id, 'prompt': prompt, 'argv': args,
                     'schema': schema, 'head': head, 'observed': observed,
                     'started': started, 'finished': time.monotonic(), 'cwd': str(Path.cwd()),
                     'entrypoint': sys.argv[0]}
            (markers / (task_id + '-' + role + '.json')).write_text(json.dumps(trace))
            if AGENT == 'codex':
                Path(args[args.index('-o') + 1]).write_text(json.dumps(result))
                print(json.dumps({'type': 'fixture_complete'}))
            else:
                print(json.dumps({'type': 'system', 'subtype': 'init'}))
                print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                  'permission_denials': [], 'structured_output': result}))
                print(json.dumps({'type': 'system', 'subtype': 'task_summary', 'detail': None,
                                  'uuid': 'fixture-trailer', 'session_id': 'fixture-session'}))
            """))
        binary.chmod(0o755)
        return binary

    def test_real_adapters_overlap_integrate_latest_parent_and_handoff_accepted_dependencies(self):
        markers = self.root / "markers"
        markers.mkdir()
        codex = self.fake_binary("codex", markers)
        claude = self.fake_binary("claude-code", markers)
        check = self.root / "verify.py"
        check.write_text(dedent("""\
            import json
            from pathlib import Path
            import subprocess
            import sys
            values = {path.name: path.read_text() for path in Path.cwd().glob('*.txt')}
            assert all(value == name[:-4] + '\\n' for name, value in values.items()), values
            if 'combined.txt' in values:
                assert 'alpha.txt' in values and 'beta.txt' in values
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
            with Path(sys.argv[1]).open('a') as stream:
                stream.write(json.dumps({'head': head, 'values': values}) + '\\n')
            """))
        checks = self.root / "checks.jsonl"
        self.write_tickets(ticket("alpha", worker="codex-slot"),
                           ticket("beta", worker="claude-slot"),
                           ticket("combined", worker="codex-slot", depends_on=["alpha", "beta"]))
        config = replace(self.config, agent="claude-code", agent_binary=str(claude),
                         workers=(WorkerConfig("codex-slot", "codex", str(codex)),
                                  WorkerConfig("claude-slot", "claude-code", str(claude))),
                         verification=((sys.executable, str(check), str(checks)),))
        result = run_parallel(config)
        self.assertEqual(result["status"], "success", result["error"])
        tasks = {task["id"]: task for task in result["tasks"]}
        attempts = {attempt["task_id"]: attempt for attempt in result["attempts"]}
        traces = {(task_id, role): json.loads((markers / f"{task_id}-{role}.json").read_text())
                  for task_id in tasks for role in ("worker", "review")}
        alpha, beta = traces["alpha", "worker"], traces["beta", "worker"]
        self.assertLess(max(alpha["started"], beta["started"]), min(alpha["finished"], beta["finished"]))
        self.assertNotEqual(alpha["cwd"], beta["cwd"])
        self.assertEqual((alpha["agent"], beta["agent"]), ("codex", "claude-code"))
        self.assertEqual((alpha["head"], beta["head"]), (self.base, self.base))
        self.assertEqual((alpha["entrypoint"], beta["entrypoint"]), (str(codex), str(claude)))
        expected_parent = self.base
        checked = [json.loads(line) for line in checks.read_text().splitlines()]
        self.assertEqual(len(checked), 4)
        self.assertEqual(checked[0], {"head": self.base, "values": {}})
        for task_id, worker_id in (("alpha", "codex-slot"), ("beta", "claude-slot"),
                                   ("combined", "codex-slot")):
            details = tasks[task_id]["details"]
            sha = details["integrated_sha"]
            review = traces[task_id, "review"]
            self.assertEqual(tasks[task_id]["status"], "done")
            self.assertEqual(details["worker_id"], worker_id)
            self.assertEqual(details["agent"], traces[task_id, "worker"]["agent"])
            self.assertEqual((details["reviewed_sha"], details["verified_sha"], review["head"]), (sha, sha, sha))
            self.assertEqual(git(self.repo, "rev-parse", sha + "^"), expected_parent)
            self.assertEqual(git(self.repo, "rev-parse", details["candidate_sha"] + "^"),
                             attempts[task_id]["base_sha"])
            expected_diff = git(self.repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                                "--no-renames", "--ignore-submodules=none", expected_parent, sha, "--")
            self.assertIn(expected_diff, review["prompt"])
            self.assertIn(f"+{task_id}", review["prompt"])
            self.assertIn(f"Diff from {expected_parent} to {sha}:", review["prompt"])
            self.assertTrue(any(entry["head"] == sha for entry in checked))
            self.assertTrue(all(check["returncode"] == 0 and not check["timed_out"]
                                for check in details["verification"]))
            for role in ("worker", "review"):
                artifacts = Path(result["run_dir"]) / "artifacts" / attempts[task_id]["id"] / role
                self.assertEqual(json.loads((artifacts / "result.json").read_text()), details[role])
                self.assertEqual(json.loads((artifacts / "schema.json").read_text()), traces[task_id, role]["schema"])
                if traces[task_id, role]["agent"] == "claude-code":
                    events = [json.loads(line) for line in (artifacts / "events.jsonl").read_text().splitlines()]
                    self.assertEqual(events[-2]["type"], "result")
                    self.assertEqual((events[-1]["type"], events[-1]["subtype"]), ("system", "task_summary"))
            expected_parent = sha
        self.assertNotEqual(tasks["beta"]["details"]["candidate_sha"], tasks["beta"]["details"]["integrated_sha"])
        self.assertEqual(attempts["combined"]["base_sha"], tasks["beta"]["details"]["integrated_sha"])
        self.assertEqual(traces["combined", "worker"]["observed"], {"alpha.txt": "alpha\n", "beta.txt": "beta\n"})
        self.assertEqual(git(self.repo, "rev-parse", result["branch"]), expected_parent)
        for task_id in tasks:
            self.assertEqual(git(self.repo, "show", f"{result['branch']}:{task_id}.txt"), task_id)
        self.assertIn("heartbeat_at", tasks["alpha"]["details"])
        self.assertIn("heartbeat_at", tasks["beta"]["details"])
        dispatch = [event for event in result["events"] if event["kind"] == "dispatch"]
        self.assertEqual({event["task_id"]: event["details"]["worker_id"] for event in dispatch},
                         {"alpha": "codex-slot", "beta": "claude-slot", "combined": "codex-slot"})
        messages = [event for event in result["events"] if event["kind"] == "message"]
        for task_id in tasks:
            self.assertEqual({event["details"]["message_kind"] for event in messages
                              if event["task_id"] == task_id},
                             {"dependency_handoff", "worker_result", "integration", "review_result"})
        handoff = next(event["details"]["body"]["dependencies"] for event in messages
                       if event["task_id"] == "combined" and event["details"]["message_kind"] == "dependency_handoff")
        self.assertEqual([item["task_id"] for item in handoff], ["alpha", "beta"])
        for item in handoff:
            self.assertEqual(item["integrated_sha"], tasks[item["task_id"]]["details"]["integrated_sha"])
        beta_integration = next(event["details"]["body"] for event in messages
                                if event["task_id"] == "beta" and event["details"]["message_kind"] == "integration")
        self.assertEqual(beta_integration["worker_base"], self.base)
        self.assertEqual(beta_integration["accepted_base"], tasks["alpha"]["details"]["integrated_sha"])
        self.assertEqual(RunStore.read(Path(result["run_dir"]) / "state.sqlite"),
                         {key: value for key, value in result.items() if key != "run_dir"})
        self.assertEqual(json.loads((Path(result["run_dir"]) / "report.json").read_text()), result)
        self.assert_clean_original()

    def injected_run(self, tasks, callback=None):
        self.write_tickets(*tasks)
        events, lock = [], threading.Lock()
        runners = {worker.id: RecordingRunner(worker.id, events, lock, callback)
                   for worker in self.config.workers}
        reviewer = RecordingRunner("reviewer", events, lock, callback)

        def progress(message):
            if message.endswith(": done"):
                with lock:
                    events.append(("done", message.split(":", 1)[0], "coordinator"))

        result = run_parallel(self.config, runners=runners, review_runner=reviewer, progress=progress)
        self.assertEqual(result["status"], "success", result["error"])
        self.assert_clean_original()
        return result, events

    def test_shared_resource_and_exclusive_tasks_hold_ownership_through_acceptance(self):
        result, events = self.injected_run([
            ticket("a", worker="codex-slot", resources=["lockfile"]),
            ticket("b", worker="claude-slot", resources=["lockfile"]),
            ticket("exclusive", worker="codex-slot", depends_on=["a"], exclusive=True),
            ticket("after", worker="claude-slot", depends_on=["exclusive"]),
        ])
        position = {(kind, task): index for index, (kind, task, _) in enumerate(events)}
        self.assertLess(position["done", "a"], position["worker", "b"])
        self.assertLess(position["done", "a"], position["worker", "exclusive"])
        # The exclusive ticket is selected for the newly freed first slot,
        # before b is eligible on the second slot, and retains the whole pool.
        self.assertLess(position["done", "exclusive"], position["worker", "b"])
        self.assertLess(position["done", "exclusive"], position["worker", "after"])
        self.assertEqual({task["id"]: task["details"]["worker_id"] for task in result["tasks"]},
                         {"a": "codex-slot", "b": "claude-slot", "exclusive": "codex-slot", "after": "claude-slot"})

    def test_completed_candidate_retains_its_worker_slot_until_accepted(self):
        a_reviewed, b_returning = threading.Event(), threading.Event()

        def callback(task_id, review, kwargs):
            if task_id == "b" and not review:
                if not a_reviewed.wait(5):
                    raise AssertionError("a never reached independent review")
                b_returning.set()
            elif task_id == "a" and review:
                a_reviewed.set()
                if not b_returning.wait(5):
                    raise AssertionError("b never completed concurrently with review")
                # Let the coordinator collect b's result while a holds integration.
                time.sleep(0.25)

        _, events = self.injected_run([
            ticket("a", worker="codex-slot"), ticket("b", worker="claude-slot"),
            ticket("queued", worker="claude-slot"),
        ], callback)
        position = {(kind, task): index for index, (kind, task, _) in enumerate(events)}
        self.assertLess(position["worker", "b"], position["review", "a"])
        self.assertLess(position["done", "b"], position["worker", "queued"])

    def test_exclusive_ticket_waits_for_an_already_active_peer(self):
        a_reviewed = threading.Event()

        def callback(task_id, review, kwargs):
            if task_id == "b" and not review:
                if not a_reviewed.wait(5):
                    raise AssertionError("a never reached independent review")
                time.sleep(0.3)
            elif task_id == "a" and review:
                a_reviewed.set()

        _, events = self.injected_run([
            ticket("a", worker="codex-slot"), ticket("b", worker="claude-slot"),
            ticket("exclusive", worker="codex-slot", depends_on=["a"], exclusive=True),
        ], callback)
        position = {(kind, task): index for index, (kind, task, _) in enumerate(events)}
        self.assertLess(position["worker", "b"], position["done", "a"])
        self.assertLess(position["done", "a"], position["worker", "exclusive"])
        self.assertLess(position["done", "b"], position["worker", "exclusive"])


if __name__ == "__main__":
    unittest.main()
