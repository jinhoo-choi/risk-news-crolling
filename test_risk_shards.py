import base64
import copy
import hashlib
import json
import unittest

from risk_queue import Queue, digest
from risk_shards import split_job, hydrate, chunks, MAX_BYTES


class Memory:
    def __init__(self, values):
        self.values = copy.deepcopy(values)
    def get(self, path):
        return self.values.get(path), 'sha'


class Shards(unittest.TestCase):
    def fixture(self):
        items = [{'article_id':str(i),'title':'기사 '+str(i),'description':'공개 사실'} for i in range(621)]
        bodies = [{'article_id':str(i),'body':'확인할 공개 원문 '*600,'body_status':'verified'} for i in range(621)]
        inputs = {'items':items,'raw_articles':items,'history_hash':'x','exposure_hash':'y'}
        return {'job_id':'large-test','input':inputs,'input_hash':digest(inputs),
                'body_manifest':bodies,'body_manifest_hash':digest(bodies)}
    def test_realistic_scale_reconstructs_every_id_and_hash(self):
        full = self.fixture()
        small, values = split_job(full)
        self.assertNotIn('body_manifest', small)
        self.assertNotIn('items', small['input'])
        reconstructed = hydrate(Memory(values), small)
        self.assertEqual(reconstructed['input'], full['input'])
        self.assertEqual(reconstructed['body_manifest'], full['body_manifest'])
        for value in values.values():
            self.assertLessEqual(len(json.dumps(value,ensure_ascii=False).encode()), MAX_BYTES)
    def test_missing_or_changed_shard_stops_reconstruction(self):
        small, values = split_job(self.fixture())
        key = next(iter(values))
        missing = Memory(values)
        del missing.values[key]
        with self.assertRaises(RuntimeError): hydrate(missing, small)
        changed = Memory(values)
        changed.values[key][0]['title'] = '변경됨'
        with self.assertRaises(RuntimeError): hydrate(changed, small)
    def test_path_from_another_job_rejected(self):
        small, values = split_job(self.fixture())
        small['input_parts']['items'][0]['path'] = 'inputs/other/items-000.json'
        with self.assertRaises(RuntimeError): hydrate(Memory(values), small)
    def test_oversized_single_source_never_truncated(self):
        with self.assertRaises(RuntimeError): chunks([{'body':'원'*MAX_BYTES}])
    def test_large_blob_fallback_is_hash_checked(self):
        raw = json.dumps({'large':'x'*(1024*1024)}).encode()
        sha = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        queue = object.__new__(Queue)
        def request(path):
            if path.startswith('/contents/'):
                return {'type':'file','sha':sha,'encoding':'none','content':''}
            return {'encoding':'base64','content':base64.b64encode(raw).decode()}
        queue.request = request
        value, actual = queue.get('jobs/large-test.json')
        self.assertEqual(actual, sha)
        self.assertEqual(len(value['large']), 1024*1024)
        queue.request = lambda path: {'type':'file','sha':'0'*40,'encoding':'base64','content':base64.b64encode(raw).decode()}
        with self.assertRaises(RuntimeError): queue.get('jobs/large-test.json')


if __name__ == '__main__':
    unittest.main()
