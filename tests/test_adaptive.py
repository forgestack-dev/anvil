"""Public execution regressions for source status, routing, and bounded escalation."""
from contextlib import nullcontext
from dataclasses import replace
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import test_execution
from test_execution import FakeRunner, git
from anvil.execution import run_serial
from anvil.config import WorkerConfig
from anvil.parallel import run_parallel
from anvil.contracts import ContractError
from anvil.processes import InvocationExhausted
from anvil.store import RunStore
from anvil.ticket_status import sync, Publisher
from anvil.routing import Policy
from anvil.workspaces import Repository
from anvil.planning import TaskGraph


def config_options(mode="off", attempts=1):
    return {"profiles": {"standard":{"agent":"codex","model":"test-standard","effort":"medium","rank":1},
                         "strong":{"agent":"codex","model":"test-strong","effort":"high","rank":2}},
            "defaults":{"codex":"standard"}, "review_profile":"strong", "mode":mode,
            "max_attempts":attempts, "max_invocations":20}


def contents(workspace):
    """What a worker turn was handed, ignoring the worktree's own .git file."""
    return sorted(path.name for path in Path(workspace).iterdir() if path.name != '.git')


def worker_result(evidence):
    return {'status': 'completed', 'summary': 'Wrote the work', 'blockers': [],
            'acceptance': [{'criterion': 1, 'evidence': evidence}]}


def approval(evidence):
    return {'verdict': 'approve', 'summary': 'Inspected the candidate', 'findings': [],
            'acceptance': [{'criterion': 1, 'satisfied': True, 'evidence': evidence}]}


def rejection(location, finding):
    return {'verdict': 'request_changes', 'summary': 'One bounded change remains',
            'acceptance': [{'criterion': 1, 'satisfied': False, 'evidence': 'the tree was read'}],
            'findings': [{'criterion': 1, 'location': location, 'finding': finding}]}


class AmendingWorker:
    """A worker that records the worktree it was handed and adds one file per attempt.

    Its reviewer rejects the first `rejections` candidates on one bound,
    located finding, which is the shape docs/ACCEPTANCE.md stage 2 requires.
    """

    def __init__(self, rejections=1):
        self.rejections = rejections
        self.workers, self.reviews, self.seen, self.heads = [], [], [], []

    def run(self, **kwargs):
        workspace = kwargs['repo']
        kwargs['artifact_dir'].mkdir(parents=True)
        if kwargs.get('read_only'):
            self.reviews.append(kwargs['prompt'])
            if len(self.reviews) <= self.rejections:
                return rejection('first.txt:1', 'also record the amendment in second.txt')
            return approval('first.txt and second.txt inspected')
        self.workers.append(kwargs['prompt'])
        self.heads.append(git(workspace, 'rev-parse', 'HEAD'))
        self.seen.append(contents(workspace))
        if len(self.workers) == 1:
            (workspace / 'first.txt').write_text('original work')
            (workspace / 'README.md').unlink()  # the candidate deletes a file too
        else:
            (workspace / 'second.txt').write_text('the amendment')
        return worker_result('the files are written')


class CheckFailingWorker:
    """A worker whose first candidate is red against the configured check."""

    def __init__(self):
        self.workers, self.seen = [], []

    def run(self, **kwargs):
        workspace = kwargs['repo']
        kwargs['artifact_dir'].mkdir(parents=True)
        if kwargs.get('read_only'):
            return approval('the tree was inspected')
        self.workers.append(kwargs['prompt'])
        self.seen.append(contents(workspace))
        (workspace / 'first.txt').write_text('original work')
        (workspace / 'value.txt').write_text('invalid' if len(self.workers) == 1 else '1')
        return worker_result('the files are written')


class RacingWorker:
    """Writes one file per attempt; optionally waits for a peer to integrate."""

    def __init__(self, task_id, wait_for=None):
        self.task_id, self.wait_for = task_id, wait_for
        self.seen = []

    def run(self, **kwargs):
        workspace = kwargs['repo']
        kwargs['artifact_dir'].mkdir(parents=True)
        if self.wait_for is not None and not self.wait_for.wait(60):
            raise AssertionError('the peer never integrated, so no base ever advanced')
        self.seen.append(contents(workspace))
        (workspace / f'{self.task_id}.txt').write_text(self.task_id)
        return worker_result(f'{self.task_id}.txt is written')


class RejectingReviewer:
    """Rejects one ticket's first candidate and approves everything else."""

    def __init__(self, reject_for):
        self.reject_for, self.rejected = reject_for, 0

    def run(self, **kwargs):
        kwargs['artifact_dir'].mkdir(parents=True)
        task = json.JSONDecoder().raw_decode(kwargs['prompt'].split('Ticket:\n', 1)[1])[0]
        if task['id'] == self.reject_for and not self.rejected:
            self.rejected += 1
            return rejection(f"{task['id']}.txt", 'record the change the ticket asks for')
        return approval(f"{task['id']} inspected")


class ExhaustingWorker:
    """A worker whose first `exhaustions` attempts run out of turns after
    writing a partial file; its own reviewer approves whatever finally lands.
    """

    def __init__(self, exhaustions=1, category='turn_exhaustion'):
        self.exhaustions, self.category = exhaustions, category
        self.workers, self.reviews, self.seen, self.heads = [], [], [], []

    def run(self, **kwargs):
        workspace = kwargs['repo']
        kwargs['artifact_dir'].mkdir(parents=True)
        if kwargs.get('read_only'):
            self.reviews.append(kwargs['prompt'])
            return approval('first.txt and second.txt inspected')
        self.workers.append(kwargs['prompt'])
        self.heads.append(git(workspace, 'rev-parse', 'HEAD'))
        self.seen.append(contents(workspace))
        if len(self.workers) <= self.exhaustions:
            (workspace / 'first.txt').write_text('partial work')
            raise InvocationExhausted(
                'stopped after exhausting its configured turns/budget', self.category)
        (workspace / 'second.txt').write_text('the resumption')
        return worker_result('the files are written')


class ExhaustingRacingWorker:
    """Exhausts after writing a partial file and waiting for a peer to integrate."""

    def __init__(self, task_id, wait_for=None):
        self.task_id, self.wait_for = task_id, wait_for
        self.seen = []

    def run(self, **kwargs):
        workspace = kwargs['repo']
        kwargs['artifact_dir'].mkdir(parents=True)
        if kwargs.get('read_only'):
            return approval(f'{self.task_id} inspected')
        self.seen.append(contents(workspace))
        if len(self.seen) == 1:
            (workspace / f'{self.task_id}-partial.txt').write_text('partial')
            if self.wait_for is not None and not self.wait_for.wait(60):
                raise AssertionError('the peer never integrated, so no base ever advanced')
            raise InvocationExhausted('stopped after exhausting its configured turns', 'turn_exhaustion')
        (workspace / f'{self.task_id}.txt').write_text(self.task_id)
        return worker_result(f'{self.task_id}.txt is written')


