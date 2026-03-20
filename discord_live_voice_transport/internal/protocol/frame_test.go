package protocol

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestFrameDecodeAndEncodeRoundTrip(t *testing.T) {
	input := []byte(`{"type":"open_session","session_id":"sess-1","token":"bot-token","rooms":[{"guild_id":"1","channel_id":"2"}]}`)

	frame, err := DecodeLine(input)
	if err != nil {
		t.Fatalf("DecodeLine() error = %v", err)
	}
	if frame.Type != TypeOpenSession {
		t.Fatalf("Type = %q, want %q", frame.Type, TypeOpenSession)
	}

	var buf bytes.Buffer
	if err := Encode(&buf, frame); err != nil {
		t.Fatalf("Encode() error = %v", err)
	}

	var got Frame
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if got.SessionID != "sess-1" || got.Token != "bot-token" {
		t.Fatalf("round trip mismatch: %+v", got)
	}
}

func TestFrameValidationRejectsInvalidCommands(t *testing.T) {
	t.Parallel()

	cases := []Frame{
		{Type: TypeOpenSession},
		{Type: TypePlayAudio, SessionID: "sess-1", Room: &RoomRef{GuildID: "1"}},
		{Type: TypeStopAudio, SessionID: "sess-1"},
		{Type: TypeTransportFailed, Stage: "", Error: "boom"},
		{Type: "unknown"},
	}

	for _, tc := range cases {
		if err := tc.Validate(); err == nil {
			t.Fatalf("Validate() for %+v = nil, want error", tc)
		}
	}
}
