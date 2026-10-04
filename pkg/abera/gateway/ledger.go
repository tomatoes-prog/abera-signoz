package gateway

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"time"

	_ "modernc.org/sqlite"
)

var ErrQuota = errors.New("prepaid quota exhausted")
var ErrRate = errors.New("throughput limit reached")
var ErrSeries = errors.New("active series limit reached")
var ErrQueue = errors.New("durable queue is full")
var ErrRevision = errors.New("stale or conflicting entitlement")

type Ledger struct{ db *sql.DB }

func OpenLedger(path string) (*Ledger, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	_, err = db.Exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=10000; PRAGMA max_page_count=65536;
CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), subscription TEXT NOT NULL, namespace TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cycles (id TEXT PRIMARY KEY, starts INTEGER NOT NULL, ends INTEGER NOT NULL, bytes INTEGER NOT NULL DEFAULT 0, samples INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS queue (id TEXT PRIMARY KEY, signal TEXT NOT NULL, payload BLOB NOT NULL, created INTEGER NOT NULL, attention INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS series (id TEXT PRIMARY KEY, seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rate (id INTEGER PRIMARY KEY CHECK(id=1), tokens REAL NOT NULL, updated REAL NOT NULL);`)
	if err != nil {
		db.Close()
		return nil, err
	}
	return &Ledger{db: db}, nil
}

func (l *Ledger) Close() error { return l.db.Close() }

// Sync fences delayed billing events. Reusing a cycle for an upgrade retains
// its counters. A future renewal has its own cycle and never resets this one.
func (l *Ledger) Sync(c Config) error {
	b, _ := json.Marshal(c.Terms)
	h := sha256.Sum256(b)
	digest := hex.EncodeToString(h[:])
	tx, err := l.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var sub, ns, savedDigest string
	var revision int64
	err = tx.QueryRow(`SELECT subscription, namespace, revision, digest FROM identity WHERE id=1`).Scan(&sub, &ns, &revision, &savedDigest)
	if err != nil && err != sql.ErrNoRows {
		return err
	}
	if err == nil && (sub != c.SubscriptionID || ns != c.Namespace || c.Revision < revision || (c.Revision == revision && savedDigest != digest)) {
		return ErrRevision
	}
	for _, term := range c.Terms {
		var starts, ends int64
		err := tx.QueryRow(`SELECT starts, ends FROM cycles WHERE id=?`, term.CycleID).Scan(&starts, &ends)
		if err == nil && (starts != term.StartsAt || ends != term.EndsAt) {
			return fmt.Errorf("cycle boundaries cannot change")
		}
		if err != nil && err != sql.ErrNoRows {
			return err
		}
		if _, err = tx.Exec(`INSERT OR IGNORE INTO cycles(id, starts, ends) VALUES(?,?,?)`, term.CycleID, term.StartsAt, term.EndsAt); err != nil {
			return err
		}
	}
	_, err = tx.Exec(`INSERT INTO identity VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,digest=excluded.digest`, c.SubscriptionID, c.Namespace, c.Revision, digest)
	if err != nil {
		return err
	}
	return tx.Commit()
}

// Accept commits usage and the actual payload in the SAME fsynced transaction.
// A crash after acceptance therefore cannot erase the charge or queued data.
func (l *Ledger) Accept(ctx context.Context, revision int64, term Term, plan Plan, batch Batch, now time.Time) (bool, error) {
	digest := sha256.New()
	digest.Write([]byte(term.CycleID + ":" + batch.Signal + ":"))
	digest.Write(batch.Payload)
	id := hex.EncodeToString(digest.Sum(nil))
	tx, err := l.db.BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	var currentRevision int64
	if err = tx.QueryRow(`SELECT revision FROM identity WHERE id=1`).Scan(&currentRevision); err != nil {
		return false, err
	}
	if currentRevision != revision {
		return false, ErrRevision
	}
	var exists int
	if err = tx.QueryRow(`SELECT 1 FROM receipts WHERE id=?`, id).Scan(&exists); err == nil {
		return true, nil
	} else if err != sql.ErrNoRows {
		return false, err
	}
	var usedBytes, samples, queued int64
	if err = tx.QueryRow(`SELECT bytes,samples FROM cycles WHERE id=?`, term.CycleID).Scan(&usedBytes, &samples); err != nil {
		return false, err
	}
	if batch.LogTraceBytes > plan.LogTraceBytes-usedBytes || batch.MetricSamples > plan.MetricSamples-samples {
		return false, ErrQuota
	}
	if err = tx.QueryRow(`SELECT coalesce(sum(length(payload)),0) FROM queue`).Scan(&queued); err != nil {
		return false, err
	}
	if queued+int64(len(batch.Payload)) > MaxQueuedBytes {
		return false, ErrQueue
	}
	if _, err = tx.Exec(`DELETE FROM series WHERE seen < ?`, now.Unix()-3600); err != nil {
		return false, err
	}
	for _, key := range batch.Series {
		if _, err = tx.Exec(`INSERT INTO series VALUES(?,?) ON CONFLICT(id) DO UPDATE SET seen=excluded.seen`, key, now.Unix()); err != nil {
			return false, err
		}
	}
	var count int64
	if err = tx.QueryRow(`SELECT count(*) FROM series`).Scan(&count); err != nil {
		return false, err
	}
	if count > plan.ActiveSeries {
		return false, ErrSeries
	}
	tokens, updated := float64(plan.BurstBytes), float64(now.UnixNano())/1e9
	err = tx.QueryRow(`SELECT tokens,updated FROM rate WHERE id=1`).Scan(&tokens, &updated)
	if err != nil && err != sql.ErrNoRows {
		return false, err
	}
	seconds := float64(now.UnixNano()) / 1e9
	tokens = math.Min(float64(plan.BurstBytes), tokens+math.Max(0, seconds-updated)*float64(plan.IngestBytesPerSecond))
	// Tiny requests still consume capacity, bounding queue/receipt overhead.
	cost := math.Max(4096, float64(len(batch.Payload)))
	if cost > tokens {
		return false, ErrRate
	}
	if _, err = tx.Exec(`INSERT INTO rate VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET tokens=excluded.tokens,updated=excluded.updated`, tokens-cost, seconds); err != nil {
		return false, err
	}
	if _, err = tx.Exec(`UPDATE cycles SET bytes=bytes+?, samples=samples+? WHERE id=?`, batch.LogTraceBytes, batch.MetricSamples, term.CycleID); err != nil {
		return false, err
	}
	if _, err = tx.Exec(`INSERT INTO queue(id,signal,payload,created) VALUES(?,?,?,?)`, id, batch.Signal, batch.Payload, now.Unix()); err != nil {
		return false, err
	}
	if _, err = tx.Exec(`INSERT INTO receipts VALUES(?,?)`, id, now.Unix()); err != nil {
		return false, err
	}
	if _, err = tx.Exec(`DELETE FROM receipts WHERE created < ? AND id NOT IN (SELECT id FROM queue)`, now.Unix()-86400); err != nil {
		return false, err
	}
	return false, tx.Commit()
}

type Pending struct {
	ID, Signal string
	Payload    []byte
}

func (l *Ledger) Next() (Pending, error) {
	var p Pending
	err := l.db.QueryRow(`SELECT id,signal,payload FROM queue WHERE attention=0 ORDER BY created,id LIMIT 1`).Scan(&p.ID, &p.Signal, &p.Payload)
	return p, err
}
func (l *Ledger) Delivered(id string) error {
	_, err := l.db.Exec(`DELETE FROM queue WHERE id=?`, id)
	return err
}
func (l *Ledger) NeedsAttention(id string) error {
	_, err := l.db.Exec(`UPDATE queue SET attention=1 WHERE id=?`, id)
	return err
}

type Usage struct {
	CycleID                 string `json:"cycleId"`
	LogTraceBytes           int64  `json:"logTraceBytes"`
	MetricSamples           int64  `json:"metricSamples"`
	ActiveSeries            int64  `json:"activeSeries"`
	QueuedBytes             int64  `json:"queuedBytes"`
	BatchesNeedingAttention int64  `json:"batchesNeedingAttention"`
	WarningPercent          int    `json:"warningPercent"`
}

func (l *Ledger) Usage(term Term, plan Plan, now time.Time) (Usage, error) {
	u := Usage{CycleID: term.CycleID}
	if err := l.db.QueryRow(`SELECT bytes,samples FROM cycles WHERE id=?`, term.CycleID).Scan(&u.LogTraceBytes, &u.MetricSamples); err != nil {
		return u, err
	}
	if err := l.db.QueryRow(`SELECT count(*) FROM series WHERE seen>=?`, now.Unix()-3600).Scan(&u.ActiveSeries); err != nil {
		return u, err
	}
	if err := l.db.QueryRow(`SELECT coalesce(sum(length(payload)),0) FROM queue`).Scan(&u.QueuedBytes); err != nil {
		return u, err
	}
	if err := l.db.QueryRow(`SELECT count(*) FROM queue WHERE attention=1`).Scan(&u.BatchesNeedingAttention); err != nil {
		return u, err
	}
	percent := math.Max(float64(u.LogTraceBytes)/float64(plan.LogTraceBytes), float64(u.MetricSamples)/float64(plan.MetricSamples)) * 100
	for _, warning := range []int{80, 95, 100} {
		if percent >= float64(warning) {
			u.WarningPercent = warning
		}
	}
	return u, nil
}
