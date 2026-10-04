package gateway

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/plog/plogotlp"
	"go.opentelemetry.io/collector/pdata/pmetric"
	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"
)

type Batch struct {
	Signal        string
	Payload       []byte // canonical, uncompressed OTLP protobuf
	LogTraceBytes int64
	MetricSamples int64
	Series        []string
}

// Measure decodes before charging. Invalid payloads and compressed wire size
// never determine the customer's usage. Metrics count points, not bytes.
func Measure(signal string, body []byte, isJSON bool) (Batch, error) {
	b := Batch{Signal: signal}
	var err error
	switch signal {
	case "logs":
		r := plogotlp.NewExportRequest()
		if isJSON {
			err = r.UnmarshalJSON(body)
		} else {
			err = r.UnmarshalProto(body)
		}
		if err == nil {
			b.Payload, err = r.MarshalProto()
		}
		b.LogTraceBytes = int64(len(b.Payload))
	case "traces":
		r := ptraceotlp.NewExportRequest()
		if isJSON {
			err = r.UnmarshalJSON(body)
		} else {
			err = r.UnmarshalProto(body)
		}
		if err == nil {
			b.Payload, err = r.MarshalProto()
		}
		b.LogTraceBytes = int64(len(b.Payload))
	case "metrics":
		r := pmetricotlp.NewExportRequest()
		if isJSON {
			err = r.UnmarshalJSON(body)
		} else {
			err = r.UnmarshalProto(body)
		}
		if err != nil {
			return b, err
		}
		b.Payload, err = r.MarshalProto()
		b.MetricSamples = int64(r.Metrics().DataPointCount())
		series := map[string]bool{}
		resources := r.Metrics().ResourceMetrics()
		for i := 0; i < resources.Len(); i++ {
			rm := resources.At(i)
			scopes := rm.ScopeMetrics()
			for j := 0; j < scopes.Len(); j++ {
				sm := scopes.At(j)
				for k := 0; k < sm.Metrics().Len(); k++ {
					m := sm.Metrics().At(k)
					temporality, monotonic := "", false
					switch m.Type() {
					case pmetric.MetricTypeSum:
						temporality, monotonic = m.Sum().AggregationTemporality().String(), m.Sum().IsMonotonic()
					case pmetric.MetricTypeHistogram:
						temporality = m.Histogram().AggregationTemporality().String()
					case pmetric.MetricTypeExponentialHistogram:
						temporality = m.ExponentialHistogram().AggregationTemporality().String()
					}
					add := func(attrs pcommon.Map) {
						// encoding/json sorts map keys. Attribute order cannot evade cardinality.
						value, _ := json.Marshal([]any{rm.Resource().Attributes().AsRaw(), rm.SchemaUrl(), sm.SchemaUrl(), sm.Scope().Name(), sm.Scope().Version(), sm.Scope().Attributes().AsRaw(), m.Name(), m.Unit(), m.Type().String(), temporality, monotonic, attrs.AsRaw()})
						h := sha256.Sum256(value)
						series[hex.EncodeToString(h[:])] = true
					}
					switch m.Type() {
					case pmetric.MetricTypeGauge:
						for n := 0; n < m.Gauge().DataPoints().Len(); n++ {
							add(m.Gauge().DataPoints().At(n).Attributes())
						}
					case pmetric.MetricTypeSum:
						for n := 0; n < m.Sum().DataPoints().Len(); n++ {
							add(m.Sum().DataPoints().At(n).Attributes())
						}
					case pmetric.MetricTypeHistogram:
						for n := 0; n < m.Histogram().DataPoints().Len(); n++ {
							add(m.Histogram().DataPoints().At(n).Attributes())
						}
					case pmetric.MetricTypeExponentialHistogram:
						for n := 0; n < m.ExponentialHistogram().DataPoints().Len(); n++ {
							add(m.ExponentialHistogram().DataPoints().At(n).Attributes())
						}
					case pmetric.MetricTypeSummary:
						for n := 0; n < m.Summary().DataPoints().Len(); n++ {
							add(m.Summary().DataPoints().At(n).Attributes())
						}
					}
				}
			}
		}
		for key := range series {
			b.Series = append(b.Series, key)
		}
	default:
		return b, fmt.Errorf("unsupported signal")
	}
	if len(b.Payload) > MaxPayload {
		return b, fmt.Errorf("decoded payload exceeds limit")
	}
	return b, err
}
