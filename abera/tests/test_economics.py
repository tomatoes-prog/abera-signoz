from abera.tools.economics import estimate
from abera.tools.readiness import check


def test_ceiling_alone_does_not_make_four_lite_customers_profitable():
    base = estimate(['lite']*4,0)
    assert round(base['baseInfrastructureCop'],2) == 238393.60
    at_ceiling = estimate(['lite']*4,450000-base['baseInfrastructureCop'])
    assert not at_ceiling['withinCeilingAndMargin']
    assert at_ceiling['operatingMargin'] < 0
    assert estimate(['lite']*4,60000)['withinCeilingAndMargin']
    assert not estimate(['lite'],60000)['withinCeilingAndMargin']


def test_local_smoke_cannot_approve_arm_admissions():
    report = check({'plans':['lite']*4,'additionalCostsCop':60000},
                   {'result':'PASS','architecture':'x86_64','durationSeconds':180,'customers':4,'drained':True})
    assert not report['readyForDevAdmissions'] and not report['productionEnabled']
    assert '72 hours of ARM workload with four customers' in report['missing']
