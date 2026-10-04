// Package gateway implements the prepaid OTLP/HTTP entry point. Each process
// belongs to one subscription; routing never comes from telemetry attributes.
package gateway

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"sort"
	"time"

	"github.com/SigNoz/signoz/pkg/abera/namespace"
)

const MaxPayload = 4 << 20
const MaxQueuedBytes = 32 << 20

type Plan struct {
	LogTraceBytes        int64 `json:"logTraceBytes"`
	MetricSamples        int64 `json:"metricSamples"`
	ActiveSeries         int64 `json:"activeSeries"`
	IngestBytesPerSecond int64 `json:"ingestBytesPerSecond"`
	BurstBytes           int64 `json:"burstBytes"`
	StorageBytes         int64 `json:"storageBytes"`
	RetentionDays        int64 `json:"retentionDays"`
}

type Term struct {
	CycleID  string `json:"cycleId"`
	StartsAt int64  `json:"startsAt"`
	EndsAt   int64  `json:"endsAt"`
	Plan     string `json:"plan"`
}

// Config is written atomically by the host controller on a private bind mount.
// Storage observations expire, so a failed controller cannot grant endless ingest.
type Config struct {
	SubscriptionID    string `json:"subscriptionId"`
	Namespace         string `json:"namespace"`
	Revision          int64  `json:"revision"`
	State             string `json:"state"`
	Ready             bool   `json:"ready"`
	Terms             []Term `json:"terms"`
	TokenSHA256       string `json:"tokenSHA256"`
	AppURL            string `json:"appURL"`
	CollectorURL      string `json:"collectorURL"`
	StorageBytes      int64  `json:"storageBytes"`
	StorageObservedAt int64  `json:"storageObservedAt"`
	DiskHealthy       bool   `json:"diskHealthy"`
}

func ReadConfig(path string, plans map[string]Plan) (Config, error) {
	var c Config
	b, err := os.ReadFile(path)
	if err != nil {
		return c, err
	}
	if len(b) > 65536 {
		return c, fmt.Errorf("configuration exceeds size limit")
	}
	if err = json.Unmarshal(b, &c); err != nil {
		return c, err
	}
	return c, c.Validate(plans)
}

func (c Config) Validate(plans map[string]Plan) error {
	if c.SubscriptionID == "" || c.Namespace == "" || c.Revision < 1 {
		return fmt.Errorf("missing identity or revision")
	}
	if _, err := namespace.New(c.Namespace); err != nil {
		return err
	}
	if c.State != "ACTIVE" && c.State != "SUSPENDED" && c.State != "ARCHIVED" && c.State != "DELETED" {
		return fmt.Errorf("invalid lifecycle state")
	}
	if token, err := hex.DecodeString(c.TokenSHA256); err != nil || len(token) != 32 {
		return fmt.Errorf("invalid token hash")
	}
	for _, address := range []string{c.AppURL, c.CollectorURL} {
		u, err := url.Parse(address)
		if err != nil || u.Scheme != "http" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
			return fmt.Errorf("invalid internal service URL")
		}
	}
	if len(c.Terms) > 24 {
		return fmt.Errorf("too many prepaid terms")
	}
	terms := append([]Term(nil), c.Terms...)
	sort.Slice(terms, func(i, j int) bool { return terms[i].StartsAt < terms[j].StartsAt })
	seen := map[string]bool{}
	for i, term := range terms {
		if len(term.CycleID) == 0 || len(term.CycleID) > 100 || seen[term.CycleID] || term.EndsAt <= term.StartsAt || term.EndsAt-term.StartsAt > 32*86400 {
			return fmt.Errorf("invalid prepaid term")
		}
		if _, ok := plans[term.Plan]; !ok {
			return fmt.Errorf("unavailable plan")
		}
		if i > 0 && terms[i-1].EndsAt > term.StartsAt {
			return fmt.Errorf("overlapping prepaid terms")
		}
		seen[term.CycleID] = true
	}
	return nil
}

func (c Config) Current(now time.Time) (Term, bool) {
	for _, term := range c.Terms {
		if term.StartsAt <= now.Unix() && now.Unix() < term.EndsAt {
			return term, true
		}
	}
	return Term{}, false
}

func LoadPlans(path string) (map[string]Plan, error) {
	var manifest struct {
		Plans map[string]Plan `json:"plans"`
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if err = json.Unmarshal(b, &manifest); err != nil {
		return nil, err
	}
	for _, p := range manifest.Plans {
		if p.LogTraceBytes <= 0 || p.MetricSamples <= 0 || p.ActiveSeries <= 0 || p.IngestBytesPerSecond <= 0 || p.BurstBytes <= 0 || p.StorageBytes <= 0 || p.RetentionDays <= 0 {
			return nil, fmt.Errorf("invalid plan limits")
		}
	}
	if len(manifest.Plans) == 0 {
		return nil, fmt.Errorf("empty plan catalogue")
	}
	return manifest.Plans, nil
}
