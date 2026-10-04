"""Repeatable DEV workload for four synthetic tenants; never a production probe."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from abera.runtime.host import Host, RuntimeFailure, api
from abera.runtime.model import ABERA, write_json


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)] if values else None


def run(root, seconds, interval):
    host = Host(root)
    state = host.state
    tenants = state['tenants']
    if (not state.get('local') and os.environ.get('ENVIRONMENT') != 'dev') or len(tenants) != 4 or any(
        not t['subscriptionId'].startswith('development-') for t in tenants
    ):
        raise RuntimeFailure('benchmark requires exactly four synthetic DEV tenants')
    marker = 'load_' + secrets.token_hex(8)
    report = {'schemaVersion': 1, 'kind': 'development-workload', 'startedAt': int(time.time()),
              'secondsRequested': seconds, 'customers': 4, 'cpuPinned': state.get('pinCPU', False),
              'architecture': subprocess.check_output(['docker','info','--format','{{.Architecture}}'], text=True).strip(),
              'ingestLatencyMs': [], 'queryLatencyMs': [], 'errors': [], 'containerSamples': [], 'rounds': 0}
    sessions = {}
    host.observe()
    for t in tenants:
        base = f"http://127.0.0.1:{24800+t['slot']}"
        sessions[t['slot']] = api(base, 'POST', '/api/v2/sessions/email_password',
            {'email': t['adminEmail'], 'password': t['adminPassword'], 'orgId': t['orgId']})['accessToken']
    def workload(t):
        now = time.time_ns()
        resource = {'attributes': [{'key':'service.name', 'value':{'stringValue':marker}}]}
        bodies = {
            'logs': {'resourceLogs':[{'resource':resource,'scopeLogs':[{'logRecords':[
                {'timeUnixNano':str(now+i), 'body':{'stringValue':marker+' '+('x'*240)}} for i in range(50)]}]}]},
            'traces': {'resourceSpans':[{'resource':resource,'scopeSpans':[{'spans':[
                {'traceId':secrets.token_hex(16),'spanId':secrets.token_hex(8),'name':marker,'kind':2,
                 'startTimeUnixNano':str(now+i),'endTimeUnixNano':str(now+i+1_000_000)} for i in range(10)]}]}]},
            'metrics': {'resourceMetrics':[{'resource':resource,'scopeMetrics':[{'metrics':[
                {'name':marker,'gauge':{'dataPoints':[{'timeUnixNano':str(now),'asDouble':float(i),
                    'attributes':[{'key':'series','value':{'intValue':str(i)}}]} for i in range(40)]}}]}]}]},
        }
        base = f"http://127.0.0.1:{24800+t['slot']}"
        ingest = []
        for signal, body in bodies.items():
            start = time.monotonic()
            api(base,'POST','/v1/'+signal,body,t['otlpToken'])
            ingest.append((time.monotonic()-start)*1000)
        start = time.monotonic()
        api(base,'POST','/api/v5/query_range',{'schemaVersion':'v1','start':int(time.time()*1000)-300000,
            'end':int(time.time()*1000),'requestType':'time_series','compositeQuery':{'queries':[
                {'type':'builder_query','spec':{'name':'A','signal':'logs','stepInterval':60,
                 'aggregations':[{'expression':'count()'}]}}]},'noCache':True},sessions[t['slot']])
        return ingest, (time.monotonic()-start)*1000
    start = time.monotonic()
    next_observe = 0
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            while time.monotonic()-start < seconds:
                cycle = time.monotonic()
                if cycle >= next_observe:
                    host.observe()
                    ids = host.compose('ps','-q').stdout.split()
                    stats = subprocess.run(['docker','stats','--no-stream','--format','{{json .}}',*ids],capture_output=True,text=True,check=True)
                    report['containerSamples'].append({'at':int(time.time()),'containers':[json.loads(line) for line in stats.stdout.splitlines()]})
                    next_observe = cycle+25
                futures = [pool.submit(workload,t) for t in tenants]
                for t,future in zip(tenants,futures):
                    try:
                        ingest, query = future.result()
                        report['ingestLatencyMs'].extend(ingest)
                        report['queryLatencyMs'].append(query)
                    except Exception as exc:
                        report['errors'].append({'slot':t['slot'],'type':type(exc).__name__})
                report['rounds'] += 1
                if report['errors']:
                    break
                time.sleep(max(0,min(interval-(time.monotonic()-cycle),seconds-(time.monotonic()-start))))
        report['durationSeconds'] = round(time.monotonic()-start,2)
        report['drained'] = False
        deadline = time.monotonic()+120
        while time.monotonic() < deadline:
            host.observe()
            usage = [api(f"http://127.0.0.1:{24800+t['slot']}",'GET','/abera/usage',token=t['otlpToken'])['usage'] for t in tenants]
            if all(u['queuedBytes']==0 and u['batchesNeedingAttention']==0 for u in usage):
                report['drained'] = True
                break
            time.sleep(2)
        report['finalUsage'] = usage
        report['delivery'] = []
        for t in tenants:
            for signal,table,column,per_round in [('logs','logs_v2','body',50),('traces','signoz_index_v3','name',10),('metrics','samples_v4','metric_name',40)]:
                count = int(host.sql(f"SELECT count() FROM {t['namespace']}_{signal}.{table} WHERE startsWith({column},'{marker}')"))
                report['delivery'].append({'slot':t['slot'],'signal':signal,'rows':count,'expected':report['rounds']*per_round})
        ids = host.compose('ps','-q').stdout.split()
        report['containerState'] = [json.loads(subprocess.check_output(['docker','inspect','--format',
            '{"oomKilled":{{.State.OOMKilled}},"restarts":{{.RestartCount}}}',cid],text=True)) for cid in ids]
        for key in ('ingestLatencyMs','queryLatencyMs'):
            values = report.pop(key)
            report[key] = {'count':len(values),'p95':percentile(values,.95),'max':max(values) if values else None}
        report['result'] = 'PASS' if not report['errors'] and report['drained'] and all(d['rows'] >= d['expected'] for d in report['delivery']) and not any(c['oomKilled'] for c in report['containerState']) else 'FAIL'
    finally:
        for t in tenants:
            try: api(f"http://127.0.0.1:{24800+t['slot']}",'DELETE','/api/v2/sessions',token=sessions[t['slot']])
            except RuntimeFailure: pass
        write_json(ABERA/'results/benchmark.json',report)
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ABERA/'.runtime/dev')
    parser.add_argument('--seconds',type=int,default=180)
    parser.add_argument('--interval',type=float,default=5)
    args=parser.parse_args()
    if args.seconds < 10 or args.interval < 1: parser.error('minimum 10 seconds and 1 second interval')
    result=run(args.root,args.seconds,args.interval)
    print(json.dumps({k:result[k] for k in ('result','durationSeconds','architecture','rounds','ingestLatencyMs','queryLatencyMs','errors')}))
    raise SystemExit(0 if result['result']=='PASS' else 1)
