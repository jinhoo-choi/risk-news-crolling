"""Read-only executor views. No classification, cache reuse, writes or delivery.

Keep full private documents at their original paths. A status observation is not
authorization to consume: the existing fresh preflight/lock/ACK checks still run.
"""
import copy
import hashlib
import json
import math

from risk_queue import JOB_ID, digest
from risk_result_validator import utc_time


PAYLOAD_FIELDS = frozenset({'input', 'input_parts', 'body_manifest',
                            'raw_articles', 'seen_after_snapshot'})
STATES = frozenset({'pending', 'sending', 'uncertain', 'delivery_complete',
                    'completed', 'verified', 'expired', 'cancelled'})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def status_view(job, blob_sha):
    """Preserve every non-payload field, including unknown future metadata/errors.

    Source text/old ledgers are not classification inputs at this stage. References
    retain exact content hashes; semantic analysis must still load the full input.
    """
    if not isinstance(job, dict) or not JOB_ID.fullmatch(job.get('job_id', '')):
        raise ValueError('Invalid job identity')
    if job.get('status') not in STATES:
        raise ValueError('Unknown job state; cannot report an empty queue')
    if not isinstance(blob_sha, str) or not blob_sha:
        raise ValueError('Missing observed blob identity')
    result = {k: copy.deepcopy(v) for k, v in job.items() if k not in PAYLOAD_FIELDS}
    refs = {}
    for key in PAYLOAD_FIELDS & job.keys():
        encoded = canonical(job[key]).encode()
        refs[key] = {'sha256': hashlib.sha256(encoded).hexdigest(),
                     'canonical_utf8_bytes': len(encoded)}
    return {'metadata': result, 'source': {
        'path': 'jobs/'+job['job_id']+'.json', 'blob_sha': blob_sha,
        'payload_refs': refs},
        'scope': 'status_only_full_analysis_input_still_required'}


def queue_status(queue, observed_at):
    """Read all indexed jobs, and fail closed on missing/changed index or reads.

    The queue is an injected existing Queue (or offline fixture). No put/source,
    SMTP, model call, retry, expiry cleanup or state mutation occurs here.
    """
    utc_time(observed_at)
    index, initial_sha = queue.get('index.json')
    if not isinstance(index, dict) or not isinstance(index.get('jobs'), list):
        raise ValueError('Missing queue index')
    ids = index['jobs']
    if (any(not isinstance(jid, str) or not JOB_ID.fullmatch(jid) for jid in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Invalid or duplicate indexed job ID')
    jobs = []
    for jid in ids:
        job, sha = queue.get('jobs/'+jid+'.json')
        if not isinstance(job, dict) or job.get('job_id') != jid:
            raise ValueError('Missing or mismatched indexed job')
        jobs.append(status_view(job, sha))
    final, final_sha = queue.get('index.json')
    if final_sha != initial_sha or final != index:
        raise ValueError('Queue index changed during observation; fresh read required')
    return {'schema_version': 1, 'observed_at': observed_at,
            'observation_only': True, 'revalidate_before_write': True,
            'index_sha': final_sha, 'jobs': jobs,
            'semantic_analysis_performed': False}


def status_index(view):
    """Small observation index; never label delivery or all analysis complete.

    Full metadata/errors stay in the private status artifact, with exact hashes.
    Inspect that artifact for decisions; this index is only navigation and counts.
    """
    jobs = []
    for entry in view['jobs']:
        m = entry['metadata']
        receipts = m.get('receipts', {})
        counts = {}
        for state in receipts.values():
            counts[state] = counts.get(state, 0)+1
        jobs.append({'job_id': m['job_id'], 'mode': m.get('mode'),
                     'status': m['status'], 'analysis_complete': m.get('analysis_complete'),
                     'input_count': m.get('input_count'),
                     'deferred_count': len(m['deferred_ids']) if 'deferred_ids' in m else None,
                     'receipt_states': counts, 'history_saved': m.get('history_saved'),
                     'ack': m.get('ack'), 'paid_ai_calls': m.get('paid_ai_calls'),
                     'metadata_sha256': digest(m), 'source': entry['source']['path']})
    return {'observed_at': view['observed_at'], 'observation_only': True,
            'full_metadata_required_for_decisions': True, 'jobs': jobs}


def usage_observation(*, stage, engine, measurement_source, usage=None,
                      elapsed_ms=None, attempt_number=None, retry_reason=None):
    """Preserve absent counters as unknown. Never infer platform usage from API0.

    Anthropic input_tokens excludes cache reads/writes. Cache counters are only
    populated when explicitly provided; unavailable is not a measured zero.
    """
    if measurement_source not in {'provider_adaptation', 'anthropic_response',
                                   'subscription_platform', 'offline_replay'}:
        raise ValueError('Unknown measurement source')
    fields = ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
              'cache_creation_input_tokens')
    if usage is not None and not isinstance(usage, dict):
        raise ValueError('Usage must be explicit counters or unknown')
    counters = {name: (usage or {}).get(name) for name in fields}
    for value in counters.values():
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('Invalid measured counter')
    if measurement_source == 'provider_adaptation' and usage is not None:
        raise ValueError('Provider adaptation does not expose model token usage')
    for value in (elapsed_ms, attempt_number):
        if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
            raise ValueError('Invalid timing/attempt counter')
    return {'stage': stage, 'engine': engine,
            'measurement_source': measurement_source, 'tokens': counters,
            'elapsed_ms': elapsed_ms, 'attempt_number': attempt_number,
            'retry_reason': retry_reason,
            'model_call_count': None,
            'paid_ai_calls': 0 if measurement_source == 'provider_adaptation' else None,
            'cost': None, 'cost_currency': None}


if __name__ == '__main__':
    # Only an already downloaded local snapshot; no implicit live credentials.
    import argparse
    from pathlib import Path
    from risk_analysis_guard import strict_json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', help='private JSON: index, jobs, observed_at')
    parser.add_argument('--output', required=True, help='new private local status artifact (not an operational result)')
    args = parser.parse_args()
    snapshot = strict_json(Path(args.snapshot).read_text())
    class LocalSnapshot:
        def get(self, path):
            if path == 'index.json':
                return snapshot['index'], snapshot.get('index_sha') or digest(snapshot['index'])
            record = snapshot['jobs'].get(path.removeprefix('jobs/').removesuffix('.json'))
            return (record['value'], record['blob_sha']) if record else (None, None)
    view = queue_status(LocalSnapshot(), snapshot['observed_at'])
    content = canonical(view)
    import os
    with Path(args.output).open('x', encoding='utf-8') as file:
        os.chmod(file.name, 0o600)
        file.write(content)
    index = status_index(view)
    index['artifact'] = str(Path(args.output).resolve())
    index['artifact_sha256'] = hashlib.sha256(content.encode()).hexdigest()
    print(canonical(index))
