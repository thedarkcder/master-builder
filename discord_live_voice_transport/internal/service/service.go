package service

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"sync"

	"discord_live_voice_transport/internal/protocol"
	"discord_live_voice_transport/internal/transport"
)

const defaultTransportSessionID = "discord-live-voice"

type Service struct {
	runtime transport.Runtime
	writer  *lineWriter
}

func New(runtime transport.Runtime, out io.Writer) *Service {
	return &Service{
		runtime: runtime,
		writer:  newLineWriter(out),
	}
}

func (s *Service) Run(ctx context.Context, in io.Reader) error {
	if err := s.writer.Write(protocol.Ready(defaultTransportSessionID, "go", "")); err != nil {
		return err
	}

	runCtx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = s.forwardEvents(runCtx)
	}()

	err := s.processInput(runCtx, in)
	_ = s.runtime.Close(runCtx)
	cancel()
	<-done

	return err
}

func (s *Service) processInput(ctx context.Context, in io.Reader) error {
	reader := bufio.NewReader(in)
	for {
		line, err := reader.ReadBytes('\n')
		if len(line) > 0 {
			frame, decodeErr := protocol.DecodeLine(line)
			if decodeErr != nil {
				if !errors.Is(decodeErr, protocol.ErrEmptyFrame) {
					if emitErr := s.writer.Write(protocol.Failed("unknown", "decode", decodeErr.Error(), false)); emitErr != nil {
						return emitErr
					}
				}
			} else {
				_ = s.handleFrame(ctx, frame)
			}
		}

		if err != nil {
			if errors.Is(err, io.EOF) {
				return nil
			}
			return err
		}
	}
}

func (s *Service) handleFrame(ctx context.Context, frame protocol.Frame) error {
	switch frame.Type {
	case protocol.TypeOpenSession:
		return s.runtime.OpenSession(ctx, frame)
	case protocol.TypeCloseSession:
		return s.runtime.CloseSession(ctx, frame)
	case protocol.TypePlayAudio:
		return s.runtime.PlayAudio(ctx, frame)
	case protocol.TypeStopAudio:
		return s.runtime.StopAudio(ctx, frame)
	default:
		return fmt.Errorf("unsupported command type %q", frame.Type)
	}
}

func (s *Service) forwardEvents(ctx context.Context) error {
	events := s.runtime.Events()
	for {
		select {
		case frame := <-events:
			if err := s.writer.Write(frame); err != nil {
				return err
			}
		case <-ctx.Done():
			for {
				select {
				case frame := <-events:
					if err := s.writer.Write(frame); err != nil {
						return err
					}
				default:
					return ctx.Err()
				}
			}
		}
	}
}

type lineWriter struct {
	mu sync.Mutex
	w  io.Writer
}

func newLineWriter(w io.Writer) *lineWriter {
	return &lineWriter{w: w}
}

func (lw *lineWriter) Write(frame protocol.Frame) error {
	lw.mu.Lock()
	defer lw.mu.Unlock()
	return protocol.Encode(lw.w, frame)
}
