from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_execution
from test_adaptive import config_options
from anvil.config import RunConfig, WorkerConfig
from anvil.contracts import ContractError
from anvil.routing import validate_config, Policy, fingerprint, learning_catalog, resolve_cost_basis
from anvil.planning import TaskGraph
from anvil.workspaces import Repository
from anvil.telemetry import usage
from anvil.learning import train, promote, load_policy, history, rollback


class RoutingComponents(unittest.TestCase):
    setUp = test_execution.SerialExecutionTests.setUp

    def test_profiles_reject_invalid_controls_and_unknown_options(self):
        for field,value in [('effort','bogus'),('rank',True),('model','x\n--unsafe'),('agent','other')]:
            bad=config_options();bad['profiles']['standard'][field]=value
            with self.subTest(field=field),self.assertRaises(ContractError):validate_config(bad)
        for key,value in [('max_attempts',3),('max_invocations',True),('soft_budget_usd',float('nan'))]:
            bad=config_options();bad[key]=value
            with self.subTest(key=key),self.assertRaises(ContractError):validate_config(bad)

    def test_override_requires_compatible_worker_and_missing_history_uses_rules(self):
        options=config_options('adaptive')
        cfg=replace(self.config,adaptive=options,workers=(WorkerConfig('one'),),max_processes=1)
        graph=TaskGraph.load(self.tickets);repo=Repository(self.repo)
        policy=Policy(cfg,graph,repo)
        self.assertIsNone(policy.learned)
        task=replace(graph.tasks[0],profile='strong')
        self.assertEqual(policy.decision(task,cfg.workers[0],repo.head())['profile'],'strong')
        self.assertFalse(policy.compatible(task,WorkerConfig('claude','claude-code')))

    def test_normalize_claude_terminal_usage_without_message_double_counting(self):
        path=self.root.resolve()/'events.jsonl'
        events=[{'type':'assistant','usage':{'input_tokens':999999}},
                {'type':'result','usage':{'input_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':30,'output_tokens':5},'total_cost_usd':0.2,'modelUsage':{'test':{}}},
                {'type':'system','subtype':'task_summary'}]
        path.write_text('\n'.join(json.dumps(e) for e in events))
        data=usage(path,'claude-code',{})
        self.assertEqual(data['cost_usd'],0.2);self.assertEqual(data['input_tokens'],10)
        self.assertEqual(data['reported_model'],'test')

    def test_provider_reported_cost_basis_distinguishes_billed_from_list(self):
        path = self.root.resolve() / 'events.jsonl'
        for basis in ('list', 'billed'):
            events = [{'type': 'result', 'usage': {'input_tokens': 10, 'output_tokens': 5},
                       'total_cost_usd': 0.2, 'modelUsage': {'test': {'costBasis': basis}}}]
            path.write_text(json.dumps(events[0]))
            data = usage(path, 'claude-code', {})
            self.assertEqual(data['cost_basis'], basis)
            self.assertEqual(data['cost_kind'], 'provider_reported_estimate')

    def test_absent_or_unrecognized_or_multiple_model_basis_stays_unknown(self):
        path = self.root.resolve() / 'events.jsonl'
        cases = (
            {'modelUsage': {'test': {}}},
            {'modelUsage': {'test': {'costBasis': 'promotional'}}},
            {'modelUsage': {}},
            {'modelUsage': {'a': {'costBasis': 'billed'}, 'b': {'costBasis': 'billed'}}},
            {},
        )
        for extra in cases:
            with self.subTest(extra=extra):
                event = {'type': 'result', 'usage': {'input_tokens': 10, 'output_tokens': 5},
                          'total_cost_usd': 0.2, **extra}
                path.write_text(json.dumps(event))
                data = usage(path, 'claude-code', {})
                self.assertEqual(data['cost_basis'], 'unknown')

    def test_cost_basis_round_trips_through_saved_invocation_state(self):
        from anvil.ticket_status import atomic, encoded, read_regular
        path = self.root.resolve() / 'events.jsonl'
        path.write_text(json.dumps({'type': 'result', 'usage': {'input_tokens': 10, 'output_tokens': 5},
                                     'total_cost_usd': 0.2, 'modelUsage': {'test': {'costBasis': 'billed'}}}))
        data = usage(path, 'claude-code', {})
        record_path = self.root.resolve() / 'invocation.json'
        atomic(record_path, encoded({'role': 'worker', **data, 'duration_seconds': 1}))
        reloaded = json.loads(read_regular(record_path))
        self.assertEqual(reloaded['cost_basis'], 'billed')

    def test_resolve_cost_basis_requires_an_explicit_api_key(self):
        billed, reason = resolve_cost_basis({'ANTHROPIC_API_KEY': 'secret'})
        self.assertEqual(billed, 'billed')
        self.assertTrue(reason)
        for environment in ({}, {'ANTHROPIC_API_KEY': ''}, {'OTHER': 'value'}):
            with self.subTest(environment=environment):
                basis, reason = resolve_cost_basis(environment)
                self.assertEqual(basis, 'list')
                self.assertTrue(reason)

    def test_codex_cached_tokens_are_not_priced_twice_and_missing_usage_is_unknown(self):
        path=self.root.resolve()/'events.jsonl'
        path.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':30,'output_tokens':10}}))
        profile={'price':{'version':'fixture','input':1,'cached_input':0.1,'cache_write':1,'output':5}}
        data=usage(path,'codex',profile)
        self.assertEqual(data['input_tokens'],70)
        self.assertAlmostEqual(data['cost_usd'],123/1_000_000)
        path.write_text('{}');data=usage(path,'codex',profile)
        self.assertIsNone(data['cost_usd']);self.assertIsNotNone(data['usage_error'])

    def populate(self, *, candidate_success=True, samples=100, attempts=1, options=None):
        options = options if options is not None else config_options(attempts=attempts)
        cfg = replace(self.config, adaptive=options)
        repo=Repository(self.repo)
        review_profile = options['profiles'][options['review_profile']]
        with history(repo) as db:
            for split in (0,1):
                for name,cost in [('standard',1.0),('strong',0.2)]:
                    for i in range(samples):
                        profile = options['profiles'][name]
                        value={'catalog':learning_catalog(cfg),'profile':name,'agent':'codex',
                               'assessment':{'cohort':'rank-1','input_digest':f'{i*2+split:08x}'+'0'*56},
                               'cli_version':'fixture-v1','eligible':True,'cost':cost,'duration':1,
                               'reported_model':profile['model'],
                               'requested_model':profile['model'],
                               'accepted':True if name=='standard' else candidate_success,
                               'provenance':[{'worker':{'agent':'codex',
                                                        'requested_model':profile['model'],
                                                        'reported_model':profile['model'],
                                                        'cli_version':'fixture-v1'},
                                              'review':{'agent':review_profile['agent'],
                                                        'requested_model':review_profile['model'],
                                                        'reported_model':review_profile['model'],
                                                        'cli_version':'fixture-v1'}}]}
                        db.execute('INSERT INTO samples VALUES (?,?,?)',(f'{split}-{name}-{i}','t',json.dumps(value)))
        return repo,cfg

    def test_learning_requires_held_out_quality_and_explicit_promotion(self):
        repo,cfg=self.populate()
        policy=train(repo,cfg,min_samples=20,max_quality_loss=0.1,min_cost_improvement=0.2,max_latency_ratio=1.1)
        self.assertTrue(policy['validated']);self.assertEqual(policy['routes']['codex:rank-1'],'strong')
        self.assertIsNone(load_policy(repo,None,policy['catalog']))
        promote(repo,policy['id'],policy['catalog'])
        self.assertEqual(load_policy(repo,None,policy['catalog'])['id'],policy['id'])
        rollback(repo);self.assertIsNone(load_policy(repo,None,policy['catalog']))
        repeated=train(repo,cfg,min_samples=20,max_quality_loss=0.1,min_cost_improvement=0.2,max_latency_ratio=1.1)
        self.assertEqual(policy['id'],repeated['id'])

    def test_cheaper_failed_model_is_never_promoted(self):
        repo,cfg=self.populate(candidate_success=False)
        policy=train(repo,cfg,min_samples=20,max_quality_loss=0.1,min_cost_improvement=0.2,max_latency_ratio=1.1)
        self.assertFalse(policy['validated'])
        with self.assertRaises(ContractError):promote(repo,policy['id'],policy['catalog'])

    def test_sparse_history_and_catalog_change_cannot_activate_policy(self):
        repo,cfg=self.populate(samples=2)
        policy=train(repo,cfg,min_samples=5,max_quality_loss=0.1,min_cost_improvement=0.2,max_latency_ratio=1.1)
        self.assertFalse(policy['validated'])
        with self.assertRaises(ContractError):load_policy(repo,policy['id'],'another-catalog')

    def test_malformed_usage_never_claims_free_work(self):
        path=self.root.resolve()/'events.jsonl'
        for value in (None,[],42):
            path.write_text(json.dumps({'type':'result','usage':value}))
            data=usage(path,'claude-code',{})
            self.assertIsNone(data['cost_usd']);self.assertIsNotNone(data['usage_error'])

    def test_profile_null_and_unsupported_haiku_effort_are_rejected(self):
        value=self.config.to_dict();value['adaptive']=None
        with self.assertRaises(ContractError):RunConfig.from_document(value,base=self.root)
        value=config_options();value['profiles']['standard'].update(agent='claude-code',model='claude-haiku-4-5-20251001',effort='low')
        with self.assertRaises(ContractError):validate_config(value)

    def test_training_and_active_policy_require_matching_execution_conditions(self):
        repo, cfg = self.populate(attempts=2)
        gates = dict(min_samples=20, max_quality_loss=0.1,
                     min_cost_improvement=0.2, max_latency_ratio=1.1)
        policy = train(repo, cfg, **gates)
        self.assertTrue(policy['validated'])
        promote(repo, policy['id'], policy['catalog'])
        variants = []
        options = deepcopy(cfg.adaptive); options['max_attempts'] = 1
        variants.append(replace(cfg, adaptive=options))
        options = deepcopy(cfg.adaptive); options['review_profile'] = 'standard'
        variants.append(replace(cfg, adaptive=options))
        variants.append(replace(cfg, verification=(('true',),)))
        for changed in variants:
            with self.subTest(config=changed.to_dict()):
                self.assertFalse(train(repo, changed, **gates)['validated'])
                with self.assertRaises(ContractError):
                    promote(repo, policy['id'], learning_catalog(changed))
                options = deepcopy(changed.adaptive); options['mode'] = 'adaptive'
                execution = replace(changed, adaptive=options,
                                    workers=(WorkerConfig('one'),), max_processes=1)
                with self.assertRaises(ContractError):
                    Policy(execution, TaskGraph.load(self.tickets), repo)

    def test_old_accounting_catalog_is_not_training_evidence(self):
        repo, cfg = self.populate()
        with history(repo) as db:
            for run_id, task_id, data in db.execute('SELECT * FROM samples').fetchall():
                row = json.loads(data)
                row['catalog'] = fingerprint(cfg.adaptive['profiles'])
                db.execute('UPDATE samples SET data=? WHERE run_id=? AND task_id=?',
                           (json.dumps(row), run_id, task_id))
        policy = train(repo, cfg, min_samples=20, max_quality_loss=0.1,
                       min_cost_improvement=0.2, max_latency_ratio=1.1)
        self.assertFalse(policy['validated'])

    def test_learning_catalog_binds_execution_timeouts(self):
        repo, cfg = self.populate()
        catalog = learning_catalog(cfg)
        self.assertNotEqual(learning_catalog(replace(cfg, agent_timeout=cfg.agent_timeout + 1)), catalog)
        self.assertNotEqual(learning_catalog(replace(cfg, check_timeout=cfg.check_timeout + 1)), catalog)
        self.assertEqual(learning_catalog(replace(cfg, ticket_status=not cfg.ticket_status)), catalog)

    def test_training_groups_cli_versions_by_agent(self):
        from anvil.learning import _provenance_ok
        def invocation(agent, cli):
            return {'agent': agent, 'requested_model': 'm', 'reported_model': 'm',
                    'cli_version': cli}
        # A mixed pool (Codex worker, Claude reviewer) naturally reports
        # different CLI versions per agent; the sample stays eligible.
        row = {'provenance': [
            {'worker': invocation('codex', 'codex-v1'),
             'review': invocation('claude-code', 'claude-v9')},
            {'worker': invocation('codex', 'codex-v1'),
             'review': invocation('claude-code', 'claude-v9')}]}
        self.assertTrue(_provenance_ok(row))
        # Two versions for the same agent are still rejected.
        row['provenance'][1]['worker']['cli_version'] = 'codex-v2'
        self.assertFalse(_provenance_ok(row))

    def test_training_binds_reviewer_cli_version_per_agent(self):
        options = config_options()
        options['profiles']['reviewer'] = {'agent': 'claude-code', 'model': 'test-reviewer',
                                           'effort': 'low', 'rank': 1}
        options['review_profile'] = 'reviewer'
        repo, cfg = self.populate(options=options)
        with history(repo) as db:
            for run_id, task_id, data in db.execute('SELECT * FROM samples').fetchall():
                row = json.loads(data)
                row['provenance'] = [
                    {'worker': {'agent': 'codex', 'requested_model': row['requested_model'],
                                'reported_model': row['reported_model'], 'cli_version': 'codex-v1'},
                     'review': {'agent': 'claude-code', 'requested_model': 'test-reviewer',
                                'reported_model': 'test-reviewer', 'cli_version': 'claude-v9'}}]
                db.execute('UPDATE samples SET data=? WHERE run_id=? AND task_id=?',
                           (json.dumps(row), run_id, task_id))
        policy = train(repo, cfg, min_samples=20, max_quality_loss=0.1,
                       min_cost_improvement=0.2, max_latency_ratio=1.1)
        self.assertTrue(policy['validated'])
        # Worker and reviewer versions are bound per agent, not collapsed.
        self.assertEqual(policy['cli_versions'],
                         {'codex': 'fixture-v1', 'claude-code': 'claude-v9'})

    def test_training_rejects_mismatched_invocation_provenance(self):
        def worker(model='test-standard', cli='fixture-v1', reported=None):
            return {'requested_model': model, 'reported_model': reported if reported is not None else model,
                    'cli_version': cli}
        cases = {
            'escalated worker model mismatch': [
                {'worker': worker(), 'review': None},
                {'worker': worker(reported='unexpected-model'), 'review': None}],
            'reviewer model mismatch': [
                {'worker': worker(), 'review': worker(reported='unexpected-model')}],
            'reviewer cli mismatch': [
                {'worker': worker(), 'review': worker(cli='other-v1')}],
        }
        repo, cfg = self.populate()
        for label, provenance in cases.items():
            with self.subTest(label=label):
                with history(repo) as db:
                    for run_id, task_id, data in db.execute('SELECT * FROM samples').fetchall():
                        row = json.loads(data)
                        row['provenance'] = provenance
                        db.execute('UPDATE samples SET data=? WHERE run_id=? AND task_id=?',
                                   (json.dumps(row), run_id, task_id))
                policy = train(repo, cfg, min_samples=20, max_quality_loss=0.1,
                               min_cost_improvement=0.2, max_latency_ratio=1.1)
                self.assertFalse(policy['validated'])

    def test_training_rejects_missing_or_mixed_reviewer_version(self):
        # The reviewer version is unknown when provenance lacks review
        # invocations, and inconsistent when rows disagree: either way the
        # acceptance labels are not comparable and no route may validate.
        # The reviewer agent differs from the worker agent so per-row
        # provenance stays consistent while rows disagree with each other.
        options = config_options()
        options['profiles']['reviewer'] = {'agent': 'claude-code', 'model': 'test-reviewer',
                                           'effort': 'low', 'rank': 1}
        options['review_profile'] = 'reviewer'
        repo, cfg = self.populate(options=options)
        def worker(row):
            return {'agent': 'codex', 'requested_model': row['requested_model'],
                    'reported_model': row['reported_model'], 'cli_version': row['cli_version']}
        def review(cli):
            return {'agent': 'claude-code', 'requested_model': 'test-reviewer',
                    'reported_model': 'test-reviewer', 'cli_version': cli}
        cases = {
            'missing reviewer version': lambda row, i: [
                {'worker': worker(row), 'review': None}],
            'mixed reviewer versions': lambda row, i: [
                {'worker': worker(row), 'review': review('claude-v9' if i % 2 else 'other-v9')}],
        }
        for label, provenance in cases.items():
            with self.subTest(label=label):
                with history(repo) as db:
                    for n, (run_id, task_id, data) in enumerate(
                            db.execute('SELECT * FROM samples').fetchall()):
                        row = json.loads(data)
                        row['provenance'] = provenance(row, n)
                        db.execute('UPDATE samples SET data=? WHERE run_id=? AND task_id=?',
                                   (json.dumps(row), run_id, task_id))
                policy = train(repo, cfg, min_samples=20, max_quality_loss=0.1,
                               min_cost_improvement=0.2, max_latency_ratio=1.1)
                self.assertFalse(policy['validated'])
                self.assertEqual(policy['routes'], {})
