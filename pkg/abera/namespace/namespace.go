// Package namespace maps the upstream telemetry schema into one customer's
// databases. Each Community process serves exactly one namespace. ClickHouse
// grants, not name mapping, enforce the security boundary.
package namespace

import (
	"fmt"
	"os"
	"regexp"
	"strings"
)

var valid = regexp.MustCompile(`^abera_[a-f0-9]{20}$`)

var logicalDatabases = []string{
	"signoz_logs", "signoz_traces", "signoz_metrics", "signoz_metadata",
	"signoz_analytics", "signoz_meter", "signoz_audit",
}

type Mapper struct {
	replacer *strings.Replacer
	prefix   string
}

// New accepts an empty prefix for upstream-compatible, unmanaged installations.
func New(prefix string) (Mapper, error) {
	if prefix == "" {
		return Mapper{}, nil
	}
	if !valid.MatchString(prefix) {
		return Mapper{}, fmt.Errorf("invalid Abera telemetry namespace: expected abera_ followed by 20 hexadecimal characters")
	}
	pairs := make([]string, 0, len(logicalDatabases)*2)
	for _, logical := range logicalDatabases {
		pairs = append(pairs, logical, prefix+strings.TrimPrefix(logical, "signoz"))
	}
	return Mapper{replacer: strings.NewReplacer(pairs...), prefix: prefix}, nil
}

// Resolve maps trusted schema names and SQL templates from the source code.
// It must never be applied to user-authored SQL, filter values or telemetry.
func (m Mapper) Resolve(template string) string {
	if m.replacer == nil {
		return template
	}
	return m.replacer.Replace(template)
}

var process = fromEnvironment()

func fromEnvironment() Mapper {
	prefix := os.Getenv("ABERA_TELEMETRY_NAMESPACE")
	if prefix == "" && os.Getenv("ABERA_REQUIRE_NAMESPACE") == "true" {
		panic("ABERA_TELEMETRY_NAMESPACE is required for a managed Abera process")
	}
	m, err := New(prefix)
	if err != nil {
		panic(err)
	}
	return m
}

// Resolve uses the immutable namespace selected before process startup.
func Resolve(template string) string { return process.Resolve(template) }

func Managed() bool { return process.prefix != "" }

// DataCluster prevents Distributed tables from retaining migration credentials.
// DDL uses the administrator cluster; data reads/writes use a scoped user.
func DataCluster(fallback string) string {
	if process.prefix == "" {
		return fallback
	}
	return process.prefix + "_data"
}
