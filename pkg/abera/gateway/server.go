package gateway

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"go.opentelemetry.io/collector/pdata/plog/plogotlp"
	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"net/http/httputil"
	"net/url"
	"path"
	"strings"
	"time"
)

//go:embed portal.html
var portal string

type Server struct {
	Ledger     *Ledger
	Plans      map[string]Plan
	ConfigPath string
	SourcePath string
	Now        func() time.Time
	client     *http.Client
	ingest     chan struct{}
}

func NewServer(ledger *Ledger, plans map[string]Plan, configPath, sourcePath string) *Server {
	return &Server{Ledger: ledger, Plans: plans, ConfigPath: configPath, SourcePath: sourcePath, Now: time.Now,
		client: &http.Client{Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, ingest: make(chan struct{}, 2)}
}

func (s *Server) config() (Config, error) {
	c, err := ReadConfig(s.ConfigPath, s.Plans)
	if err != nil {
		return c, err
	}
	return c, s.Ledger.Sync(c)
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if r.URL.Path == "/abera" && r.Method == "GET" {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		io.WriteString(w, portal)
		return
	}
	if r.URL.Path != path.Clean(r.URL.Path) || strings.Contains(r.URL.Path, "\\") {
		http.Error(w, "invalid path", 400)
		return
	}
	if r.URL.Path == "/abera/source.tar.gz" && r.Method == "GET" {
		w.Header().Set("Content-Disposition", `attachment; filename="abera-signoz-source.tar.gz"`)
		http.ServeFile(w, r, s.SourcePath)
		return
	}
	if r.URL.Path == "/abera/licenses" && r.Method == "GET" {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		io.WriteString(w, `<h1>Abera SigNoz</h1><p>SigNoz Community y software libre. Colector modificado bajo GNU AGPLv3.</p><p><a href="/abera/source.tar.gz">Descargar código fuente correspondiente, licencias e instrucciones de compilación</a></p>`)
		return
	}
	c, err := s.config()
	if err != nil {
		http.Error(w, "service configuration unavailable", 503)
		return
	}
	if r.URL.Path == "/abera/health" {
		if !c.Ready || c.State != "ACTIVE" || !s.healthy(r.Context(), c) {
			http.Error(w, "not ready", 503)
			return
		}
		w.WriteHeader(200)
		return
	}
	if !c.Ready || c.State != "ACTIVE" {
		http.Error(w, "service unavailable", 503)
		return
	}
	if r.URL.Path == "/abera/usage" {
		if !authorizedToken(r, c) && !s.authorizedUser(r, c) {
			http.Error(w, "unauthorized", 401)
			return
		}
		term, ok := c.Current(s.Now())
		if !ok {
			http.Error(w, "no active prepaid term", 403)
			return
		}
		u, err := s.Ledger.Usage(term, s.Plans[term.Plan], s.Now())
		if err != nil {
			http.Error(w, "usage unavailable", 503)
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"usage": u, "term": term, "limits": s.Plans[term.Plan], "storageBytes": c.StorageBytes})
		return
	}
	for _, signal := range []string{"logs", "traces", "metrics"} {
		if r.URL.Path == "/v1/"+signal {
			s.accept(w, r, c, signal)
			return
		}
	}
	// No public first-user race, plan retention changes, or raw SQL in v1.
	if r.URL.Path == "/api/v1/register" || (r.Method != "GET" && strings.HasSuffix(r.URL.Path, "/settings/ttl")) {
		http.Error(w, "managed by Abera", 403)
		return
	}
	if r.Method == "POST" || r.Method == "PUT" || r.Method == "PATCH" {
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, MaxPayload))
		if err != nil {
			http.Error(w, "request too large", 413)
			return
		}
		if len(body) > 0 && strings.HasPrefix(r.URL.Path, "/api/") {
			var value any
			if json.Unmarshal(body, &value) != nil {
				http.Error(w, "expected JSON", 400)
				return
			}
			if containsSQL(value) {
				http.Error(w, "raw SQL unavailable in this plan", 403)
				return
			}
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
	}
	// Grace allows queries for four days; expiry itself already blocks ingest.
	var lastEnd int64
	for _, term := range c.Terms {
		if term.EndsAt > lastEnd {
			lastEnd = term.EndsAt
		}
	}
	if lastEnd == 0 || s.Now().Unix() >= lastEnd+4*86400 {
		http.Error(w, "subscription expired", 403)
		return
	}
	u, _ := url.Parse(c.AppURL)
	proxy := httputil.NewSingleHostReverseProxy(u)
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) { http.Error(w, "application unavailable", 502) }
	proxy.Transport = &http.Transport{ResponseHeaderTimeout: 30 * time.Second, DisableKeepAlives: true}
	proxy.ModifyResponse = func(res *http.Response) error { res.Header.Set("Link", `</abera/licenses>; rel="license"`); return nil }
	r.Header.Del("X-Signoz-Org-Id")
	proxy.ServeHTTP(w, r)
}

