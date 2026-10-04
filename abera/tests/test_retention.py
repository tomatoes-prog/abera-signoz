import pytest
from abera.runtime.retention import retention_changes

NS = "abera_" + "a" * 20


def tables(days):
    return [{"database": NS + "_" + signal, "name": name, "engine": "MergeTree",
             "create_table_query": f"CREATE TABLE t (x DateTime) ENGINE=MergeTree ORDER BY x TTL x + toIntervalDay({days}) SETTINGS index_granularity=8192"}
            for signal, name in [("logs", "logs_v2"), ("traces", "signoz_index_v3"),
                                 ("metrics", "samples_v4"), ("metrics", "exp_hist")]]


def test_upgrade_extends_retention_and_downgrade_caps_it():
    assert all("1296000" in q for q in retention_changes(tables(7), NS, 15))
    assert all("604800" in q for q in retention_changes(tables(15), NS, 7))


def test_short_housekeeping_and_offset_are_preserved():
    rows = tables(15)
    rows += [{**rows[2], "name": "samples_v4_buffer"},
             {**rows[0], "name": "logs_v2_resource", "create_table_query": "CREATE TABLE t TTL (x + toIntervalDay(_retention_days)) + toIntervalSecond(1800) SETTINGS a=1"}]
    statements = retention_changes(rows, NS, 7)
    assert "toIntervalSecond(90000)" in statements[-2]
    assert "toIntervalSecond(604800)) + toIntervalSecond(1800)" in statements[-1]


def test_incomplete_or_foreign_schema_fails_closed():
    with pytest.raises(ValueError):
        retention_changes(tables(7)[:2], NS, 7)
    rows = tables(7)
    rows[0]["database"] = "abera_" + "b" * 20 + "_logs"
    with pytest.raises(ValueError):
        retention_changes(rows, NS, 7)
