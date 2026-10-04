package gateway

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"go.opentelemetry.io/collector/pdata/plog/plogotlp"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestHealthChecksBothDependenciesAndSourceOfferIsPublic(t *testing.T) {
	s, _, _ := testServer(t)
	calls := 0
	broken := false
	s.client.Transport = roundTripFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		status := 200
		if broken && r.URL.Port() == "13133" {
			status = 503
		}
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader("{}")), Header: http.Header{}}, nil
	})
	for _, expected := range []int{200, 503} {
		w := httptest.NewRecorder()
		s.ServeHTTP(w, httptest.NewRequest("GET", "/abera/health", nil))
		if w.Code != expected {
			t.Fatalf("health=%d, want %d", w.Code, expected)
		}
		broken = true
	}
	if calls != 4 {
		t.Fatalf("dependency checks=%d", calls)
	}
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest("GET", "/abera/licenses", nil))
	if w.Code != 200 || !strings.Contains(w.Body.String(), "/abera/source.tar.gz") {
		t.Fatal("missing public source offer")
	}
}

func TestMetricSeriesIncludesScopeTemporalityAndMonotonicity(t *testing.T) {
	base := `{"resourceMetrics":[{"scopeMetrics":[{"schemaUrl":"scope-a","metrics":[{"name":"count","sum":{"aggregationTemporality":1,"isMonotonic":true,"dataPoints":[{"asInt":"1"}]}}]}]}]}`
	seen := map[string]bool{}
	for _, payload := range []string{base, strings.Replace(base, "scope-a", "scope-b", 1), strings.Replace(base, `Temporality":1`, `Temporality":2`, 1), strings.Replace(base, `Monotonic":true`, `Monotonic":false`, 1)} {
		batch, err := Measure("metrics", []byte(payload), true)
		if err != nil || batch.MetricSamples != 1 || len(batch.Series) != 1 {
			t.Fatalf("%+v %v", batch, err)
		}
		if seen[batch.Series[0]] {
			t.Fatal("different metric identities shared a series counter")
		}
		seen[batch.Series[0]] = true
	}
}

var testNow = time.Unix(1700000000, 0)

const testToken = "test-secret-with-at-least-thirty-two-characters"

func testConfig() Config {
	h := sha256.Sum256([]byte(testToken))
	return Config{SubscriptionID: "customer-a", Namespace: "abera_00000000000000000001", Revision: 1, State: "ACTIVE", Ready: true,
		TokenSHA256: hex.EncodeToString(h[:]), AppURL: "http://app:8080", CollectorURL: "http://collector:4318", DiskHealthy: true, StorageObservedAt: testNow.Unix(),
		Terms: []Term{{CycleID: "paid-1", StartsAt: testNow.Unix() - 100, EndsAt: testNow.Unix() + 100, Plan: "lite"}}}
}
func testPlan() Plan {
	return Plan{LogTraceBytes: 10000, MetricSamples: 10, ActiveSeries: 10, IngestBytesPerSecond: 1000000, BurstBytes: 1000000, StorageBytes: 1000000, RetentionDays: 7}
}
func openTestLedger(t *testing.T) *Ledger {
	t.Helper()
	l, err := OpenLedger(filepath.Join(t.TempDir(), "usage.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	if err = l.Sync(testConfig()); err != nil {
		t.Fatal(err)
	}
	return l
}
func usage(t *testing.T, l *Ledger, c Config, p Plan) Usage {
	t.Helper()
	u, err := l.Usage(c.Terms[0], p, testNow)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

func TestConcurrentQuotaIsAtomicAndRejectionDoesNotCharge(t *testing.T) {
	l := openTestLedger(t)
	c := testConfig()
	p := testPlan()
	var accepted atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < 40; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			b := Batch{Signal: "metrics", Payload: []byte(fmt.Sprint(i)), MetricSamples: 1}
			_, err := l.Accept(context.Background(), c.Revision, c.Terms[0], p, b, testNow)
			if err == nil {
				accepted.Add(1)
			} else if !errors.Is(err, ErrQuota) {
				t.Errorf("%v", err)
			}
		}(i)
	}
	wg.Wait()
	if accepted.Load() != 10 || usage(t, l, c, p).MetricSamples != 10 {
		t.Fatal("quota overshoot or lost update")
	}
}