func (s *Server) healthy(ctx context.Context, c Config) bool {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	u, _ := url.Parse(c.CollectorURL)
	u.Host = u.Hostname() + ":13133"
	for _, address := range []string{c.AppURL + "/api/v1/health", u.String()} {
		req, _ := http.NewRequestWithContext(ctx, "GET", address, nil)
		res, err := s.client.Do(req)
		if err != nil {
			return false
		}
		res.Body.Close()
		if res.StatusCode != 200 {
			return false
		}
	}
	return true
}

func containsSQL(value any) bool {
	switch v := value.(type) {
	case map[string]any:
		for k, x := range v {
			if (k == "queryType" || k == "type") && (x == "clickhouse_sql" || x == "clickhouse") {
				return true
			}
			if k == "clickhouseQueries" {
				if m, ok := x.(map[string]any); ok && len(m) > 0 {
					return true
				}
			}
			if containsSQL(x) {
				return true
			}
		}
	case []any:
		for _, x := range v {
			if containsSQL(x) {
				return true
			}
		}
	}
	return false
}

func authorizedToken(r *http.Request, c Config) bool {
	token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !ok || len(token) < 32 || len(token) > 256 {
		return false
	}
	want, _ := hex.DecodeString(c.TokenSHA256)
	got := sha256.Sum256([]byte(token))
	return subtle.ConstantTimeCompare(want, got[:]) == 1
}

func (s *Server) authorizedUser(r *http.Request, c Config) bool {
	if r.Header.Get("Authorization") == "" {
		return false
	}
	req, _ := http.NewRequestWithContext(r.Context(), "GET", c.AppURL+"/api/v2/users/me", nil)
	req.Header.Set("Authorization", r.Header.Get("Authorization"))
	res, err := s.client.Do(req)
	if err != nil {
		return false
	}
	defer res.Body.Close()
	return res.StatusCode == 200
}

