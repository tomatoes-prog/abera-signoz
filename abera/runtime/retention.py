"""Change only the pinned schema's table TTLs, never customer SQL or attributes."""
import re

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
INTERVAL = re.compile(r"toInterval(Second|Day|Month|Year)\(([0-9]+|_retention_days)\)")
SECONDS = {"Second": 1, "Day": 86400, "Month": 31 * 86400, "Year": 366 * 86400}
# These are deliberately shorter housekeeping windows in collector v0.144.6.
# All other TTL tables follow the purchased plan, including after an upgrade.
SHORT_TTLS = {("logs", "usage"): 259200, ("metrics", "usage"): 259200,
              ("traces", "usage"): 259200, ("traces", "span_attributes"): 172800,
              ("metrics", "samples_v4_buffer"): 90000, ("metrics", "time_series_v4_buffer"): 90000}


def retention_changes(tables, namespace, days):
    if not re.fullmatch(r"abera_[a-f0-9]{20}", namespace) or days not in {7, 15}:
        raise ValueError("invalid retention target")
    statements = []
    primary_tables = {"logs_v2", "signoz_index_v3", "samples_v4", "exp_hist"}
    found = set()
    for table in tables:
        database, name, ddl = table["database"], table["name"], table["create_table_query"]
        if not database.startswith(namespace + "_") or not IDENTIFIER.fullmatch(database) or not IDENTIFIER.fullmatch(name):
            raise ValueError("table outside the assigned namespace")
        if not table["engine"].endswith("MergeTree"):
            continue
        if " TTL " not in ddl:
            if name in primary_tables:
                raise ValueError("primary telemetry table has no retention TTL")
            continue
        ttl = ddl.split(" TTL ", 1)[1].split(" SETTINGS ", 1)[0]
        match = INTERVAL.search(ttl)
        if not match:
            raise ValueError(f"unrecognized TTL in {name}; release needs review")
        suffix = database[len(namespace) + 1:]
        seconds = min(days * 86400, SHORT_TTLS.get((suffix, name), days * 86400))
        updated = ttl[:match.start()] + f"toIntervalSecond({seconds})" + ttl[match.end():]
        if name in primary_tables:
            found.add(name)
        if ttl != updated:
            statements.append(f"ALTER TABLE {database}.{name} MODIFY TTL {updated} SETTINGS materialize_ttl_after_modify=0")
    if found != primary_tables:
        raise ValueError("required telemetry tables were not migrated")
    return statements
