package signozapiserver

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/SigNoz/signoz/pkg/authz"
	"github.com/SigNoz/signoz/pkg/http/middleware"
	"github.com/SigNoz/signoz/pkg/types/authtypes"
	"github.com/SigNoz/signoz/pkg/types/coretypes"
	"github.com/SigNoz/signoz/pkg/valuer"
	"github.com/gorilla/mux"
)

type historyRouteAuthZ struct {
	authz.AuthZ
	checks int
}

func (a *historyRouteAuthZ) CheckWithTupleCreation(context.Context, authtypes.Claims, valuer.UUID, authtypes.Relation, coretypes.Resource, []coretypes.Selector, []coretypes.Selector) error {
	a.checks++
	return nil
}

type historyRouteHandler struct{}

func (historyRouteHandler) GetRuleHistoryStats(w http.ResponseWriter, _ *http.Request) {
	_, _ = w.Write([]byte("stats"))
}
func (historyRouteHandler) GetRuleHistoryTimeline(w http.ResponseWriter, _ *http.Request) {
	_, _ = w.Write([]byte("timeline"))
}
func (historyRouteHandler) GetRuleHistoryContributors(w http.ResponseWriter, _ *http.Request) {
	_, _ = w.Write([]byte("top_contributors"))
}
func (historyRouteHandler) GetRuleHistoryFilterKeys(w http.ResponseWriter, _ *http.Request) {
	_, _ = w.Write([]byte("filter_keys"))
}
func (historyRouteHandler) GetRuleHistoryFilterValues(w http.ResponseWriter, _ *http.Request) {
	_, _ = w.Write([]byte("filter_values"))
}
func (historyRouteHandler) GetRuleHistoryOverallStatus(w http.ResponseWriter, _ *http.Request) {
	_, _ = w.Write([]byte("overall_status"))
}

func TestRuleHistoryV2RoutesRemainAvailableAndAuthorized(t *testing.T) {
	for _, endpoint := range []string{"stats", "timeline", "top_contributors", "filter_keys", "filter_values", "overall_status"} {
		t.Run(endpoint, func(t *testing.T) {
			authorizer := &historyRouteAuthZ{}
			provider := &provider{
				authzMiddleware:         middleware.NewAuthZ(slog.Default(), nil, authorizer),
				ruleStateHistoryHandler: historyRouteHandler{},
			}
			router := mux.NewRouter()
			if err := provider.addRuleStateHistoryRoutes(router); err != nil {
				t.Fatal(err)
			}
			request := httptest.NewRequest(http.MethodGet, "/api/v2/rules/00000000-0000-0000-0000-000000000001/history/"+endpoint, nil)
			anonymous := httptest.NewRecorder()
			router.ServeHTTP(anonymous, request)
			if anonymous.Code != http.StatusUnauthorized || authorizer.checks != 0 {
				t.Fatalf("anonymous history request: status=%d checks=%d", anonymous.Code, authorizer.checks)
			}
			claims := authtypes.Claims{OrgID: "00000000-0000-0000-0000-000000000002"}
			request = request.WithContext(authtypes.NewContextWithClaims(request.Context(), claims))
			response := httptest.NewRecorder()
			router.ServeHTTP(response, request)
			if response.Code != http.StatusOK || response.Body.String() != endpoint || authorizer.checks != 1 {
				t.Fatalf("authorized v2 history request: status=%d body=%q checks=%d", response.Code, response.Body.String(), authorizer.checks)
			}
		})
	}
}
