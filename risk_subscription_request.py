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
    if value['operation'] != 'consume' or not JOB_ID.fullmatch(value['job_id']) or type(value['self_test']) is not bool or value['dry_run'] is not False:
        raise RuntimeError('허용되지 않은 소비 요청')
    if not JOB_ID.fullmatch(value['request_id']):
        raise RuntimeError('요청 ID 오류')
    command = [sys.executable, 'risk_subscription.py', 'consume', '--job-id', value['job_id']]
    if value['self_test']:
        command.append('--self-test')
    subprocess.run(command, check=True)
    if output := __import__('os').environ.get('GITHUB_OUTPUT'):
        with open(output, 'a') as stream:
            stream.write('job_id='+value['job_id']+'\n')
            stream.write('self_test='+str(value['self_test']).lower()+'\n')


if __name__ == '__main__':
    main()
