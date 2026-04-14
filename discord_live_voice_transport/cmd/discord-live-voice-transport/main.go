package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"discord_live_voice_transport/internal/service"
	"discord_live_voice_transport/internal/transport"
)

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	runtime := transport.NewDisgoRuntime(logger)
	svc := service.New(runtime, os.Stdout)

	if err := svc.Run(ctx, os.Stdin); err != nil && err != io.EOF {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
