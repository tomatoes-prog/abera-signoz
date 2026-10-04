"""Transparent 31-day cost scenario. Inputs are assumptions, not an AWS invoice."""
import argparse
import json
from decimal import Decimal as D
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from abera.runtime.model import ABERA, read_json


def estimate(plans, additional_cop, fx=None):
    release, catalog = read_json(ABERA/'release.json'),read_json(ABERA/'plans.json')
    econ = release['economics']
    if not 0 <= len(plans) <= 4 or any(p not in catalog['plans'] for p in plans):
        raise ValueError('only four Lite/Essential slots are available')
    fx = D(str(fx if fx is not None else econ['copPerUsdScenario']))
    additional = D(str(additional_cop))
    if fx <= 0 or additional < 0: raise ValueError('invalid cost scenario')
    hours = D(catalog['cycleDays']*24)
    base = (hours*(D(econ['ec2UsdPerHour'])+D(econ['ipv4UsdPerHour']))+
            D(econ['dataVolumeGiB']+econ['rootVolumeGiB'])*D(econ['ebsUsdPerGiBMonth']))*fx
    revenue = D(sum(catalog['plans'][p]['priceBeforeTax'] for p in plans))
    total = base+additional
    allowable = min(D(econ['pilotCeilingCop']),revenue*(1-D(econ['minimumOperatingMargin'])))
    return {'periodDays':catalog['cycleDays'],'plans':plans,'copPerUsdScenario':float(fx),'baseInfrastructureCop':float(base),
            'additionalCostsCop':float(additional),'totalCostCop':float(total),'revenueBeforeTaxCop':float(revenue),
            'operatingMargin':float((revenue-total)/revenue) if revenue else None,
            'remainingBudgetForAdditionalCostsCop':float(allowable-base),'withinCeilingAndMargin':total <= allowable,
            'excludedUnlessIncludedInAdditionalCosts':['S3 and archived customers','ECR','DynamoDB/KMS/Lambda/CloudWatch/Secrets',
                'shared ALB/DNS/control plane','data transfer','payment fees','taxes and FX spread','support and labor'],
            'warning':'Scenario only. Validate occupancy, measured usage and all additional costs before opening sales.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plans',nargs='*',default=['lite']*4)
    parser.add_argument('--additional-cost-cop',type=D,required=True)
    parser.add_argument('--cop-per-usd',type=D)
    args=parser.parse_args()
    print(json.dumps(estimate(args.plans,args.additional_cost_cop,args.cop_per_usd),indent=2))
