package namespace

import (
	"strings"
	"testing"
)

func TestDatabasesAndKeeperPathsUseIndependentNamespaces(t *testing.T) {
	a, err := New("abera_00000000000000000001")
	if err != nil {
		t.Fatal(err)
	}
	b, err := New("abera_00000000000000000002")
	if err != nil {
		t.Fatal(err)
	}
	for _, db := range logicalDatabases {
		for _, value := range []string{db, "SELECT * FROM " + db + ".logs WHERE body = ?", "/clickhouse/tables/" + db + "/logs/{shard}"} {
			if a.Resolve(value) == b.Resolve(value) || strings.Contains(a.Resolve(value), "signoz_") {
				t.Fatalf("namespace escaped mapping: %s", value)
			}
		}
	}
}

func TestUnmanagedKeepsUpstreamSchema(t *testing.T) {
	m, err := New("")
	if err != nil {
		t.Fatal(err)
	}
	for _, db := range logicalDatabases {
		if m.Resolve(db) != db {
			t.Fatal("changed upstream default")
		}
	}
}

func TestNamespaceRejectsCustomerControlledSQLAndPaths(t *testing.T) {
	for _, prefix := range []string{"tenant-a", "abera_123", "abera_00000000000000000001; DROP DATABASE x", "../signoz_logs", "abera_ABCDEF12345678900000"} {
		if _, err := New(prefix); err == nil {
			t.Fatalf("accepted %q", prefix)
		}
	}
}
