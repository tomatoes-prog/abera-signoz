package app

import (
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/SigNoz/signoz/pkg/http/middleware"
	"github.com/SigNoz/signoz/pkg/modules/savedview"
	"github.com/SigNoz/signoz/pkg/queryparser"
	"github.com/SigNoz/signoz/pkg/signoz"
)

// Unrelated handlers are registered but never invoked by these requests. A
// restored legacy history route must fail this regression even before auth.
type historyUnusedHandlers struct{ savedview.Handler }

func (historyUnusedHandlers) Get(http.ResponseWriter, *http.Request)                      {}
func (historyUnusedHandlers) Set(http.ResponseWriter, *http.Request)                      {}
func (historyUnusedHandlers) List(http.ResponseWriter, *http.Request)                     {}
func (historyUnusedHandlers) Create(http.ResponseWriter, *http.Request)                   {}
func (historyUnusedHandlers) Update(http.ResponseWriter, *http.Request)                   {}
func (historyUnusedHandlers) Delete(http.ResponseWriter, *http.Request)                   {}
func (historyUnusedHandlers) GetTopOperations(http.ResponseWriter, *http.Request)         {}
func (historyUnusedHandlers) GetEntryPointOperations(http.ResponseWriter, *http.Request)  {}
func (historyUnusedHandlers) GetQuickFilters(http.ResponseWriter, *http.Request)          {}
func (historyUnusedHandlers) GetSourceFilters(http.ResponseWriter, *http.Request)         {}
func (historyUnusedHandlers) UpdateQuickFilters(http.ResponseWriter, *http.Request)       {}
func (historyUnusedHandlers) GetSpanPercentileDetails(http.ResponseWriter, *http.Request) {}

func (historyUnusedHandlers) ListQuickFiltersV2(http.ResponseWriter, *http.Request)   {}
func (historyUnusedHandlers) GetQuickFiltersV2(http.ResponseWriter, *http.Request)    {}
func (historyUnusedHandlers) UpdateQuickFiltersV2(http.ResponseWriter, *http.Request) {}

func TestLegacyRuleHistoryRoutesAreAbsent(t *testing.T) {
	handler := &APIHandler{
		Signoz: &signoz.SigNoz{Handlers: signoz.Handlers{
			SavedView:      historyUnusedHandlers{},
			Services:       historyUnusedHandlers{},
			Apdex:          historyUnusedHandlers{},
			QuickFilter:    historyUnusedHandlers{},
			SpanPercentile: historyUnusedHandlers{},
		}},
		QueryParserAPI: &queryparser.API{},
	}
	router := NewRouter()
	handler.RegisterRoutes(router, middleware.NewAuthZ(slog.Default(), nil, nil))
	for _, endpoint := range []string{"stats", "timeline", "top_contributors", "filter_keys", "filter_values", "overall_status"} {
		t.Run(endpoint, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodGet, "/api/v1/rules/00000000-0000-0000-0000-000000000001/history/"+endpoint, nil)
			response := httptest.NewRecorder()
			router.ServeHTTP(response, request)
			if response.Code != http.StatusNotFound {
				t.Fatalf("removed legacy history route returned %d, want 404", response.Code)
			}
		})
	}
}