func (s *Server) accept(w http.ResponseWriter, r *http.Request, c Config, signal string) {
	if r.Method != "POST" {
		w.Header().Set("Allow", "POST")
		http.Error(w, "method not allowed", 405)
		return
	}
	if !authorizedToken(r, c) {
		http.Error(w, "unauthorized", 401)
		return
	}
	term, ok := c.Current(s.Now())
	if !ok {
		http.Error(w, "no active prepaid term", 403)
		return
	}
	p := s.Plans[term.Plan]
	if !c.DiskHealthy || s.Now().Unix()-c.StorageObservedAt > 90 || c.StorageObservedAt > s.Now().Unix()+5 {
		http.Error(w, "storage health unavailable", 503)
		return
	}
	if c.StorageBytes >= p.StorageBytes {
		http.Error(w, "storage capacity reached", 429)
		return
	}
	select {
	case s.ingest <- struct{}{}:
		defer func() { <-s.ingest }()
	default:
		w.Header().Set("Retry-After", "1")
		http.Error(w, "busy", 429)
		return
	}
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || (media != "application/json" && media != "application/x-protobuf") {
		http.Error(w, "unsupported content type", 415)
		return
	}
	var reader io.Reader = http.MaxBytesReader(w, r.Body, MaxPayload)
	if encoding := r.Header.Get("Content-Encoding"); encoding == "gzip" {
		gz, err := gzip.NewReader(reader)
		if err != nil {
			http.Error(w, "invalid gzip", 400)
			return
		}
		defer gz.Close()
		reader = gz
	} else if encoding != "" && encoding != "identity" {
		http.Error(w, "unsupported encoding", 415)
		return
	}
	body, err := io.ReadAll(io.LimitReader(reader, MaxPayload+1))
	if err != nil {
		http.Error(w, "invalid request body", 400)
		return
	}
	if len(body) > MaxPayload {
		http.Error(w, "decoded payload too large", 413)
		return
	}
	batch, err := Measure(signal, body, media == "application/json")
	if err != nil {
		http.Error(w, "invalid OTLP payload", 400)
		return
	}
	if len(batch.Payload) > 0 {
		_, err = s.Ledger.Accept(r.Context(), c.Revision, term, p, batch, s.Now())
		if err != nil {
			status := 503
			if errors.Is(err, ErrQuota) || errors.Is(err, ErrRate) || errors.Is(err, ErrSeries) {
				status = 429
			}
			w.Header().Set("Retry-After", "60")
			http.Error(w, http.StatusText(status), status)
			return
		}
	}
	w.Header().Set("Content-Type", media)
	if media == "application/json" {
		io.WriteString(w, "{}")
	} else {
		w.WriteHeader(200)
	}
}

// Deliver retries transport failures without consuming the quota again. OTLP
// delivery is at least once: an ambiguous downstream acknowledgement can repeat
// data, but cannot create another usage charge in this ledger.
func (s *Server) Deliver(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		c, err := s.config()
		if err != nil || !c.Ready || c.State != "ACTIVE" {
			continue
		}
		for n := 0; n < 32; n++ {
			pending, err := s.Ledger.Next()
			if err == sql.ErrNoRows {
				break
			}
			if err != nil {
				slog.Error("queue read failed")
				break
			}
			req, _ := http.NewRequestWithContext(ctx, "POST", c.CollectorURL+"/v1/"+pending.Signal, bytes.NewReader(pending.Payload))
			req.Header.Set("Content-Type", "application/x-protobuf")
			res, err := s.client.Do(req)
			if err != nil {
				break
			}
			body, readErr := io.ReadAll(io.LimitReader(res.Body, 65537))
			res.Body.Close()
			if readErr != nil {
				break
			}
			if res.StatusCode == 200 && !fullyAccepted(pending.Signal, body) {
				// OTLP forbids replaying partial successes. Retain the payload in
				// quarantine and expose the failure in usage for operator recovery.
				s.Ledger.NeedsAttention(pending.ID)
				slog.Error("collector partially rejected a batch")
				break
			}
			if res.StatusCode != 200 {
				if res.StatusCode >= 400 && res.StatusCode < 500 && res.StatusCode != 429 && res.StatusCode != 408 {
					s.Ledger.NeedsAttention(pending.ID)
				}
				slog.Warn("collector unavailable", "status", res.StatusCode)
				break
			}
			if err = s.Ledger.Delivered(pending.ID); err != nil {
				slog.Error("queue acknowledgement failed")
				break
			}
		}
	}
}

func fullyAccepted(signal string, body []byte) bool {
	switch signal {
	case "logs":
		r := plogotlp.NewExportResponse()
		return r.UnmarshalProto(body) == nil && r.PartialSuccess().RejectedLogRecords() == 0
	case "metrics":
		r := pmetricotlp.NewExportResponse()
		return r.UnmarshalProto(body) == nil && r.PartialSuccess().RejectedDataPoints() == 0
	case "traces":
		r := ptraceotlp.NewExportResponse()
		return r.UnmarshalProto(body) == nil && r.PartialSuccess().RejectedSpans() == 0
	}
	return false
}
