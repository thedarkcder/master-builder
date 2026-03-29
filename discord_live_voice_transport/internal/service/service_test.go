package service

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"

	"discord_live_voice_transport/internal/protocol"
)

type fakeRuntime struct {
	events chan protocol.Frame

	openCalls  []protocol.Frame
	closeCalls []protocol.Frame
	playCalls  []protocol.Frame
	stopCalls  []protocol.Frame
}

func newFakeRuntime() *fakeRuntime {
	return &fakeRuntime{events: make(chan protocol.Frame, 8)}
}

func (r *fakeRuntime) OpenSession(ctx context.Context, frame protocol.Frame) error {
	r.openCalls = append(r.openCalls, frame)
	r.events <- protocol.Ready(frame.SessionID, "fake", "client-1")
	r.events <- protocol.RoomUpdated(frame.SessionID, frame.Rooms[0], 2, []string{"u-1", "u-2"}, true)
	return nil
}

func (r *fakeRuntime) CloseSession(ctx context.Context, frame protocol.Frame) error {
	r.closeCalls = append(r.closeCalls, frame)
	return nil
}

func (r *fakeRuntime) PlayAudio(ctx context.Context, frame protocol.Frame) error {
	r.playCalls = append(r.playCalls, frame)
	r.events <- protocol.PlaybackFinished(frame.SessionID, *frame.Room, len(frame.OpusFramesBase64)+boolToInt(frame.OpusFrameBase64 != ""))
	return nil
}

func (r *fakeRuntime) StopAudio(ctx context.Context, frame protocol.Frame) error {
	r.stopCalls = append(r.stopCalls, frame)
	return nil
}

func (r *fakeRuntime) Events() <-chan protocol.Frame {
	return r.events
}

func (r *fakeRuntime) Close(ctx context.Context) error {
	return nil
}

func boolToInt(v bool) int {
	if v {
		return 1
	}
	return 0
}

func decodeFrames(t *testing.T, output string) []protocol.Frame {
	t.Helper()

	lines := strings.Split(strings.TrimSpace(output), "\n")
	frames := make([]protocol.Frame, 0, len(lines))
	for _, line := range lines {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var frame protocol.Frame
		if err := json.Unmarshal([]byte(line), &frame); err != nil {
			t.Fatalf("json.Unmarshal(%q) error = %v", line, err)
		}
		frames = append(frames, frame)
	}
	return frames
}

func TestServiceRoutesCommandsAndForwardsEvents(t *testing.T) {
	runtime := newFakeRuntime()
	var out bytes.Buffer
	svc := New(runtime, &out)

	input := strings.NewReader(strings.Join([]string{
		`{"type":"open_session","session_id":"sess-1","token":"bot-token","rooms":[{"guild_id":"1","channel_id":"2"}]}`,
		`{"type":"play_audio","session_id":"sess-1","room":{"guild_id":"1","channel_id":"2"},"opus_frames_base64":["aGVsbG8="]}`,
		`{"type":"stop_audio","session_id":"sess-1","room":{"guild_id":"1","channel_id":"2"}}`,
		`{"type":"close_session","session_id":"sess-1"}`,
	}, "\n"))

	if err := svc.Run(context.Background(), input); err != nil {
		t.Fatalf("Run() error = %v", err)
	}

	if len(runtime.openCalls) != 1 {
		t.Fatalf("open calls = %d, want 1", len(runtime.openCalls))
	}
	if len(runtime.playCalls) != 1 {
		t.Fatalf("play calls = %d, want 1", len(runtime.playCalls))
	}
	if len(runtime.stopCalls) != 1 {
		t.Fatalf("stop calls = %d, want 1", len(runtime.stopCalls))
	}
	if len(runtime.closeCalls) != 1 {
		t.Fatalf("close calls = %d, want 1", len(runtime.closeCalls))
	}

	frames := decodeFrames(t, out.String())
	if len(frames) < 4 {
		t.Fatalf("event count = %d, want at least 4", len(frames))
	}
	if frames[0].Type != protocol.TypeTransportReady {
		t.Fatalf("first frame type = %q, want %q", frames[0].Type, protocol.TypeTransportReady)
	}
	if frames[1].Type != protocol.TypeTransportReady {
		t.Fatalf("second frame type = %q, want %q", frames[1].Type, protocol.TypeTransportReady)
	}
	if frames[2].Type != protocol.TypeRoomState {
		t.Fatalf("third frame type = %q, want %q", frames[2].Type, protocol.TypeRoomState)
	}
	if frames[3].Type != protocol.TypePlaybackFinished {
		t.Fatalf("fourth frame type = %q, want %q", frames[3].Type, protocol.TypePlaybackFinished)
	}
}

func TestServiceEmitsDecodeFailures(t *testing.T) {
	runtime := newFakeRuntime()
	var out bytes.Buffer
	svc := New(runtime, &out)

	if err := svc.Run(context.Background(), strings.NewReader("{")); err != nil {
		t.Fatalf("Run() error = %v", err)
	}

	frames := decodeFrames(t, out.String())
	if len(frames) != 2 {
		t.Fatalf("event count = %d, want 2", len(frames))
	}
	if frames[0].Type != protocol.TypeTransportReady {
		t.Fatalf("first frame type = %q, want %q", frames[0].Type, protocol.TypeTransportReady)
	}
	if frames[1].Type != protocol.TypeTransportFailed {
		t.Fatalf("second frame type = %q, want %q", frames[1].Type, protocol.TypeTransportFailed)
	}
}

func TestServiceEmitsTransportReadyBeforeReceivingInput(t *testing.T) {
	runtime := newFakeRuntime()
	var out bytes.Buffer
	svc := New(runtime, &out)

	if err := svc.Run(context.Background(), strings.NewReader("")); err != nil {
		t.Fatalf("Run() error = %v", err)
	}

	frames := decodeFrames(t, out.String())
	if len(frames) != 1 {
		t.Fatalf("event count = %d, want 1", len(frames))
	}
	if frames[0].Type != protocol.TypeTransportReady {
		t.Fatalf("frame type = %q, want %q", frames[0].Type, protocol.TypeTransportReady)
	}
	if frames[0].SessionID != "discord-live-voice" {
		t.Fatalf("session id = %q, want %q", frames[0].SessionID, "discord-live-voice")
	}
}
