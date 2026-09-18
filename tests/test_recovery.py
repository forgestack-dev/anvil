"""Recovery at public execution boundaries with real Git and deterministic workers."""
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from anvil.config import WorkerConfig
from anvil.environment import managed_environment
from anvil.contracts import ContractError
from anvil.execution import run_serial
from anvil.parallel import run_parallel
from anvil.recovery import resume, assert_quiescent
from anvil.store import RunStore, StoreError
from anvil.workspaces import Repository, RepositoryLock, WorkspaceError
import test_execution
from test_execution import FakeRunner, git
from test_adaptive import config_options


class RecoveryTests(unittest.TestCase):
    setUp = test_execution.SerialExecutionTests.setUp

    def resume_with(self, result, runner=None):
        runner = runner or FakeRunner()
        restored = resume(Path(result['run_dir']), runners={'serial': runner}, review_runner=runner)
        return restored, runner

    def interrupted(self, phase='worker', *, adaptive=False, ticket_status=False):
        cfg = replace(self.config, adaptive=config_options(attempts=2) if adaptive else None,
                      ticket_status=ticket_status)
        if phase == 'worker':
            return run_serial(cfg, runner=FakeRunner('interrupt'))
        fired = False
        def notify(message):
            nonlocal fired
            if not fired and phase in message:
                fired = True
                raise KeyboardInterrupt
        return run_serial(cfg, runner=FakeRunner(), progress=notify)

    def test_restart_worker_retires_token_and_preserves_artifacts(self):
        first = self.interrupted()
        old = first['attempts'][0]
        result, runner = self.resume_with(first)
        self.assertEqual(result['run_id'], first['run_id'])
        self.assertEqual(result['status'], 'success', result['error'])
        self.assertEqual(len(runner.workers), 2)
        self.assertTrue(Path(old['workspace']).is_dir())
        self.assertEqual(result['attempts'][0]['status'], 'interrupted')
        self.assertEqual(git(self.repo, 'show', f"{result['branch']}:value.txt"), '2')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.base)
        with RunStore(Path(result['run_dir'])/'state.sqlite') as store:
            with self.assertRaises(StoreError):
                store.record_message('t1', attempt_id=old['id'], kind='late', body={})

    def test_completed_ticket_is_not_repeated_and_handoff_survives(self):
        first = self.interrupted('t1: done')
        details = first['tasks'][0]['details']
        result, runner = self.resume_with(first)
        self.assertEqual(result['status'], 'success', result['error'])
        self.assertEqual(result['tasks'][0]['details'], details)
        self.assertEqual(len(runner.workers), 1)
        self.assertEqual(runner.workers[0], details['integrated_sha'])

    def test_review_and_verification_interruptions_reuse_immutable_candidate(self):
        for phase in ('t1: reviewing', 't1: verifying'):
            with self.subTest(phase=phase):
                first = self.interrupted(phase)
                old = Path(first['attempts'][0]['workspace'])
                (old/'value.txt').write_text('late old worker mutation')
                result, runner = self.resume_with(first)
                self.assertEqual(result['status'], 'success', result['error'])
                self.assertEqual(len(runner.workers), 1)  # only dependent t2
                self.assertEqual(len(runner.reviews), 2)
                self.assertEqual((old/'value.txt').read_text(), 'late old worker mutation')
                self.assertTrue(result['tasks'][0]['details']['recovered_candidate'])

    def test_branch_advance_before_ledger_completion_reconciles_once(self):
        advance = Repository.advance_branch
        fired = False
        def crash(repo, branch, old, new):
            nonlocal fired
            advance(repo, branch, old, new)
            if not fired:
                fired = True
                raise KeyboardInterrupt
        with patch.object(Repository, 'advance_branch', crash):
            first = run_serial(self.config, runner=FakeRunner())
        self.assertEqual(first['tasks'][0]['status'], 'interrupted')
        tip = git(self.repo, 'rev-parse', first['branch'])
        result, runner = self.resume_with(first)
        self.assertEqual(result['status'], 'success', result['error'])
        self.assertEqual(result['tasks'][0]['details']['integrated_sha'], tip)
        self.assertEqual(len(runner.workers), 1)
        self.assertEqual(sum(e['kind']=='task' and e['task_id']=='t1' and e['to_status']=='done'
                             for e in result['events']), 1)

    def test_before_branch_advance_repeats_review_and_checks(self):
        with patch.object(Repository, 'advance_branch', side_effect=KeyboardInterrupt):
            first = run_serial(self.config, runner=FakeRunner())
        self.assertEqual(git(self.repo, 'rev-parse', first['branch']), self.base)
        result, runner = self.resume_with(first)
        self.assertEqual(result['status'], 'success', result['error'])
        self.assertEqual(len(runner.reviews), 2)
        self.assertEqual(len(runner.workers), 1)

    def test_changed_inputs_and_foreign_branch_leave_ledger_untouched(self):
        first = self.interrupted()
        path = Path(first['run_dir'])/'state.sqlite'
        before = RunStore.read(path)
        original = self.tickets.read_text()
        doc = json.loads(original); doc['tasks'][0]['objective']='changed'
        self.tickets.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ContractError, 'inputs changed'):
            self.resume_with(first)
        self.assertEqual(RunStore.read(path), before)
        self.tickets.write_text(original)
        (self.repo/'extra').write_text('unrelated')
        git(self.repo,'add','.')
        git(self.repo,'-c','user.name=T','-c','user.email=t@t','commit','-qm','Unrelated')
        git(self.repo,'update-ref',f"refs/heads/{first['branch']}",'HEAD')
        with self.assertRaisesRegex(ContractError, 'without a verified'):
            self.resume_with(first)
        self.assertEqual(RunStore.read(path), before)

    def test_running_supervisor_and_live_orphan_block_resume(self):
        first = self.interrupted()
        with RepositoryLock(Repository(self.repo)):
            with self.assertRaises(WorkspaceError):
                self.resume_with(first)
        child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], start_new_session=True)
        try:
            record = Path(first['run_dir'])/'commands'/'orphan.json'
            record.write_text(json.dumps({'phase':'running','pgid':child.pid}))
            with self.assertRaisesRegex(ContractError,'may still be alive'):
                self.resume_with(first)
        finally:
            os.killpg(child.pid,signal.SIGKILL); child.wait()
        result, _ = self.resume_with(first)
        self.assertEqual(result['status'],'success',result['error'])

    def test_spawn_gap_is_fail_closed(self):
        first = self.interrupted()
        (Path(first['run_dir'])/'commands'/'spawn.json').write_text('{"phase":"spawning"}')
        with self.assertRaisesRegex(ContractError, 'unresolved command spawn'):
            self.resume_with(first)

    def test_status_and_adaptive_budget_survive_resume(self):
        first = self.interrupted(adaptive=True, ticket_status=True)
        result, _ = self.resume_with(first)
        self.assertEqual(result['status'], 'success', result['error'])
        self.assertEqual(result['ticket_publication']['status'], 'synchronized')
        decisions = [e['details']['body'] for e in result['events']
                     if e['details'].get('message_kind')=='routing_decision']
        self.assertEqual([d['reservation']['cumulative_invocations'] for d in decisions],[2,4,6])
        self.assertEqual(result['learning']['status'],'excluded_recovery_evidence')

    def test_remaining_budget_is_enforced(self):
        options=config_options(attempts=2); options['max_invocations']=2
        first=run_serial(replace(self.config,adaptive=options),runner=FakeRunner('interrupt'))
        result, runner=self.resume_with(first)
        self.assertEqual(result['status'],'failed')
        self.assertIn('budget exhausted',result['error'])
        self.assertEqual(runner.workers,[])

    def test_failed_run_is_not_implicitly_retried(self):
        first=run_serial(self.config,runner=FakeRunner('bad-check'))
        with self.assertRaisesRegex(ContractError,'requires an interrupted'):
            self.resume_with(first)

    def test_old_ledger_is_not_adopted(self):
        first=self.interrupted()
        with RunStore(Path(first['run_dir'])/'state.sqlite') as store:
            store._connection.execute("DELETE FROM events WHERE kind='recovery_protocol'")
        with self.assertRaisesRegex(ContractError,'predates native recovery'):
            self.resume_with(first)

    def test_repeated_resume_keeps_candidate_and_completed_work(self):
        first = self.interrupted('t1: reviewing')
        class InterruptReview(FakeRunner):
            def run(self, **kw):
                if kw.get('read_only'):
                    raise KeyboardInterrupt
                return super().run(**kw)
        second, _ = self.resume_with(first, InterruptReview())
        self.assertEqual(second['status'], 'interrupted', second['error'])
        third, runner = self.resume_with(second)
        self.assertEqual(third['status'], 'success', third['error'])
        self.assertEqual(len(runner.workers), 1)
        self.assertEqual(sum(e['kind']=='recovery' for e in third['events']), 2)

    def test_unverifiable_candidate_restarts_implementation(self):
        first = self.interrupted('t1: reviewing')
        with RunStore(Path(first['run_dir'])/'state.sqlite') as store:
            details=first['tasks'][0]['details'] | {'candidate_sha':'0'*40}
            store._connection.execute('UPDATE tasks SET details=? WHERE id=?',(json.dumps(details),'t1'))
        result, runner = self.resume_with(first)
        self.assertEqual(result['status'],'success',result['error'])
        self.assertEqual(len(runner.workers),2)

    def test_resume_candidate_still_requires_passing_checks(self):
        first = self.interrupted('t1: reviewing')
        class Mutate(FakeRunner):
            def run(self, **kw):
                result=super().run(**kw)
                if kw.get('read_only'):
                    (kw['repo']/'value.txt').write_text('bad')
                return result
        result, _ = self.resume_with(first, Mutate())
        self.assertEqual(result['status'],'failed')
        self.assertEqual(git(self.repo,'rev-parse',result['branch']),self.base)
        self.assertEqual(result['tasks'][1]['status'],'pending')

    def test_recovery_does_not_reset_escalation_allowance(self):
        class RejectThenInterrupt(FakeRunner):
            def run(self, **kw):
                if kw.get('read_only'):
                    return {'verdict':'request_changes','summary':'Fix it',
                            'acceptance':[{'criterion':1,'satisfied':False,'evidence':'value.txt read'}],
                            'findings':[{'criterion':1,'location':'value.txt',
                                         'finding':'The value is wrong'}]}
                if self.workers:
                    raise KeyboardInterrupt
                return super().run(**kw)
        first=run_serial(replace(self.config,adaptive=config_options(attempts=2)),runner=RejectThenInterrupt())
        self.assertEqual(first['status'],'interrupted',first['error'])
        result, runner=self.resume_with(first,FakeRunner('review-rejects'))
        self.assertEqual(result['status'],'blocked',result['error'])
        self.assertEqual(len(runner.workers),1)
        self.assertEqual(sum(e['kind']=='retry' for e in result['events']),1)

    def test_unknown_usage_keeps_previous_currency_reservation(self):
        options=config_options(attempts=2)
        options['profiles']['standard']['reserve_usd']=1
        options['profiles']['strong']['reserve_usd']=10
        options['soft_budget_usd']=15
        first=run_serial(replace(self.config,adaptive=options),runner=FakeRunner('interrupt'))
        result, runner=self.resume_with(first)
        self.assertEqual(result['status'],'failed')
        self.assertIn('soft cost budget',result['error'])
        self.assertEqual(runner.workers,[])

    def test_parallel_pool_preserves_completed_dependency(self):
        cfg=replace(self.config, workers=(WorkerConfig('a'),WorkerConfig('b')),max_processes=2)
        def stop(message):
            if message=='t1: done': raise KeyboardInterrupt
        first=run_parallel(cfg,runners={'a':FakeRunner(),'b':FakeRunner()},review_runner=FakeRunner(),progress=stop)
        workers={'a':FakeRunner(),'b':FakeRunner()}
        result=resume(Path(first['run_dir']),runners=workers,review_runner=FakeRunner())
        self.assertEqual(result['status'],'success',result['error'])
        self.assertEqual(sum(len(w.workers) for w in workers.values()),1)
        self.assertEqual(result['tasks'][0]['details'],first['tasks'][0]['details'])

    def test_actual_supervisor_death_after_git_advance(self):
        config_path=self.root/'run.json';config_path.write_text(json.dumps(self.config.to_dict()))
        source=Path(__file__).resolve().parents[1]/'src'
        script='''
import os, sys
from pathlib import Path
from anvil.config import RunConfig
from anvil.execution import run_serial
from anvil.workspaces import Repository
from test_execution import FakeRunner
advance=Repository.advance_branch
def crash(repo, branch, old, new):
    advance(repo, branch, old, new)
    os._exit(73)
Repository.advance_branch=crash
run_serial(RunConfig.load(Path(sys.argv[1])),runner=FakeRunner())
'''
        env=os.environ.copy();env['PYTHONPATH']=os.pathsep.join([str(source),str(Path(__file__).parent.resolve())])
        process=subprocess.run([sys.executable,'-c',script,str(config_path)],env=env,capture_output=True,timeout=30)
        self.assertEqual(process.returncode,73,process.stderr.decode())
        run_dir=next(self.config.state_dir.iterdir())
        saved=RunStore.read(run_dir/'state.sqlite');saved['run_dir']=str(run_dir)
        self.assertEqual(saved['status'],'running')
        self.assertEqual(saved['tasks'][0]['status'],'integrating')
        result,runner=self.resume_with(saved)
        self.assertEqual(result['status'],'success',result['error'])
        self.assertEqual(len(runner.workers),1)
        self.assertEqual(git(self.repo,'show',f"{result['branch']}:value.txt"),'2')

    def test_newer_ticket_binding_and_changed_policy_refuse_without_reset(self):
        first=self.interrupted(ticket_status=True)
        path=Path(first['run_dir'])/'state.sqlite'
        before=RunStore.read(path)
        run_serial(replace(self.config,ticket_status=True),runner=FakeRunner())
        with self.assertRaisesRegex(ContractError,'newer run'):
            self.resume_with(first)
        self.assertEqual(RunStore.read(path),before)
        adaptive=self.interrupted(adaptive=True)
        frozen=Path(adaptive['run_dir'])/'routing-frozen.json'
        value=json.loads(frozen.read_text());value['policy_version']='changed'
        frozen.write_text(json.dumps(value))
        with self.assertRaisesRegex(ContractError,'frozen routing policy changed'):
            self.resume_with(adaptive)

    def test_cancelled_muse_handoff_stops_without_waiting_for_timeout(self):
        from anvil.adapters.muse import MuseRunner
        from anvil.processes import ProcessScope, ProcessCancelled
        scope=ProcessScope(1);scope.cancel()
        with scope.activate(), self.assertRaises(ProcessCancelled):
            MuseRunner().run(repo=self.repo,prompt='Read this',schema={},
                             artifact_dir=self.root/'muse-cancel',timeout=60)

    def test_startup_interruption_before_branch_creation_can_resume(self):
        first=self.interrupted('verifying the baseline')
        self.assertEqual(first['attempts'],[])
        result,runner=self.resume_with(first)
        self.assertEqual(result['status'],'success',result['error'])
        self.assertEqual(len(runner.workers),2)

    def test_command_registry_tracks_process_until_group_cleanup(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        from anvil.processes import ProcessScope, ProcessCancelled, run_process
        from anvil.recovery import track_commands
        scope=ProcessScope(1);directory=self.root/'tracked';directory.mkdir()
        track_commands(scope,directory)
        def work():
            with scope.activate():
                return run_process([sys.executable,'-c','import time;time.sleep(20)'],
                    cwd=self.repo,stdin=None,stdout_path=directory/'out',stderr_path=directory/'err',
                    timeout=30,env=managed_environment())
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(work)
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    records=list((directory/'commands').glob('*.json'))
                    if records and json.loads(records[0].read_text()).get('phase')=='running':
                        break
                    time.sleep(.01)
                else:
                    self.fail('process was not durably registered')
                with self.assertRaisesRegex(ContractError,'may still be alive'):
                    assert_quiescent(directory)
            finally:
                scope.cancel()
            with self.assertRaises(ProcessCancelled):future.result(timeout=5)
        self.assertEqual(list((directory/'commands').glob('*.json')),[])
