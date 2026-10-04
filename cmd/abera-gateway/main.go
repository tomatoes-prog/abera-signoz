package main

import (
	"context"
	"flag"
	"github.com/SigNoz/signoz/pkg/abera/gateway"
	"log"
	"net/http"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	config := flag.String("config", "/etc/abera/gateway.json", "private controller configuration")
	plansPath := flag.String("plans", "/etc/abera/plans.json", "prepaid plans")
	dbPath := flag.String("ledger", "/var/lib/abera/usage.db", "durable ledger and queue")
	source := flag.String("source", "/usr/share/licenses/signoz/abera-signoz-source.tar.gz", "corresponding source archive")
	listen := flag.String("listen", ":8081", "HTTP bind address behind TLS ingress")
	flag.Parse()
	plans, err := gateway.LoadPlans(*plansPath)
	if err != nil {
		log.Fatal(err)
	}
	if _, err = gateway.ReadConfig(*config, plans); err != nil {
		log.Fatal(err)
	}
	ledger, err := gateway.OpenLedger(*dbPath)
	if err != nil {
		log.Fatal(err)
	}
	defer ledger.Close()
	handler := gateway.NewServer(ledger, plans, *config, *source)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go handler.Deliver(ctx)
	server := &http.Server{Addr: *listen, Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 30 * time.Second, WriteTimeout: 45 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		server.Shutdown(shutdown)
	}()
	if err = server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
