package transport

import (
	"discord_live_voice_transport/internal/protocol"
)

type eventSink struct {
	ch chan protocol.Frame
}

func newEventSink(size int) *eventSink {
	return &eventSink{ch: make(chan protocol.Frame, size)}
}

func (s *eventSink) Events() <-chan protocol.Frame {
	return s.ch
}

func (s *eventSink) emit(frame protocol.Frame) {
	s.ch <- frame
}
