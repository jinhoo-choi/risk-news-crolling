import copy
import json
import unittest

from risk_queue import digest
from risk_review_context import queue_status, status_view, status_index, usage_observation


class ReadOnlyQueue:
    def __init__(self, values):
        self.values = copy.deepcopy(values)
        self.reads = []

    def get(self, path):
        self.reads.append(path)
        value = self.values.get(path)
        return copy.deepcopy(value), digest(value) if value is not None else None


class ContextTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
