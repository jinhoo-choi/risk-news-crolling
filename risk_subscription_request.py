"""Public wake file contains metadata only; fixed CLI operations and flags."""
import json
import re
import subprocess
import sys
from pathlib import Path
from risk_queue import JOB_ID


def read_summary(stdout):
    summaries = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and {'job_id', 'status', 'paid_ai_calls'} <= value.keys():
            summaries.append(value)
    if len(summaries) != 1:
        raise RuntimeError('소비 결과 요약 누락·중복')
    return summaries[0]


def main():
    value = json.loads(Path('risk_subscription_request.json').read_text())
    if set(value) != {'operation','job_id','self_test','dry_run','request_id'}:
        raise RuntimeError('소비 메타데이터 필드 오류')
    if value['operation'] not in {'consume', 'recover', 'collect-review'} or not JOB_ID.fullmatch(value['job_id']) or type(value['self_test']) is not bool or value['dry_run'] is not False:
        raise RuntimeError('허용되지 않은 소비 요청')
    if value['operation'] == 'collect-review' and (value['job_id'] != 'pending-review' or value['self_test']):
        raise RuntimeError('보류 재수집 요청 범위 오류')
    if value['operation'] == 'recover' and value['self_test']:
        raise RuntimeError('복구는 운영 회차만 허용')
    if not JOB_ID.fullmatch(value['request_id']):
        raise RuntimeError('요청 ID 오류')
    command = [sys.executable, 'risk_subscription.py', value['operation']]
    if value['operation'] != 'collect-review':
        command += ['--job-id', value['job_id']]
    if value['self_test']:
        command.append('--self-test')
    try:
        process = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        # Child stdout/stderr may contain private articles. Expose only fixed
        # outbox phase/result metadata, never the captured traceback or payload.
        for line in (error.stdout or '').splitlines():
            if re.fullmatch(r'RISK_OUTBOX_VERIFY (?:phase=put outcome=unconfirmed|'
                            r'phase=read source=(?:put_commit|fresh) attempt=[1-3] '
                            r'outcome=(?:verified|missing|mismatch|unavailable|blocked) '
                            r'commit=(?:[0-9a-f]{40}|unconfirmed) http=(?:0|[1-5][0-9]{2}) '
                            r'request_id=[A-Za-z0-9:-]{1,100})', line):
                print(line)
        raise
    print(process.stdout, end='')
    summary = read_summary(process.stdout)
    if value['operation'] == 'collect-review':
        if (summary.get('status') != 'pending' or summary.get('paid_ai_calls') != 0
                or not JOB_ID.fullmatch(summary.get('job_id', ''))):
            raise RuntimeError('보류 재수집 결과 확인 실패')
        return
    if (summary.get('job_id') != value['job_id'] or summary.get('paid_ai_calls') != 0
            or summary.get('status') not in {'verified', 'completed', 'delivery_complete'}):
        raise RuntimeError('소비 결과 완료 상태 확인 실패')
    if output := __import__('os').environ.get('GITHUB_OUTPUT'):
        with open(output, 'a') as stream:
            stream.write('job_id='+value['job_id']+'\n')
            stream.write('self_test='+str(value['self_test']).lower()+'\n')
            stream.write('needs_ack='+str(summary['status'] == 'delivery_complete').lower()+'\n')


if __name__ == '__main__':
    main()