class AdaptiveTests(unittest.TestCase):
    setUp = test_execution.SerialExecutionTests.setUp

    def test_status_observed_during_worker_review_and_after_acceptance(self):
        owner=self
        statuses=[]
        class Observe(FakeRunner):
            def run(self, **kw):
                task=json.JSONDecoder().raw_decode(kw['prompt'].split('Ticket:\n',1)[1])[0]
                doc=json.loads(owner.tickets.read_text())
                status=next(t['execution']['status'] for t in doc['tasks'] if t['id']==task['id'])
                statuses.append(status)
                return super().run(**kw)
        result=run_serial(replace(self.config,ticket_status=True),runner=Observe())
        self.assertEqual(result['status'],'success',result.get('error'))
        self.assertEqual(statuses,['in_progress','in_review']*2)
        self.assertEqual([t['execution']['status'] for t in json.loads(self.tickets.read_text())['tasks']],['done','done'])
        self.assertEqual(result['ticket_publication']['status'],'synchronized')
        self.assertEqual(sync(Path(result['run_dir']))['status'],'synchronized')

    def test_conflicting_user_edit_preserved_without_losing_acceptance(self):
        owner=self
        class Edit(FakeRunner):
            def run(self,**kw):
                if not self.workers:
                    doc=json.loads(owner.tickets.read_text());doc['tasks'][0]['title']='User correction'
                    owner.tickets.write_text(json.dumps(doc))
                return super().run(**kw)
        result=run_serial(replace(self.config,ticket_status=True),runner=Edit())
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['ticket_publication']['status'],'status_sync_pending')
        self.assertEqual(json.loads(self.tickets.read_text())['tasks'][0]['title'],'User correction')

    def test_in_repo_status_dirtiness_allows_next_run_but_not_input_edits(self):
        tickets=self.repo/'tickets.json';tickets.write_bytes(self.tickets.read_bytes())
        git(self.repo,'add','.');git(self.repo,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','Tickets')
        cfg=replace(self.config,tickets=tickets,ticket_status=True)
        result=run_serial(cfg,runner=FakeRunner());self.assertEqual(result['status'],'success',result.get('error'))
        second=run_serial(cfg,runner=FakeRunner());self.assertEqual(second['status'],'success',second.get('error'))
        self.assertEqual(sync(Path(result['run_dir']))['status'],'status_sync_pending')
        doc=json.loads(tickets.read_text());doc['tasks'][0]['objective']='New requirement';tickets.write_text(json.dumps(doc))
        with self.assertRaises(ContractError):run_serial(cfg,runner=FakeRunner())

    def test_status_failure_never_publishes_done(self):
        for mode in ('blocked','review-rejects','bad-check','interrupt'):
            with self.subTest(mode=mode):
                result=run_serial(replace(self.config,ticket_status=True),runner=FakeRunner(mode))
                state=json.loads(self.tickets.read_text())['tasks'][0]['execution']['status']
                self.assertNotEqual(state,'done')
                self.assertIn(state,('blocked','failed','interrupted'))

    def test_shadow_runs_baseline_while_recording_stronger_recommendation(self):
        doc=json.loads(self.tickets.read_text());doc['tasks'][0]['risk']='high';self.tickets.write_text(json.dumps(doc))
        cfg=replace(self.config,adaptive=config_options('shadow'))
        result=run_serial(cfg,runner=FakeRunner())
        self.assertEqual(result['status'],'success',result.get('error'))
        decision=result['routing']['attempts'][0]['decision']
        self.assertEqual(decision['profile'],'standard');self.assertEqual(decision['recommended_profile'],'strong')
        self.assertFalse(result['routing']['cost_complete'])

    def test_rejected_review_escalates_with_new_attempt_and_same_acceptance_gate(self):
        class Once(FakeRunner):
            def run(self,**kw):
                self.mode='review-rejects' if kw.get('read_only') and not self.reviews else 'success'
                return super().run(**kw)
        result=run_serial(replace(self.config,ticket_status=True,adaptive=config_options(attempts=2)),runner=Once())
        self.assertEqual(result['status'],'success',result.get('error'))
        attempts=[a for a in result['attempts'] if a['task_id']=='t1']
        self.assertEqual(len(attempts),2);self.assertNotEqual(attempts[0]['workspace'],attempts[1]['workspace'])
        self.assertEqual([a['status'] for a in attempts],['failed','done'])
        for task in result['tasks']:
            d=task['details'];self.assertEqual(d['reviewed_sha'],d['integrated_sha']);self.assertEqual(d['verified_sha'],d['integrated_sha'])

    def test_verification_failure_escalates_but_worker_block_does_not(self):
        class Once(FakeRunner):
            def run(self,**kw):
                self.mode='bad-check' if not self.workers else 'success'
                return super().run(**kw)
        cfg=replace(self.config,adaptive=config_options(attempts=2))
        result=run_serial(cfg,runner=Once());self.assertEqual(result['status'],'success',result.get('error'))
        self.assertEqual(len(result['attempts']),3)
        blocked=run_serial(cfg,runner=FakeRunner('blocked'))
        self.assertEqual(blocked['status'],'blocked');self.assertEqual(len(blocked['attempts']),1)

    def test_exhaustion_and_review_mutation_do_not_bypass_stop(self):
        cfg=replace(self.config,adaptive=config_options(attempts=2))
        for mode,expected in [('review-rejects','blocked'),('review-mutates','failed')]:
            result=run_serial(cfg,runner=FakeRunner(mode))
            self.assertEqual(result['status'],expected,result.get('error'))
            self.assertEqual(git(self.repo,'rev-parse',result['branch']),self.base)

    def test_budget_requires_worker_and_review_before_claim(self):
        options=config_options();options['max_invocations']=1
        result=run_serial(replace(self.config,adaptive=options),runner=FakeRunner())
        self.assertEqual(result['status'],'failed');self.assertEqual(result['attempts'],[])
        self.assertIn('budget',result['error'])

    def test_soft_reservations_keep_unknown_cost_reserved(self):
        options=config_options();options['soft_budget_usd']=3
        for profile in options['profiles'].values():profile['reserve_usd']=1
        result=run_serial(replace(self.config,adaptive=options),runner=FakeRunner())
        self.assertEqual(result['status'],'failed');self.assertEqual(result['tasks'][0]['status'],'done')
        self.assertEqual(len(result['attempts']),1)

    def test_parallel_pool_records_a_classified_exhaustion_category_on_stop(self):
        for mode, category in (('turn-exhaustion', 'turn_exhaustion'),
                               ('budget-exhaustion', 'budget_exhaustion')):
            with self.subTest(mode=mode):
                cfg = replace(self.config, workers=(WorkerConfig('one'),))
                result = run_parallel(cfg, runners={'one': FakeRunner(mode)}, review_runner=FakeRunner())
                self.assertEqual(result['status'], 'failed')
                failed_task = next(task for task in result['tasks'] if task['status'] == 'failed')
                self.assertEqual(failed_task['details']['failure_category'], category)
                # Nothing was written before exhausting, so no revision names it.
                self.assertNotIn('exhausted_sha', failed_task['details'])

    def test_parallel_pool_retains_an_exhausted_workers_partial_tree(self):
        """The worker thread that observes InvocationExhausted defers scope
        cancellation so the coordinator can still run Git; the run still
        stops the same way, with the revision as the only new thing."""
        cfg = replace(self.config, workers=(WorkerConfig('one'),))
        result = run_parallel(cfg, runners={'one': FakeRunner('turn-exhaustion-partial')},
                              review_runner=FakeRunner())
        self.assertEqual(result['status'], 'failed')
        failed_task = next(task for task in result['tasks'] if task['status'] == 'failed')
        self.assertEqual(failed_task['details']['failure_category'], 'turn_exhaustion')
        revision = failed_task['details']['exhausted_sha']
        self.assertEqual(git(self.repo, 'show', '-s', '--format=%P', revision), self.base)
        self.assertEqual(git(self.repo, 'show', f'{revision}:partial.txt'), 'interrupted mid-turn')
        attempt = result['attempts'][0]['id']
        ref = f"refs/anvil/exhausted/{result['run_id']}/{attempt}"
        self.assertEqual(git(self.repo, 'rev-parse', ref), revision)
        # Never a candidate: the task's own status history never held one.
        self.assertNotIn('candidate_sha', failed_task['details'])
        self.assertEqual(git(self.repo, 'rev-parse', result['branch']), self.base)

    def test_parallel_status_and_affinity(self):
        cfg=replace(self.config,ticket_status=True,adaptive=config_options(),workers=(WorkerConfig('one'),WorkerConfig('two')),max_processes=2)
        result=run_parallel(cfg,runners={'one':FakeRunner(),'two':FakeRunner()},review_runner=FakeRunner())
        self.assertEqual(result['status'],'success',result.get('error'))
        self.assertEqual(result['ticket_publication']['status'],'synchronized')

    def test_atomic_replace_before_cursor_acknowledgement_replays(self):
        original=Publisher.save
        calls=[]
        def fail_after_replace(self,state):
            if state['pending'] is None and state['cursor'] and not calls:
                calls.append(1);raise OSError('crash before acknowledgement')
            original(self,state)
        with patch.object(Publisher,'save',fail_after_replace):
            result=run_serial(replace(self.config,ticket_status=True),runner=FakeRunner())
        self.assertEqual(result['status'],'success')
        self.assertEqual(sync(Path(result['run_dir']))['status'],'synchronized')

    def test_real_mixed_processes_receive_distinct_profiles_and_publish_status(self):
        import test_parallel_execution as fixtures
        from anvil.config import WorkerConfig
        markers=self.root/'markers';markers.mkdir()
        codex=fixtures.ParallelExecutionTests.fake_binary(self,'codex',markers)
        claude=fixtures.ParallelExecutionTests.fake_binary(self,'claude-code',markers)
        for binary in (codex,claude):
            text=binary.read_text().replace('prompt = sys.stdin.read()', '''if '--help' in args:
    print('--model --config --effort --max-budget-usd');sys.exit(0)
if '--version' in args:
    print('fixture-v1');sys.exit(0)
prompt = sys.stdin.read()''')
            binary.write_text(text)
        self.tickets.write_text(json.dumps({'version':1,'tasks':[
            fixtures.ticket('alpha',worker='codex-slot'), fixtures.ticket('beta',worker='claude-slot')]}))
        options=config_options()
        options['profiles']['claude']={'agent':'claude-code','model':'test-claude','effort':'low','rank':1}
        options['defaults']['claude-code']='claude';options['review_profile']='claude'
        cfg=replace(self.config,agent='claude-code',agent_binary=str(claude),ticket_status=True,adaptive=options,
                    workers=(WorkerConfig('codex-slot','codex',str(codex)),WorkerConfig('claude-slot','claude-code',str(claude))),max_processes=3)
        result=run_parallel(cfg)
        self.assertEqual(result['status'],'success',result.get('error'))
        for ticket,model in [('alpha','test-standard'),('beta','test-claude')]:
            trace=json.loads((markers/f'{ticket}-worker.json').read_text())
            self.assertEqual(trace['argv'][trace['argv'].index('--model')+1],model)
        a=json.loads((markers/'alpha-worker.json').read_text());b=json.loads((markers/'beta-worker.json').read_text())
        self.assertLess(max(a['started'],b['started']),min(a['finished'],b['finished']))
        self.assertEqual([t['execution']['status'] for t in json.loads(self.tickets.read_text())['tasks']],['done','done'])

    def test_stale_attempt_cannot_publish_or_integrate_after_retry(self):
        cfg=replace(self.config,adaptive=config_options(attempts=2))
        class Once(FakeRunner):
            def run(self,**kw):
                self.mode='review-rejects' if kw.get('read_only') and not self.reviews else 'success'
                return super().run(**kw)
        result=run_serial(cfg,runner=Once())
        old=next(a for a in result['attempts'] if a['status']=='failed')
        from anvil.store import StoreError
        with RunStore(Path(result['run_dir'])/'state.sqlite') as store:
            with self.assertRaises(StoreError):
                store.transition(old['task_id'],'done',attempt_id=old['id'])
        from anvil.learning import import_run
        from anvil.workspaces import Repository
        self.assertEqual(import_run(Repository(self.repo),result)['imported_or_existing'],2)

    def test_retry_keeps_ticket_in_progress_and_preserves_failure_cause(self):
        owner=self
        seen=[]
        states={}
        original=Publisher.flush
        def spy_flush(publisher):
            original(publisher)
            for task in json.loads(owner.tickets.read_text())['tasks']:
                status=task['execution']['status']
                # Only watch a ticket once it has started: a retry must never
                # send it back to an unowned todo.
                if states.get(task['id']) not in (None,'todo'):
                    seen.append(status)
                states[task['id']]=status
        class Once(FakeRunner):
            def run(self,**kw):
                self.mode='review-rejects' if kw.get('read_only') and not self.reviews else 'success'
                return super().run(**kw)
        cfg=replace(self.config,ticket_status=True,adaptive=config_options(attempts=2))
        with patch.object(Publisher,'flush',spy_flush):
            result=run_serial(cfg,runner=Once())
        self.assertEqual(result['status'],'success',result.get('error'))
        # The retried ticket moves straight from review back to running; it is
        # never published as an unowned todo.
        self.assertNotIn('todo',seen)
        failed=next(a for a in result['routing']['attempts'] if a['status']=='failed')
        self.assertEqual(failed['failure_category'],'review_rejection')
        raw=next(a for a in result['attempts'] if a['status']=='failed')
        self.assertEqual(raw['details']['failure_category'],'review_rejection')
        self.assertIn('retry_reason',raw['details'])

    def test_verification_failure_retry_preserves_structured_cause(self):
        class Once(FakeRunner):
            def run(self,**kw):
                self.mode='bad-check' if not self.workers else 'success'
                return super().run(**kw)
        result=run_serial(replace(self.config,adaptive=config_options(attempts=2)),runner=Once())
        self.assertEqual(result['status'],'success',result.get('error'))
        failed=next(a for a in result['routing']['attempts'] if a['status']=='failed')
        self.assertEqual(failed['failure_category'],'verification_failure')

    def test_exhaustion_categories_survive_to_evidence_and_are_excluded_from_the_gate(self):
        """turn_exhaustion/budget_exhaustion mark an invocation that hit a
        ceiling before producing any accepted candidate. The category must
        survive from the ledger event through the telemetry evidence a
        routing policy gate reads, and that gate must then treat the sample
        as insufficient evidence rather than as a scored rejection: it must
        not be promoted or demoted as if a candidate had been judged."""
        from anvil.adaptive_runtime import report
        from anvil.learning import import_run, history
        from anvil.contracts import Task
        for category in ("turn_exhaustion", "budget_exhaustion"):
            with self.subTest(category=category):
                state_dir = self.root / f"exhaustion-{category}"
                with RunStore(state_dir / "state.sqlite") as store:
                    task = Task(category, "Implement it", "Deliver it", (), ("It works",))
                    store.initialize(run_id=f"run-{category}", repo=str(self.repo),
                                     branch="anvil/exhaustion", base_sha=self.base,
                                     tasks=(task,),
                                     config={"verification": [["true"]],
                                             "adaptive": config_options()})
                    store.set_run("running")
                    attempt = store.start_attempt(category, self.base, "/workspace/attempt")
                    decision = {"profile": "standard", "recommended_profile": "standard",
                               "reason": "deterministic complexity/risk rules",
                               "policy_version": "v1", "learned_policy": None,
                               "assessment": {"cohort": "rank-1", "input_digest": "digest"}}
                    store.record_message(category, attempt_id=attempt,
                                         kind="routing_decision", body=decision)
                    store.transition(category, "candidate", attempt_id=attempt,
                                     details={"candidate_sha": "candidate"})
                    store.record_rejection(category, attempt_id=attempt,
                                           reason="ceiling reached before any candidate was judged",
                                           failure_category=category)
                    store.transition(category, "blocked", attempt_id=attempt,
                                     details={"error": "ceiling reached"})
                    store.set_run("blocked")
                    result = store.snapshot()
                result["run_dir"] = state_dir
                report(result)
                record = result["routing"]["attempts"][0]
                self.assertEqual(record["failure_category"], category)
                self.assertEqual(record["evaluation"], "insufficient_evidence")
                imported = import_run(Repository(self.repo), result)
                self.assertEqual(imported["imported_or_existing"], 1)
                with history(Repository(self.repo)) as db:
                    row = db.execute("SELECT data FROM samples WHERE run_id=?",
                                     (result["run_id"],)).fetchone()
                self.assertFalse(json.loads(row[0])["eligible"])

    def test_benchmark_keeps_configured_retry_limit_for_evidence_catalog(self):
        import anvil.benchmark as benchmark_module
        from anvil.routing import learning_catalog
        tasks=[{'id':'b1','title':'Low-risk check','objective':'Check','depends_on':[],
                'acceptance_criteria':['Checked'],'risk':'low'}]
        self.tickets.write_text(json.dumps({'version':1,'tasks':tasks}))
        captured=[]
        def fake_run(cfg,**kw):
            captured.append(cfg)
            raise RuntimeError('stop after capture')
        cfg=replace(self.config,adaptive=config_options(attempts=2))
        with patch.object(benchmark_module,'run',fake_run):
            with self.assertRaises(RuntimeError):
                benchmark_module.compare(cfg,['standard','strong'])
        selected=captured[0].adaptive
        self.assertEqual(selected['max_attempts'],2)
        # Benchmark evidence fingerprints into the production catalog, so it
        # stays eligible under the same retry limit.
        self.assertEqual(learning_catalog(captured[0]),learning_catalog(cfg))

    def test_benchmark_budget_sums_reachable_escalation_paths(self):
        import anvil.benchmark as benchmark_module
        from anvil.contracts import ContractError
        tasks=[{'id':'b1','title':'Low-risk check','objective':'Check','depends_on':[],
                'acceptance_criteria':['Checked'],'risk':'low'}]
        self.tickets.write_text(json.dumps({'version':1,'tasks':tasks}))
        def options_with_budget(limit):
            options=config_options(attempts=2)
            options['profiles']['standard']['reserve_usd']=1.0
            options['profiles']['strong']['reserve_usd']=10.0
            options['soft_budget_usd']=limit
            return replace(self.config,adaptive=options)
        def fake_run(cfg,**kw):
            raise RuntimeError('stop after budget check')
        # Reachable maximum is 31 for standard (1+10 plus two 10 reviews)
        # plus 20 for strong (10 plus one 10 review): 51. The old
        # worst-case-per-attempt math reserved 80 and rejected this budget.
        with patch.object(benchmark_module,'run',fake_run):
            with self.assertRaisesRegex(RuntimeError,'stop after budget check'):
                benchmark_module.compare(options_with_budget(79.0),['standard','strong'])
        # A budget below the reachable maximum is still rejected.
        with self.assertRaisesRegex(ContractError,'aggregate soft budget'):
            benchmark_module.compare(options_with_budget(50.0),['standard','strong'])

    def test_benchmark_invocation_budget_counts_reachable_attempts(self):
        import anvil.benchmark as benchmark_module
        from anvil.contracts import ContractError
        tasks=[{'id':'b1','title':'Low-risk check','objective':'Check','depends_on':[],
                'acceptance_criteria':['Checked'],'risk':'low'}]
        self.tickets.write_text(json.dumps({'version':1,'tasks':tasks}))
        def options_with_invocations(limit):
            options=config_options(attempts=2)
            options['max_invocations']=limit
            return replace(self.config,adaptive=options)
        def fake_run(cfg,**kw):
            raise RuntimeError('stop after invocation budget check')
        # Reachable maximum is 4 calls for standard (two attempts, worker plus
        # reviewer each) plus 2 for strong, which has no escalation headroom:
        # 6. The old per-profile math counted 8.
        with patch.object(benchmark_module,'run',fake_run):
            with self.assertRaisesRegex(RuntimeError,'stop after invocation budget check'):
                benchmark_module.compare(options_with_invocations(6),['standard','strong'])
        with self.assertRaisesRegex(ContractError,'aggregate invocation budget'):
            benchmark_module.compare(options_with_invocations(5),['standard','strong'])

    def test_benchmark_incomplete_telemetry_charges_escalation_path(self):
        import anvil.benchmark as benchmark_module
        tasks=[{'id':'b1','title':'Low-risk check','objective':'Check','depends_on':[],
                'acceptance_criteria':['Checked'],'risk':'low'}]
        self.tickets.write_text(json.dumps({'version':1,'tasks':tasks}))
        options=config_options(attempts=2)
        options['profiles']['standard']['reserve_usd']=1.0
        options['profiles']['strong']['reserve_usd']=10.0
        def fake_run(cfg,**kw):
            return {'run_dir':'x','status':'success',
                    'routing':{'known_cost_usd':1.0}}
        cfg=replace(self.config,adaptive=options)
        with patch.object(benchmark_module,'run',fake_run):
            result=benchmark_module.compare(cfg,['standard','strong'])
        # Without complete telemetry each profile charges its bounded
        # escalation path: standard reserves 1+10 plus two reviews (10 each),
        # strong reserves 10 plus one review; the review profile is strong.
        self.assertEqual(result['known_or_reserved_cost_usd'],51.0)

    def test_damaged_history_does_not_erase_accepted_run_report(self):
        history=self.repo/'.git'/'anvil-routing'
        history.mkdir();(history/'history.sqlite').write_bytes(b'not a database')
        result=run_serial(replace(self.config,adaptive=config_options()),runner=FakeRunner())
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['learning']['status'],'history_update_pending')
        saved=json.loads((Path(result['run_dir'])/'report.json').read_text())
        self.assertEqual(saved['status'],'success')

    def test_corrupt_telemetry_does_not_break_saved_routing_report(self):
        result=run_serial(replace(self.config,adaptive=config_options()),runner=FakeRunner())
        artifact=Path(result['run_dir'])/'artifacts'/result['attempts'][0]['id']/'worker'/'invocation.json'
        artifact.write_text('{"role":"worker","cost_usd":-100,"duration_seconds":1}')
        from anvil.adaptive_runtime import report
        report(result)
        self.assertEqual(result['status'],'success')
        self.assertFalse(result['routing']['cost_complete'])
        self.assertGreaterEqual(result['routing']['known_cost_usd'],0)

    def test_ticket_profile_without_adaptive_rejects_before_dispatch(self):
        doc = json.loads(self.tickets.read_text())
        doc['tasks'][0]['profile'] = 'nonexistent-profile'
        self.tickets.write_text(json.dumps(doc))
        runner = FakeRunner()
        for parallel in (False, True):
            with self.subTest(parallel=parallel), self.assertRaisesRegex(ContractError, 'profiles require'):
                if parallel:
                    cfg = replace(self.config, workers=(WorkerConfig('one'),), max_processes=1)
                    run_parallel(cfg, runners={'one': runner}, review_runner=runner)
                else:
                    run_serial(self.config, runner=runner)
        self.assertEqual(runner.workers, [])
        self.assertEqual(runner.reviews, [])

    def test_accounting_survives_a_state_dir_reached_through_a_symlink(self):
        """read_regular refuses symlinked paths; normalized config keeps it aimed.

        The guard exists so a worker cannot redirect the ticket file it is
        judged against, and it inspects every ancestor. Ordinary macOS paths
        have symlinked ancestors (/tmp and /var both link into /private), so
        telemetry would silently degrade to unknown cost if a run ever read its
        artifacts through the path the operator typed. It does not: run_serial
        and run_parallel both normalize the configuration on entry, and
        RunConfig.from_document resolves state_dir. This test names that
        dependency, because the normalization reads like re-validation.
        """
        import json as _json
        from anvil.ticket_status import read_regular
        real = Path(self.root).resolve() / "state-real"
        real.mkdir()
        link = Path(self.root).resolve() / "state-link"
        link.symlink_to(real, target_is_directory=True)

        class Metered(FakeRunner):
            """Emits the codex-shaped usage stream MeasuredRunner reads."""
            def run(self, **kwargs):
                outcome = super().run(**kwargs)
                (kwargs["artifact_dir"] / "events.jsonl").write_text(_json.dumps(
                    {"type": "turn.completed",
                     "usage": {"input_tokens": 100000, "cached_input_tokens": 0,
                               "output_tokens": 10000}}) + "\n")
                return outcome

        price = {"version": "test-1", "input": 3.0, "cached_input": 0.3,
                 "cache_write": 3.75, "output": 15.0}
        options = config_options()
        for profile in options["profiles"].values():
            profile["price"] = price
        config = replace(self.config, state_dir=link, adaptive=options)
        result = run_serial(config, runner=Metered())

        self.assertEqual(result["status"], "success", result.get("error"))
        routing = result["routing"]
        self.assertTrue(routing["cost_complete"])
        self.assertGreater(routing["known_cost_usd"], 0)
        for attempt in routing["attempts"]:
            for invocation in attempt["invocations"]:
                self.assertIsNone(invocation.get("usage_error"))
                self.assertEqual(invocation["cost_kind"], "configured_price_estimate")
        # The run recorded its own location resolved, not as it was configured.
        run_dir = Path(result["run_dir"])
        self.assertFalse(any(p.is_symlink() for p in (run_dir, *run_dir.parents)))
        self.assertTrue(run_dir.is_relative_to(real))
        # And the guard still refuses the same artifacts by the configured path.
        through_link = link / run_dir.name / "artifacts"
        invocation = next(through_link.glob("*/worker/invocation.json"))
        with self.assertRaisesRegex(ContractError, "must not contain symlinks"):
            read_regular(invocation)

    def test_a_conceded_rejection_never_escalates(self):
        """The stage's whole point: no retry can satisfy a requirement the
        ticket does not state, so the run stops for its author instead of
        spending a stronger profile on the same criteria."""
        from anvil.learning import history
        class Conceded(FakeRunner):
            def run(self, **kw):
                outcome = super().run(**kw)
                if kw.get('read_only'):
                    outcome.update(verdict='request_changes',
                                   findings=[{'criterion': 1, 'location': 'README.md',
                                              'finding': 'the field is undocumented'}])
                return outcome
        runner = Conceded()
        result = run_serial(replace(self.config, adaptive=config_options(attempts=2)), runner=runner)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(sum(e['kind'] == 'retry' for e in result['events']), 0)
        self.assertEqual(len(runner.workers), 1)
        self.assertEqual(len(result['attempts']), 1)
        details = result['attempts'][0]['details']
        self.assertEqual(details['failure_category'], 'unlisted_requirement')
        self.assertIn('the ticket does not carry', result['error'])
        # The worker met every stated criterion, so the sample teaches routing
        # nothing and must not be charged to its profile.
        record = result['routing']['attempts'][0]
        self.assertEqual(record['failure_category'], 'unlisted_requirement')
        self.assertEqual(record['evaluation'], 'insufficient_evidence')
        with history(Repository(self.repo)) as db:
            row = db.execute('SELECT data FROM samples WHERE run_id=?', (result['run_id'],)).fetchone()
        if row is not None:
            self.assertFalse(json.loads(row[0])['eligible'])

    def test_an_unsatisfied_criterion_still_escalates(self):
        """The contrast that proves the stop discriminates."""
        runner = FakeRunner('review-rejects')
        result = run_serial(replace(self.config, adaptive=config_options(attempts=2)), runner=runner)
        self.assertEqual(sum(e['kind'] == 'retry' for e in result['events']), 1)
        self.assertEqual(len(runner.workers), 2)
        self.assertNotEqual(result['attempts'][0]['details'].get('failure_category'),
                            'unlisted_requirement')

    def test_a_failing_check_never_reaches_review(self):
        """Checks-first: a red candidate costs no review invocation."""
        runner = FakeRunner("bad-check")
        result = run_serial(replace(self.config, adaptive=config_options()), runner=runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runner.reviews, [])
        details = result["tasks"][0]["details"]
        self.assertIn("candidate_sha", details)
        self.assertNotIn("verified_sha", details)
        self.assertNotIn("review", details)
        self.assertNotEqual(details["verification"][0]["returncode"], 0)
        # An unstarted review is a real zero, never unknown accounting.
        review = next(i for i in result["routing"]["attempts"][0]["invocations"]
                      if i["role"] == "review")
        self.assertEqual((review["cost_usd"], review["cost_kind"]), (0, "not_started"))

    def test_a_check_rejection_retries_without_ever_reviewing(self):
        runner = FakeRunner("bad-check")
        result = run_serial(replace(self.config, adaptive=config_options(attempts=2)), runner=runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runner.reviews, [])
        self.assertEqual(len(runner.workers), 2)
        categories = [a["details"].get("failure_category") for a in result["attempts"]]
        self.assertEqual(categories[0], "verification_failure")

    def test_an_unstarted_review_does_not_consume_its_reservation(self):
        """Stage 1 accounting: only a dispatched review is charged.

        Under checks-first a rejected candidate often never reaches review, so
        charging the reserved reviewer cost would spend a run's soft budget on
        invocations that never happened.
        """
        from types import SimpleNamespace
        from anvil.adaptive_runtime import Session
        from anvil.ticket_status import atomic, encoded
        for reviewed, expected in ((False, 0.25), (True, 4.0)):
            with self.subTest(reviewed=reviewed):
                # resolve(): read_regular refuses symlinked paths, and on macOS
                # a temp dir sits under /var, itself a link to /private/var.
                artifacts = Path(self.root).resolve() / f"settle-{reviewed}"
                worker = artifacts / "worker"
                worker.mkdir(parents=True)
                atomic(worker / "invocation.json",
                       encoded({"role": "worker", "cost_usd": 0.25, "duration_seconds": 1.0,
                                "cost_kind": "provider_reported_estimate"}))
                session = Session.__new__(Session)
                session.committed_cost = 0.0
                session.reservations = {"attempt": 4.0}
                item = SimpleNamespace(attempt_id="attempt", artifacts=artifacts)
                session.settle(item, reviewed=reviewed)
                self.assertAlmostEqual(session.committed_cost, expected)
                self.assertEqual(session.reservations, {})

    def test_cancellation_and_infrastructure_error_are_not_quality_failures(self):
        """An interrupted or broken check is never scored against the worker.

        Checks now precede review, so the error lands before any review exists;
        the attempt must still evaluate as insufficient evidence rather than as
        a rejection. See docs/ACCEPTANCE.md.
        """
        from anvil.execution import verify
        from anvil.processes import ProcessError
        from anvil.learning import history
        for error in (KeyboardInterrupt(), ProcessError('verification process unavailable')):
            calls = []
            def stop_at_integration_checks(*args):
                calls.append(1)
                if len(calls) > 1:
                    raise error
                return verify(*args)
            with self.subTest(error=type(error).__name__), patch('anvil.parallel.verify', stop_at_integration_checks):
                result = run_serial(replace(self.config, adaptive=config_options()), runner=FakeRunner())
            attempt = result['routing']['attempts'][0]
            self.assertEqual(attempt['evaluation'], 'insufficient_evidence')
            # The checks broke before review, so no verdict was ever produced.
            self.assertNotIn('review', result['attempts'][0]['details'])
            with history(Repository(self.repo)) as db:
                row = db.execute('SELECT data FROM samples WHERE run_id=?', (result['run_id'],)).fetchone()
            if isinstance(error, KeyboardInterrupt):
                self.assertIsNone(row)  # resumable runs cannot be imported as immutable final evidence
            else:
                self.assertFalse(json.loads(row[0])['eligible'])

    def test_infrastructure_error_after_approval_is_not_a_quality_failure(self):
        """The post-approval half of the case above, which checks-first moved."""
        from anvil.processes import ProcessError
        from anvil.learning import history
        from anvil.workspaces import Repository as Repo
        for error in (KeyboardInterrupt(), ProcessError('git unavailable')):
            with self.subTest(error=type(error).__name__), \
                 patch.object(Repo, 'advance_branch', side_effect=error):
                result = run_serial(replace(self.config, adaptive=config_options()), runner=FakeRunner())
            attempt = result['routing']['attempts'][0]
            self.assertEqual(attempt['evaluation'], 'insufficient_evidence')
            self.assertEqual(result['attempts'][0]['details']['review']['verdict'], 'approve')
            with history(Repository(self.repo)) as db:
                row = db.execute('SELECT data FROM samples WHERE run_id=?', (result['run_id'],)).fetchone()
            if isinstance(error, KeyboardInterrupt):
                self.assertIsNone(row)
            else:
                self.assertFalse(json.loads(row[0])['eligible'])

    def test_real_check_rejection_remains_eligible_with_bound_execution_conditions(self):
        from anvil.learning import history
        from anvil.routing import learning_catalog
        result = run_serial(replace(self.config, adaptive=config_options()), runner=FakeRunner('bad-check'))
        self.assertEqual(result['routing']['attempts'][0]['evaluation'], 'rejected')
        with history(Repository(self.repo)) as db:
            sample = json.loads(db.execute('SELECT data FROM samples WHERE run_id=?', (result['run_id'],)).fetchone()[0])
        self.assertTrue(sample['eligible'])
        self.assertFalse(sample['accepted'])
        self.assertEqual(sample['catalog'], learning_catalog(result['config']))

    # -- Amend retries (docs/AMEND_RETRIES.md) ------------------------------

    def amend_ticket(self):
        """One ticket with one criterion; the fixture's check ignores its files."""
        self.tickets.write_text(json.dumps({'version': 1, 'tasks': [
            {'id': 't1', 'title': 'Write the work', 'objective': 'Write the work',
             'depends_on': [], 'acceptance_criteria': ['The work is in the tree']}]}))

    def amend_run(self, runner=None, rejections=1):
        self.amend_ticket()
        runner = AmendingWorker(rejections) if runner is None else runner
        cfg = replace(self.config, adaptive=config_options(attempts=2))
        return run_serial(cfg, runner=runner), runner

    def decisions_for(self, result, task_id=None):
        return [event['details']['body'] for event in result['events']
                if event['details'].get('message_kind') == 'routing_decision'
                and task_id in (None, event['task_id'])]

    def test_an_amend_retry_starts_from_the_rejected_candidates_tree(self):
        """Criterion 1: the replacement attempt opens the candidate, not an empty
        worktree, while HEAD stays at the accepted base and the deletion holds."""
        result, runner = self.amend_run()
        self.assertEqual(result['status'], 'success', result.get('error'))
        self.assertEqual(len(runner.workers), 2)
        self.assertEqual(runner.seen, [['README.md'], ['first.txt']])
        self.assertEqual(runner.heads, [self.base, self.base])
        integrated = result['tasks'][0]['details']['integrated_sha']
        self.assertEqual(sorted(git(self.repo, 'ls-tree', '-r', '--name-only', integrated).split()),
                         ['first.txt', 'second.txt'])
        self.assertEqual(git(self.repo, 'show', f'{integrated}:first.txt'), 'original work')
        self.assertEqual(git(self.repo, 'show', f'{integrated}:second.txt'), 'the amendment')

    def test_the_amended_candidate_is_one_commit_on_the_base(self):
        """Criterion 1: commit_candidate, prepare_integration and the phase
        machine are unchanged, so the accepted commit is still a squash."""
        result, _ = self.amend_run()
        details = result['tasks'][0]['details']
        integrated = details['integrated_sha']
        self.assertEqual(git(self.repo, 'rev-list', '--parents', '-n', '1', integrated).split(),
                         [integrated, self.base])
        self.assertEqual(git(self.repo, 'rev-parse', result['branch']), integrated)
        self.assertEqual(details['verified_sha'], integrated)
        self.assertEqual(details['reviewed_sha'], integrated)

    def test_a_verification_failure_amends_from_the_candidate_the_checks_rejected(self):
        """Criterion 1: the better of the two cases, bounded by check output."""
        result, runner = self.amend_run(runner=CheckFailingWorker())
        self.assertEqual(result['status'], 'success', result.get('error'))
        self.assertEqual(runner.seen, [['README.md'], ['README.md', 'first.txt', 'value.txt']])
        self.assertIn('required verification failed', runner.workers[1])
        self.assertIn('output of the configured checks', runner.workers[1])
        self.assertIn('amended_candidate', self.decisions_for(result)[1])

    def test_an_amend_is_refused_once_a_peer_has_advanced_the_base(self):
        """Criterion 2, and the failure the guard exists to prevent.

        The second case replaces _amend_source with one that ignores the base:
        that is a version which always amends, and it runs here. Under it the
        peer's integrated file is deleted from the accepted tree by a commit
        whose single parent is the new base and whose diff no review would read
        as a mistake, which is what the case asserts. The two cases assert
        opposite outcomes for the same scenario, so an implementation that
        always amends fails the first case: it would lose peer.txt there too.
        """
        always = lambda item, base, failure_category: item.candidate
        for amends_always in (False, True):
            with self.subTest(amends_always=amends_always):
                self.tickets.write_text(json.dumps({'version': 1, 'tasks': [
                    {'id': 'peer', 'title': 'Peer work', 'objective': 'Integrate first',
                     'depends_on': [], 'acceptance_criteria': ['peer.txt exists'],
                     'worker': 'quick'},
                    {'id': 'alpha', 'title': 'Slower work', 'objective': 'Finish after the peer',
                     'depends_on': [], 'acceptance_criteria': ['alpha.txt exists'],
                     'worker': 'slow'}]}))
                integrated = threading.Event()
                workers = {'quick': RacingWorker('peer'),
                           'slow': RacingWorker('alpha', wait_for=integrated)}
                cfg = replace(self.config, adaptive=config_options(attempts=2),
                              workers=(WorkerConfig('quick'), WorkerConfig('slow')),
                              max_processes=3)
                with (patch('anvil.parallel._amend_source', always) if amends_always
                      else nullcontext()):
                    result = run_parallel(
                        cfg, runners=workers, review_runner=RejectingReviewer('alpha'),
                        progress=lambda m: integrated.set() if m == 'peer: done' else None)
                self.assertEqual(result['status'], 'success', result.get('error'))
                tree = git(self.repo, 'ls-tree', '-r', '--name-only', result['branch']).split()
                self.assertIn('alpha.txt', tree)
                if amends_always:
                    self.assertNotIn('peer.txt', tree)
                    continue
                # The peer's integrated work survives the replacement attempt.
                self.assertIn('peer.txt', tree)
                self.assertEqual(git(self.repo, 'show', f"{result['branch']}:peer.txt"), 'peer')
                # The fallback is today's fresh worktree on the advanced base,
                # and a fresh-worktree retry escalates exactly as it did before.
                self.assertIn('peer.txt', workers['slow'].seen[1])
                decisions = self.decisions_for(result, 'alpha')
                self.assertEqual([d['profile'] for d in decisions], ['standard', 'strong'])
                self.assertNotIn('amended_candidate', decisions[1])

    def test_an_amend_keeps_its_profile_and_spends_no_escalation(self):
        """Criterion 3: the profile produced a candidate, so it is kept, and the
        allowance stays for the attempt after it. The cap is unchanged."""
        result, _ = self.amend_run()
        self.assertEqual(result['status'], 'success', result.get('error'))
        decisions = self.decisions_for(result)
        self.assertEqual([d['profile'] for d in decisions], ['standard', 'standard'])
        self.assertIn('amendment of candidate', decisions[1]['reason'])
        # Had the amend escalated, the attempt after it would have had nothing
        # left: strong is the top rank. Keeping the profile leaves the rank.
        cfg = replace(self.config, adaptive=config_options(attempts=2),
                      workers=(WorkerConfig('serial'),))
        policy = Policy(cfg, TaskGraph.load(self.tickets), Repository(self.repo))
        self.assertEqual(policy.escalation(decisions[1]['profile']), 'strong')
        self.assertIsNone(policy.escalation('strong'))
        self.assertEqual(sum(e['kind'] == 'retry' for e in result['events']), 1)
        self.assertEqual(len(result['attempts']), 2)

    def test_the_amended_attempt_names_a_candidate_that_resolves(self):
        """Criterion 5: provenance that resolves through the retained ref."""
        result, _ = self.amend_run()
        rejected = next(a for a in result['attempts'] if a['status'] == 'failed')
        amended = next(a for a in result['attempts'] if a['status'] == 'done')
        decision = next(event['details']['body'] for event in result['events']
                        if event['details'].get('message_kind') == 'routing_decision'
                        and event['attempt_id'] == amended['id'])
        self.assertEqual(decision['amended_candidate'], rejected['details']['candidate_sha'])
        reference = f"refs/anvil/candidate/{result['run_id']}/{rejected['id']}"
        self.assertEqual(git(self.repo, 'rev-parse', reference), decision['amended_candidate'])
        self.assertEqual(git(self.repo, 'cat-file', '-t', decision['amended_candidate']), 'commit')

    def test_a_rejected_amend_is_an_ordinary_review_rejection(self):
        """Criterion 5: no new failure category enters the evidence."""
        result, runner = self.amend_run(rejections=2)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(len(runner.workers), 2)
        self.assertEqual([a['failure_category'] for a in result['routing']['attempts']],
                         ['review_rejection', 'review_rejection'])
        self.assertEqual(git(self.repo, 'rev-parse', result['branch']), self.base)

    def test_a_check_failure_reaches_the_prompt_as_output_not_as_a_log_path(self):
        """A worker's file tools are rooted at its worktree, so a prompt that
        names a recorded log file tells it to read something it will be refused.

        Claude Code reports the refusal as a denied permission and the adapter
        discards the whole result, so an attempt that did the work is lost. Run
        3586bd13 lost one that way. The supervisor holds the output already.
        """
        _, runner = self.amend_run(runner=CheckFailingWorker())
        amend = runner.workers[1]
        self.assertIn('required verification failed', amend)
        # The failing command and its own output, not a path to them.
        self.assertIn('exit status: 1', amend)
        self.assertIn('AssertionError', amend)
        self.assertNotIn('.stderr.log', amend)
        self.assertNotIn('.stdout.log', amend)
        # Nothing outside the worktree is named at all.
        self.assertNotIn(str(self.root / 'state'), amend)

    def test_the_amend_prompt_states_the_tree_the_findings_and_the_criteria(self):
        """Criterion 4: the three things the fresh prompt does not say."""
        _, runner = self.amend_run()
        fresh, amend = runner.workers
        self.assertIn('already holds the candidate', amend)
        self.assertIn('independent review', amend)
        self.assertIn('acceptance criteria are unchanged and all of them still apply', amend)
        # Bound and located, as the review produced them, not a paragraph.
        self.assertIn('criterion 1 (first.txt:1): also record the amendment in second.txt', amend)
        self.assertNotIn('already holds the candidate', fresh)

    # -- Resumed exhausted attempts (docs/RUN_ECONOMICS.md slice 4) ---------

    def resume_ticket(self):
        """One ticket with one criterion; mirrors amend_ticket for the exhaustion path."""
        self.tickets.write_text(json.dumps({'version': 1, 'tasks': [
            {'id': 't1', 'title': 'Write the work', 'objective': 'Write the work',
             'depends_on': [], 'acceptance_criteria': ['The work is in the tree']}]}))

    def resume_run(self, runner=None, exhaustions=1, category='turn_exhaustion'):
        self.resume_ticket()
        runner = ExhaustingWorker(exhaustions, category) if runner is None else runner
        cfg = replace(self.config, adaptive=config_options(attempts=2), workers=(WorkerConfig('one'),))
        return run_parallel(cfg, runners={'one': runner}, review_runner=runner), runner

    def test_a_resumed_attempt_sees_the_exhausted_trees_files_and_is_accepted(self):
        """Criterion 1: exhaustion gets the same retry a rejection gets.
        Criterion 3/4: the restored tree is exactly what commit_exhausted
        recorded for this attempt, and the second worker sees it uncommitted
        at the accepted base."""
        for category in ('turn_exhaustion', 'budget_exhaustion'):
            with self.subTest(category=category):
                result, runner = self.resume_run(category=category)
                self.assertEqual(result['status'], 'success', result.get('error'))
                self.assertEqual(len(runner.workers), 2)
                self.assertEqual(runner.seen, [['README.md'], ['README.md', 'first.txt']])
                self.assertEqual(runner.heads, [self.base, self.base])
                integrated = result['tasks'][0]['details']['integrated_sha']
                self.assertEqual(
                    sorted(git(self.repo, 'ls-tree', '-r', '--name-only', integrated).split()),
                    ['README.md', 'first.txt', 'second.txt'])
                self.assertEqual(git(self.repo, 'show', f'{integrated}:first.txt'), 'partial work')
                self.assertEqual(git(self.repo, 'show', f'{integrated}:second.txt'), 'the resumption')
                failed = next(a for a in result['attempts'] if a['status'] == 'failed')
                self.assertEqual(failed['details']['failure_category'], category)

    def test_a_resumption_escalates_and_names_a_revision_that_resolves(self):
        """Criterion 5: a resumed attempt escalates like a fresh retry, unlike
        an amend. Criterion 6: its routing_decision names the exhausted
        revision it restored, and that revision resolves through the retained
        ref rather than depending on one existing (criterion 3)."""
        result, _ = self.resume_run()
        decisions = self.decisions_for(result)
        self.assertEqual([d['profile'] for d in decisions], ['standard', 'strong'])
        self.assertIn('resumption of exhausted attempt', decisions[1]['reason'])
        exhausted = next(a for a in result['attempts'] if a['status'] == 'failed')
        resumed = next(a for a in result['attempts'] if a['status'] == 'done')
        decision = next(event['details']['body'] for event in result['events']
                        if event['details'].get('message_kind') == 'routing_decision'
                        and event['attempt_id'] == resumed['id'])
        revision = decision['resumed_exhausted_attempt']
        reference = f"refs/anvil/exhausted/{result['run_id']}/{exhausted['id']}"
        self.assertEqual(git(self.repo, 'rev-parse', reference), revision)
        self.assertEqual(git(self.repo, 'cat-file', '-t', revision), 'commit')
        # The attempt cap is unchanged: one resumption is the only retry.
        self.assertEqual(sum(e['kind'] == 'retry' for e in result['events']), 1)
        self.assertEqual(len(result['attempts']), 2)

    def test_an_exhaustion_with_no_retry_available_still_stops_the_run_unchanged(self):
        """Criterion 1: with no retry available -- no adaptive configuration,
        or an attempt cap already spent -- the run stops exactly as it did
        before this ticket: same status, same error, same failure category,
        and no branch advance. A budget or process failure that is not an
        exhaustion is unaffected by any of this."""
        for adaptive in (None, config_options(attempts=1)):
            with self.subTest(adaptive=bool(adaptive)):
                self.resume_ticket()
                cfg = replace(self.config, adaptive=adaptive, workers=(WorkerConfig('one'),))
                runner = ExhaustingWorker()
                result = run_parallel(cfg, runners={'one': runner}, review_runner=runner)
                self.assertEqual(result['status'], 'failed')
                failed_task = next(t for t in result['tasks'] if t['status'] == 'failed')
                self.assertEqual(failed_task['details']['failure_category'], 'turn_exhaustion')
                self.assertIn('exhausted_sha', failed_task['details'])
                self.assertEqual(len(runner.workers), 1)
                self.assertEqual(git(self.repo, 'rev-parse', result['branch']), self.base)
        # An ordinary process failure, never an exhaustion, is unaffected.
        cfg = replace(self.config, adaptive=config_options(attempts=2), workers=(WorkerConfig('one'),))
        result = run_parallel(cfg, runners={'one': FakeRunner('interrupt')}, review_runner=FakeRunner())
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(len(result['attempts']), 1)

    def test_a_resumption_is_refused_once_a_peer_has_advanced_the_base(self):
        """Criterion 2, and the failure the guard exists to prevent, mirroring
        the amend guard test. The second case replaces _resume_source with one
        that ignores the base: that is a version which always resumes, and it
        runs here. Under it the peer's integrated file is deleted from the
        accepted tree by a commit whose single parent is the new base, which
        is what the case asserts. An implementation that always resumes fails
        the first case: it would lose peer.txt there too.
        """
        always = lambda item, base, revision: revision
        for resumes_always in (False, True):
            with self.subTest(resumes_always=resumes_always):
                self.tickets.write_text(json.dumps({'version': 1, 'tasks': [
                    {'id': 'peer', 'title': 'Peer work', 'objective': 'Integrate first',
                     'depends_on': [], 'acceptance_criteria': ['peer.txt exists'],
                     'worker': 'quick'},
                    {'id': 'alpha', 'title': 'Slower work', 'objective': 'Finish after the peer',
                     'depends_on': [], 'acceptance_criteria': ['alpha.txt exists'],
                     'worker': 'slow'}]}))
                integrated = threading.Event()
                workers = {'quick': RacingWorker('peer'),
                           'slow': ExhaustingRacingWorker('alpha', wait_for=integrated)}
                cfg = replace(self.config, adaptive=config_options(attempts=2),
                              workers=(WorkerConfig('quick'), WorkerConfig('slow')),
                              max_processes=3)
                with (patch('anvil.parallel._resume_source', always) if resumes_always
                      else nullcontext()):
                    result = run_parallel(
                        cfg, runners=workers, review_runner=FakeRunner(),
                        progress=lambda m: integrated.set() if m == 'peer: done' else None)
                self.assertEqual(result['status'], 'success', result.get('error'))
                tree = git(self.repo, 'ls-tree', '-r', '--name-only', result['branch']).split()
                self.assertIn('alpha.txt', tree)
                if resumes_always:
                    self.assertNotIn('peer.txt', tree)
                    continue
                # The peer's integrated work survives the replacement attempt.
                self.assertIn('peer.txt', tree)
                self.assertEqual(git(self.repo, 'show', f"{result['branch']}:peer.txt"), 'peer')
                # The fallback is today's fresh worktree on the advanced base,
                # and a fresh-worktree retry escalates exactly as it did before.
                self.assertIn('peer.txt', workers['slow'].seen[1])
                decisions = self.decisions_for(result, 'alpha')
                self.assertEqual([d['profile'] for d in decisions], ['standard', 'strong'])
                self.assertNotIn('resumed_exhausted_attempt', decisions[1])


