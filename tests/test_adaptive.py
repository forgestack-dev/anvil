"""Public execution regressions for source status, routing, and bounded escalation."""
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_execution
from test_execution import FakeRunner, git
from anvil.execution import run_serial
from anvil.config import WorkerConfig
from anvil.parallel import run_parallel
from anvil.contracts import ContractError
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