func TestCrashRetryFutureRenewalAndUpgradePreserveCounters(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ledger.db")
	l, err := OpenLedger(path)
	if err != nil {
		t.Fatal(err)
	}
	c := testConfig()
	p := testPlan()
	if err = l.Sync(c); err != nil {
		t.Fatal(err)
	}
	b := Batch{Signal: "logs", Payload: []byte("payload"), LogTraceBytes: 7}
	if _, err = l.Accept(context.Background(), 1, c.Terms[0], p, b, testNow); err != nil {
		t.Fatal(err)
	}
	l.Close()
	l, err = OpenLedger(path)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	duplicate, err := l.Accept(context.Background(), 1, c.Terms[0], p, b, testNow)
	if err != nil || !duplicate {
		t.Fatal("retry charged again", err)
	}
	if pending, err := l.Next(); err != nil || string(pending.Payload) != "payload" {
		t.Fatal("crash lost payload", err)
	}
	c.Revision = 2
	c.Terms = append(c.Terms, Term{CycleID: "paid-2", StartsAt: c.Terms[0].EndsAt, EndsAt: c.Terms[0].EndsAt + 31*86400, Plan: "lite"})
	if err = l.Sync(c); err != nil {
		t.Fatal(err)
	}
	if usage(t, l, c, p).LogTraceBytes != 7 {
		t.Fatal("early renewal reset current cycle")
	}
	c.Revision = 3
	c.Terms[0].Plan = "essential"
	if err = l.Sync(c); err != nil {
		t.Fatal(err)
	}
	if usage(t, l, c, p).LogTraceBytes != 7 {
		t.Fatal("upgrade reset current cycle")
	}
	if err = l.Sync(testConfig()); !errors.Is(err, ErrRevision) {
		t.Fatal("stale revision accepted", err)
	}
	_, err = l.Accept(context.Background(), 2, c.Terms[0], p, b, testNow)
	if !errors.Is(err, ErrRevision) {
		t.Fatal("inflight old revision accepted")
	}
}

func TestCardinalityRateAndStorageQueueFailureDoNotCharge(t *testing.T) {
	l := openTestLedger(t)
	c := testConfig()
	p := testPlan()
	p.ActiveSeries = 1
	b := Batch{Signal: "metrics", Payload: []byte("a"), MetricSamples: 1, Series: []string{"x", "y"}}
	if _, err := l.Accept(context.Background(), 1, c.Terms[0], p, b, testNow); !errors.Is(err, ErrSeries) {
		t.Fatal(err)
	}
	p.BurstBytes = 4095
	b.Series = nil
	if _, err := l.Accept(context.Background(), 1, c.Terms[0], p, b, testNow); !errors.Is(err, ErrRate) {
		t.Fatal(err)
	}
	if u := usage(t, l, c, p); u.MetricSamples != 0 || u.ActiveSeries != 0 || u.QueuedBytes != 0 {
		t.Fatal(u)
	}
}

func TestOTLPMeasuresUncompressedProtobufAndMetricPoints(t *testing.T) {
	logs := []byte(`{"resourceLogs":[{"scopeLogs":[{"logRecords":[{"body":{"stringValue":"hello"}}]}]}]}`)
	jsonBatch, err := Measure("logs", logs, true)
	if err != nil {
		t.Fatal(err)
	}
	protoBatch, err := Measure("logs", jsonBatch.Payload, false)
	if err != nil {
		t.Fatal(err)
	}
	if protoBatch.LogTraceBytes != int64(len(jsonBatch.Payload)) || protoBatch.LogTraceBytes != jsonBatch.LogTraceBytes {
		t.Fatal("wire format changed charge")
	}
	metrics := []byte(`{"resourceMetrics":[{"scopeMetrics":[{"metrics":[{"name":"cpu","gauge":{"dataPoints":[{"asDouble":1,"attributes":[{"key":"host","value":{"stringValue":"a"}}]},{"asDouble":2,"attributes":[{"key":"host","value":{"stringValue":"a"}}]}]}}]}]}]}`)
	m, err := Measure("metrics", metrics, true)
	if err != nil || m.MetricSamples != 2 || len(m.Series) != 1 || m.LogTraceBytes != 0 {
		t.Fatal(m, err)
	}
	if _, err := Measure("logs", []byte("invalid protobuf"), false); err == nil {
		t.Fatal("accepted malformed payload")
	}
}

