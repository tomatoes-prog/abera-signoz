"""Check supplied DEV evidence. Does not enable sales or modify AWS."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from abera.runtime.model import read_json
from abera.tools.economics import estimate

REQUIRED = ('armImagesVerified','awsVersionedRestoreVerified','hostRebootVerified',
    'hostLossRecoveryWithinFourHours','billingEndToEndVerified','isolationAndNoisyNeighborVerified',
    'storageNearQuotaAndBackupVerified','vulnerabilityAndLicenseReviewComplete',
    'taxClassificationReviewed','additionalCostsMeasured',
    'billingOnDemandHostVerified','fifthCustomerNewHostVerified','emptyHostAndVolumeRetiredVerified',
    'suspendedSubscriptionRetainedVerified','backupExpiryWithoutHostVerified')


def check(acceptance, benchmark):
    missing = [name for name in REQUIRED if acceptance.get(name) is not True]
    if acceptance.get('environment') != 'dev' or not re.fullmatch(r'[0-9]{12}',acceptance.get('awsAccountId','')):
        missing.append('confirmed development account')
    if acceptance.get('instanceType') != 'r7g.medium': missing.append('budgeted instance type')
    if (benchmark.get('result') != 'PASS' or benchmark.get('customers') != 4
        or benchmark.get('architecture') not in {'aarch64','arm64'} or benchmark.get('durationSeconds',0) < 72*3600
        or benchmark.get('secondsRequested',0) < 72*3600 or not benchmark.get('drained')):
        missing.append('72 hours of ARM workload with four customers')
    if any(c.get('oomKilled') or c.get('restarts',0) for c in benchmark.get('containerState',[])):
        missing.append('stable containers')
    for key, limit in [('ingestLatencyMs',1000),('queryLatencyMs',2500)]:
        if benchmark.get(key,{}).get('p95') is None or benchmark[key]['p95'] > limit:
            missing.append(key+' p95')
    cost = estimate(acceptance.get('plans',[]),acceptance.get('additionalCostsCop',0),acceptance.get('copPerUsdScenario',4000))
    if not cost['withinCeilingAndMargin']: missing.append('budget and operating margin')
    return {'readyForDevAdmissions':not missing,'missing':missing,'costScenario':cost,'productionEnabled':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--acceptance',type=Path,required=True); p.add_argument('--benchmark',type=Path,required=True)
    a=p.parse_args(); result=check(read_json(a.acceptance),read_json(a.benchmark))
    print(json.dumps(result,indent=2)); raise SystemExit(0 if result['readyForDevAdmissions'] else 1)