class AdaptiveRunLevelSettings(unittest.TestCase):
    """agent_turns is a run-level setting; routing must not drop it."""

    setUp = test_execution.SerialExecutionTests.setUp

    def session(self, **overrides):
        from dataclasses import replace
        from anvil.adaptive_runtime import Session
        from anvil.config import WorkerConfig
        from anvil.planning import TaskGraph
        from anvil.workspaces import Repository
        config = replace(self.config, agent="codex",
                         workers=(WorkerConfig("only", "codex", "codex"),),
                         max_processes=1, adaptive=config_options(), **overrides)
        graph = TaskGraph.load(config.tickets)
        return Session(config, graph, Repository(self.repo), injected=True), graph, config

    def runner_kwargs(self, **overrides):
        from unittest.mock import patch
        session, graph, config = self.session(**overrides)
        task = graph.tasks[0]
        worker = config.workers[0]
        session.decide(task, worker, self.base, "attempt-1")

        class Item:
            attempt_id = "attempt-1"

        item = Item()
        item.worker = worker
        item.task = task
        with patch("anvil.adaptive_runtime.create_runner") as create:
            session.runner(item)
            worker_call = create.call_args
            session.runner(item, review=True)
            review_call = create.call_args
        return worker_call.kwargs, review_call.kwargs

    def test_agent_turns_reaches_both_roles_under_adaptive_routing(self):
        worker_kwargs, review_kwargs = self.runner_kwargs(agent_turns=96)
        self.assertEqual(worker_kwargs["turns"], 96)
        self.assertEqual(review_kwargs["turns"], 96)

    def test_absent_agent_turns_leaves_the_adapter_default(self):
        worker_kwargs, review_kwargs = self.runner_kwargs()
        self.assertIsNone(worker_kwargs["turns"])
        self.assertIsNone(review_kwargs["turns"])