func testServer(t *testing.T) (*Server, Config, string) {
	t.Helper()
	l := openTestLedger(t)
	c := testConfig()
	file := filepath.Join(t.TempDir(), "config.json")
	saveConfig(t, file, c)
	s := NewServer(l, map[string]Plan{"lite": testPlan(), "essential": testPlan()}, file, "")
	s.Now = func() time.Time { return testNow }
	return s, c, file
}
func saveConfig(t *testing.T, file string, c Config) {
	t.Helper()
	b, _ := json.Marshal(c)
	if err := os.WriteFile(file, b, 0600); err != nil {
		t.Fatal(err)
	}
}
func request(s *Server, path, token, encoding string, body []byte) *httptest.ResponseRecorder {
	r := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
	r.Header.Set("Authorization", "Bearer "+token)
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("Content-Encoding", encoding)
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	return w
}

func TestHTTPRejectsUnauthorizedExpiredStaleHealthAndZipBomb(t *testing.T) {
	s, c, file := testServer(t)
	body := []byte(`{"resourceLogs":[{"scopeLogs":[{"logRecords":[{"body":{"stringValue":"test"}}]}]}]}`)
	if w := request(s, "/v1/logs", "incorrect-token-that-is-at-least-32-characters", "", body); w.Code != 401 {
		t.Fatal(w.Code)
	}
	c.StorageObservedAt -= 91
	saveConfig(t, file, c)
	if w := request(s, "/v1/logs", testToken, "", body); w.Code != 503 {
		t.Fatal(w.Code)
	}
	c.StorageObservedAt = testNow.Unix()
	c.Terms[0].EndsAt = testNow.Unix()
	c.Revision++
	saveConfig(t, file, c)
	// Reusing cycle boundaries is rejected by the ledger, never reset.
	if w := request(s, "/v1/logs", testToken, "", body); w.Code != 503 {
		t.Fatal(w.Code)
	}
	c = testConfig()
	saveConfig(t, file, c)
	var compressed bytes.Buffer
	gz := gzip.NewWriter(&compressed)
	gz.Write([]byte(strings.Repeat("x", MaxPayload+1)))
	gz.Close()
	if w := request(s, "/v1/logs", testToken, "gzip", compressed.Bytes()); w.Code != 413 {
		t.Fatal(w.Code)
	}
	if usage(t, s.Ledger, c, testPlan()).LogTraceBytes != 0 {
		t.Fatal("rejection charged usage")
	}
	if w := request(s, "/v1/logs", testToken, "", body); w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	s.Now = func() time.Time { return testNow.Add(101 * time.Second) }
	if w := request(s, "/v1/logs", testToken, "", body); w.Code != 403 {
		t.Fatal("expired term accepted", w.Code)
	}
}

func TestPublicBootstrapAndNestedSQLAreBlocked(t *testing.T) {
	s, _, _ := testServer(t)
	for _, path := range []string{"/api/v1/register", "/api/v2/settings/ttl", "/api/v5/query_range"} {
		if w := request(s, path, testToken, "", []byte(`{"compositeQuery":{"queries":[{"type":"clickhouse_sql","spec":{"query":"SELECT * FROM system.query_log"}}]}}`)); w.Code != 403 {
			t.Fatal(path, w.Code)
		}
	}
}

func TestPartialSuccessIsQuarantined(t *testing.T) {
	l := openTestLedger(t)
	c := testConfig()
	p := testPlan()
	b := Batch{Signal: "logs", Payload: []byte("data"), LogTraceBytes: 4}
	if _, err := l.Accept(context.Background(), 1, c.Terms[0], p, b, testNow); err != nil {
		t.Fatal(err)
	}
	response := plogotlp.NewExportResponse()
	response.PartialSuccess().SetRejectedLogRecords(1)
	body, _ := response.MarshalProto()
	if fullyAccepted("logs", body) || !fullyAccepted("logs", nil) {
		t.Fatal("incorrect OTLP acknowledgement")
	}
	pending, _ := l.Next()
	if err := l.NeedsAttention(pending.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := l.Next(); err != sql.ErrNoRows {
		t.Fatal("quarantined batch is retried")
	}
	if u := usage(t, l, c, p); u.BatchesNeedingAttention != 1 || u.LogTraceBytes != 4 {
		t.Fatal(u)
	}
}
