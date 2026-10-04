// Package querylimits keeps managed queries within their ClickHouse profile.
package querylimits

import (
	"context"
	"time"
)

type cancellationContext struct{ context.Context }

// clickhouse-go v2.44 overwrites max_execution_time with deadline+5 seconds.
// Keep the parent's cancellation and values, but let the explicit server
// setting govern execution time. The original deadline still closes Done.
func (cancellationContext) Deadline() (time.Time, bool) { return time.Time{}, false }

func Apply(ctx context.Context, settings map[string]any) context.Context {
	settings["max_threads"] = 1
	settings["max_execution_time"] = 15
	settings["max_execution_time_leaf"] = 15
	settings["max_result_rows"] = 100000
	settings["max_memory_usage"] = 268435456
	return cancellationContext{ctx}
}
