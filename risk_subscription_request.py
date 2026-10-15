"""Public wake file contains metadata only; fixed CLI operations and flags."""
import json
import subprocess
import sys
from pathlib import Path
from risk_queue import JOB_ID


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
    process = subprocess.run(command, check=True, capture_output=True, text=True)
    print(process.stdout, end='')
    summary = json.loads(process.stdout.strip().splitlines()[-1])
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
