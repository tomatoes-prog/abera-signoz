package querylimits

import (
	"context"
	"testing"
	"time"
)

func TestDriverCannotExtendBudgetAndCancellationSurvives(t *testing.T) {
	type key struct{}
	parent, cancel := context.WithTimeout(context.WithValue(context.Background(), key{}, "value"), time.Minute)
	settings := map[string]any{"max_threads": 24, "max_execution_time": 300}
	ctx := Apply(parent, settings)
	if _, ok := ctx.Deadline(); ok {
		t.Fatal("driver would override the fixed query budget")
	}
	if settings["max_threads"] != 1 || settings["max_execution_time"] != 15 || ctx.Value(key{}) != "value" {
		t.Fatal("lost query limits or context")
	}
	cancel()
	select {
	case <-ctx.Done():
		if ctx.Err() != context.Canceled {
			t.Fatal(ctx.Err())
		}
	case <-time.After(time.Second):
		t.Fatal("query cancellation was lost")
	}
}
