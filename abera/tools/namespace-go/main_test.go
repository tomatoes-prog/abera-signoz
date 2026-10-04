package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func fixture(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for name, body := range files {
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func read(t *testing.T, root, path string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(root, path))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestRewriteCompilesAndIsIdempotent(t *testing.T) {
	root := fixture(t, map[string]string{
		"go.mod": "module fixture\n\ngo 1.22\n",
		"internal/aberanamespace/map.go": `package aberanamespace
func Resolve(s string) string { return s }`,
		"schema/schema.go": `package schema
// signoz_logs is a database; do not rewrite comments.
const ( DB = "signoz_logs"; Table = DB + ".logs" )`,
		"query/query.go": `package query
import "fixture/schema"
const Query = "SELECT * FROM " + schema.Table
func Local() string { const db = "signoz_metrics"; return db }
func UserSQL(sql string) string { return sql }
`,
		"query/query_test.go": `package query
import "testing"
func TestQuery(t *testing.T) { if Query != "SELECT * FROM signoz_logs.logs" { t.Fatal(Query) } }`,
	})
	helper := "fixture/internal/aberanamespace"
	if err := rewrite(root, "fixture", helper); err != nil {
		t.Fatal(err)
	}
	first := read(t, root, "query/query.go")
	if !strings.Contains(first, "var Query") || !strings.Contains(first, `return sql`) {
		t.Fatal(first)
	}
	if !strings.Contains(read(t, root, "schema/schema.go"), "// signoz_logs is a database") {
		t.Fatal("changed comment")
	}
	if err := rewrite(root, "fixture", helper); err != nil {
		t.Fatal(err)
	}
	if first != read(t, root, "query/query.go") {
		t.Fatal("second run changed source")
	}
	cmd := exec.Command("go", "test", "./...")
	cmd.Dir = root
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
}

func TestRejectsIotaWithoutWritingPartialChanges(t *testing.T) {
	code := "package p\nconst ( N = iota; DB = \"signoz_traces\" )\n"
	root := fixture(t, map[string]string{"p/p.go": code})
	if err := rewrite(root, "fixture", "fixture/internal/aberanamespace"); err == nil {
		t.Fatal("expected manual split")
	}
	if read(t, root, "p/p.go") != code {
		t.Fatal("wrote partial changes")
	}
}

func TestCollectorDataClusterAndThreadOnlyPatchCompile(t *testing.T) {
	module := "fixture/signoz-otel-collector"
	root := fixture(t, map[string]string{
		"go.mod":                         "module " + module + "\n\ngo 1.22\n",
		"internal/aberanamespace/map.go": "package aberanamespace\nfunc DataCluster(s string) string { return s }\nfunc Resolve(s string) string { return s }\n",
		"schema_migrator/table.go":       "package schema_migrator\ntype Distributed struct { Cluster string }\nfunc (d Distributed) ToSQL() string { return d.Cluster }\n",
		"query/query.go":                 "package query\nconst SQL = `SELECT 1 SETTINGS max_threads = 8`\n",
	})
	for i := 0; i < 2; i++ {
		if err := rewrite(root, module, module+"/internal/aberanamespace"); err != nil {
			t.Fatal(err)
		}
	}
	if code := read(t, root, "schema_migrator/table.go"); strings.Count(code, "aberanamespace.DataCluster(d.Cluster)") != 1 {
		t.Fatal(code)
	}
	if code := read(t, root, "query/query.go"); strings.Contains(code, "aberanamespace") || !strings.Contains(code, "max_threads = 1") {
		t.Fatal(code)
	}
	cmd := exec.Command("go", "test", "./...")
	cmd.Dir = root
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
}
