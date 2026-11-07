import copy
import contextlib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from risk_queue import digest
from risk_review_context import queue_status, status_view, status_index, usage_observation, analysis_plan, canonical, instruction_context


class ReadOnlyQueue:
    def __init__(self, values):
        self.values = copy.deepcopy(values)
        self.reads = []

    def get(self, path):
        self.reads.append(path)
        value = self.values.get(path)
        return copy.deepcopy(value), digest(value) if value is not None else None


class ContextTests(unittest.TestCase):
    def test_current_instructions_preserve_business_text_and_schema_without_legacy_commands(self):
        root = Path(__file__).parent
        with contextlib.chdir(root), patch('socket.socket', side_effect=AssertionError('network prohibited')):
            view = instruction_context()
        filtered = (root/'filter_prompt.txt').read_text()
        self.assertEqual(view['filter'], filtered.split('[구독 모드 적용 범위]', 1)[1]
                         .split('[기존 API 전용 중복 기사 처리]', 1)[0].strip())
        self.assertEqual(view['schema'], json.loads((root/'risk_result_schema.json').read_text()))
        self.assertEqual(view['policy_hash'], digest(view['policy']))
        self.assertNotIn('id 최소값 1건만 true', view['filter'])
        self.assertNotIn('조치는 최대 3개', view['action'])
        self.assertIn('5개 구성요소', view['action'])
        self.assertIn('보류 해소는 기다리지 않는다', view['operations'])
        self.assertIn('accepted 재발송', view['operations'])
        self.assertIn('14시 미발송 시 원복 검토', view['operations'])
        self.assertNotIn('ANTHROPIC_API_KEY', view['operations'])
        for name, source in view['sources'].items():
            self.assertEqual(source['sha256'], hashlib.sha256((root/name).read_bytes()).hexdigest())

    def test_instruction_scope_and_even_omitted_prompt_hash_must_be_valid(self):
        root = Path(__file__).parent
        for mutation in ('omitted-text', 'missing-boundary', 'duplicate-boundary', 'legacy-policy'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                for name in ('risk_policy.json', 'risk_result_schema.json', 'filter_prompt.txt', 'action_prompt.txt', 'RUNBOOK.md'):
                    Path(tmp, name).write_bytes((root/name).read_bytes())
                with contextlib.chdir(tmp):
                    if mutation == 'omitted-text':
                        checked = json.loads(Path('risk_policy.json').read_text())
                        p = Path('action_prompt.txt'); p.write_text(p.read_text()+'\nlegacy changed')
                        with patch('risk_subscription.policy', return_value=checked), self.assertRaisesRegex(ValueError, '추출 중'):
                            instruction_context()
                    elif mutation == 'legacy-policy':
                        p = Path('risk_policy.json'); rules = json.loads(p.read_text())
                        rules['delivery_focus_version'] = 1
                        p.write_text(json.dumps(rules))
                    else:
                        p = Path('RUNBOOK.md'); text = p.read_text()
                        marker = '<!-- subscription-operations:start -->'
                        p.write_text(text.replace(marker, '' if mutation == 'missing-boundary' else marker+marker))
                    with self.assertRaises((ValueError, RuntimeError)):
                        instruction_context()

    def test_instruction_cli_needs_no_snapshot_and_never_overwrites_artifact(self):
        root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, 'instructions.json')
            command = [sys.executable, str(root/'risk_review_context.py'), '--instructions', '--output', str(target)]
            done = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            index = json.loads(done.stdout)
            self.assertEqual(index['scope'], 'new_production_v2_only')
            self.assertEqual(index['artifact_sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            original = target.read_bytes()
            self.assertNotEqual(subprocess.run(command, cwd=root, capture_output=True).returncode, 0)
            self.assertEqual(target.read_bytes(), original)

    def fixture(self):
        states = ['completed', 'pending', 'sending', 'uncertain',
                  'delivery_complete', 'expired', 'verified', 'cancelled']
        data = {'index.json': {'jobs': [str(i) for i in range(len(states))]}}
        for i, state in enumerate(states):
            data['jobs/'+str(i)+'.json'] = {
                'job_id': str(i), 'status': state, 'mode': 'production',
                'analysis_complete': False, 'deferred_ids': ['not-normal'],
                'receipts': {'private-recipient-hash': 'accepted' if i == 0 else 'sending'},
                'input': {'items': [{'article_id': 'one', 'body': '원문'*10000}]},
                'seen_after_snapshot': '과거원장'*10000,
                'error': {'cause_confirmed': False, 'message': 'not yet confirmed'},
                'future_required_field': {'must_keep': True}}
        return data

    def test_all_jobs_metadata_and_unknown_errors_preserved_without_writes(self):
        values = self.fixture()
        q = ReadOnlyQueue(values)
        view = queue_status(q, '2026-10-22T00:00:00Z')
        self.assertEqual([x['metadata']['job_id'] for x in view['jobs']], values['index.json']['jobs'])
        for entry in view['jobs']:
            original = values[entry['source']['path']]
            for key, value in entry['metadata'].items():
                self.assertEqual(value, original[key])
            self.assertEqual(entry['source']['payload_refs']['input']['sha256'], digest(original['input']))
            self.assertFalse(entry['metadata']['analysis_complete'])
        self.assertEqual(q.values, values)
        self.assertEqual(len(q.reads), len(values)+1)

    def test_missing_job_and_timeout_never_become_zero(self):
        values = self.fixture()
        del values['jobs/1.json']
        with self.assertRaises(ValueError):
            queue_status(ReadOnlyQueue(values), '2026-10-22T00:00:00Z')
        class Timeout(ReadOnlyQueue):
            def get(self, path):
                if path.startswith('jobs/'):
                    raise TimeoutError('observed read timeout')
                return super().get(path)
        with self.assertRaises(TimeoutError):
            queue_status(Timeout(self.fixture()), '2026-10-22T00:00:00Z')

    def test_index_conflict_blocks_and_fresh_restart_recovers(self):
        class Conflict(ReadOnlyQueue):
            def get(self, path):
                value, sha = super().get(path)
                if path == 'index.json' and len(self.reads) > 1:
                    sha = 'changed'
                return value, sha
        with self.assertRaises(ValueError):
            queue_status(Conflict(self.fixture()), '2026-10-22T00:00:00Z')
        self.assertEqual(len(queue_status(ReadOnlyQueue(self.fixture()), '2026-10-22T00:00:00Z')['jobs']), 8)

    def test_partial_wrong_identity_unknown_state_and_invalid_ids_rejected(self):
        for key, value in [('job_id', 'wrong'), ('status', 'new_unknown')]:
            values = self.fixture()
            values['jobs/0.json'][key] = value
            with self.assertRaises(ValueError):
                queue_status(ReadOnlyQueue(values), '2026-10-22T00:00:00Z')
        for ids in [['one', 'one'], ['../bad'], [{}]]:
            with self.assertRaises(ValueError):
                queue_status(ReadOnlyQueue({'index.json': {'jobs': ids}}), '2026-10-22T00:00:00Z')

    def test_changed_body_policy_schema_model_history_exposure_invalidate_reference(self):
        for key in ['body_hash', 'policy_hash', 'schema_hash', 'model_version',
                    'history_hash', 'exposure_hash', 'event_stage', 'correction_date']:
            job = {'job_id': 'one', 'status': 'pending', 'input': {key: 'before'}}
            first = status_view(job, digest(job))
            job['input'][key] = 'after'
            second = status_view(job, digest(job))
            self.assertNotEqual(first['source'], second['source'])
        # This only invalidates references; it never authorizes analysis caching.

    def test_usage_unknown_is_not_zero_and_cache_categories_are_separate(self):
        r = usage_observation(stage='classify', engine='subscription',
                              measurement_source='provider_adaptation')
        self.assertEqual(r['paid_ai_calls'], 0)
        self.assertIsNone(r['model_call_count'])
        self.assertTrue(all(v is None for v in r['tokens'].values()))
        explicit = {'input_tokens': 11, 'output_tokens': 12,
                    'cache_read_input_tokens': 13, 'cache_creation_input_tokens': 14}
        r = usage_observation(stage='classify', engine='api',
                              measurement_source='anthropic_response', usage=explicit,
                              attempt_number=2, retry_reason='HTTP 429', elapsed_ms=123)
        self.assertEqual(r['tokens'], explicit)
        self.assertEqual(r['retry_reason'], 'HTTP 429')
        self.assertIsNone(r['cost'])
        with self.assertRaises(ValueError):
            usage_observation(stage='classify', engine='subscription',
                              measurement_source='provider_adaptation', usage=explicit)
        for bad in [True, -1, float('nan')]:
            with self.assertRaises(ValueError):
                usage_observation(stage='classify', engine='api',
                                  measurement_source='anthropic_response', usage={'input_tokens': bad})

    def test_status_index_never_promotes_completed_to_analysis_complete(self):
        view = queue_status(ReadOnlyQueue(self.fixture()), '2026-10-22T00:00:00Z')
        index = status_index(view)
        self.assertEqual(len(index['jobs']), 8)
        self.assertEqual(index['jobs'][0]['status'], 'completed')
        self.assertFalse(index['jobs'][0]['analysis_complete'])
        self.assertEqual(index['jobs'][0]['deferred_count'], 1)
        self.assertTrue(index['full_metadata_required_for_decisions'])
        self.assertTrue(all(entry['ack'] is None for entry in index['jobs']))


class AnalysisPlanTests(unittest.TestCase):
    def fixture(self):
        items = [dict(article_id=i, title='기사', description='사건 설명', url='https://example.invalid/'+i)
                 for i in ['done', 'unread', 'missing']]
        manifest = [dict(article_id=i, body='원문과 숫자 123. '*2000 if i != 'missing' else '',
                         body_status='verified' if i != 'missing' else 'missing')
                    for i in ['done', 'unread', 'missing']]
        inputs = {'items': items, 'raw_articles': items}
        job = dict(job_id='test', input=inputs, input_hash=digest(inputs), policy_hash='policy',
                   body_manifest=manifest, body_manifest_hash=digest(manifest), review_pass_version=2)
        checkpoint = {k: job[k] for k in ['job_id', 'input_hash', 'policy_hash', 'body_manifest_hash']}
        checkpoint.update(covered_ids=['done'], classifications=[dict(article_id='done', relevant=False, reason='기존 판정')], analyses={})
        return job, checkpoint

    def test_resume_never_rereads_completed_and_preserves_every_source_character(self):
        job, checkpoint = self.fixture()
        original = copy.deepcopy(job)
        plan = analysis_plan(job, [checkpoint], max_bytes=1000)
        self.assertEqual(plan['pending_ids'], ['unread'])
        self.assertEqual(plan['missing_body_ids'], ['missing'])
        self.assertEqual(plan['classifications'][0], checkpoint['classifications'][0])
        self.assertIsNone(plan['classifications'][1]['relevant'])
        self.assertTrue(all(len(canonical(p).encode()) <= 1000 for p in plan['pages']))
        self.assertEqual(''.join(p['body'] for p in plan['pages']), job['body_manifest'][1]['body'])
        self.assertTrue(plan['pages'][-1]['complete'])
        self.assertFalse(any(p['complete'] for p in plan['pages'][:-1]))
        self.assertEqual(job, original)

    def test_checkpoint_corruption_or_changed_body_never_reuses(self):
        job, checkpoint = self.fixture()
        for field in ['job_id', 'input_hash', 'policy_hash', 'body_manifest_hash']:
            bad = copy.deepcopy(checkpoint)
            bad[field] = 'changed'
            with self.assertRaises(ValueError):
                analysis_plan(job, [bad])
        with self.assertRaises(ValueError):
            analysis_plan(job, [checkpoint, checkpoint])
        job['body_manifest'][0]['body'] += 'altered'
        with self.assertRaises(ValueError):
            analysis_plan(job, [checkpoint])

    def test_legacy_contract_is_not_silently_converted(self):
        job, checkpoint = self.fixture()
        job['review_pass_version'] = 1
        plan = analysis_plan(job, [checkpoint])
        self.assertEqual(plan['missing_body_ids'], [])
        self.assertEqual(plan['pending_ids'], ['unread', 'missing'])


if __name__ == '__main__':
    unittest.main()
