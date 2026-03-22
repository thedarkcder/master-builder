package transport

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"testing"
	"time"

	botgateway "github.com/disgoorg/disgo/gateway"
	"github.com/disgoorg/disgo/voice"
	"github.com/disgoorg/snowflake/v2"
)

func TestIsTransientDaveDecryptError(t *testing.T) {
	t.Parallel()

	if !isTransientDaveDecryptError(errors.New("failed to DAVE decrypt packet: failed to decrypt frame")) {
		t.Fatal("expected DAVE decrypt error to be classified as transient")
	}
	if isTransientDaveDecryptError(errors.New("failed to read packet: EOF")) {
		t.Fatal("unexpectedly classified unrelated error as transient")
	}
}

func TestResilientAudioReceiverIgnoresTransientDecryptErrors(t *testing.T) {
	t.Parallel()

	udp := &fakeUDPConn{
		readPackets: []fakeReadPacketResult{
			{err: errors.New("failed to DAVE decrypt packet: failed to decrypt frame")},
		},
	}
	conn := &fakeConn{udp: udp}
	receiver := &fakeOpusReceiver{}
	audioReceiver := newResilientAudioReceiver(slog.Default(), receiver, conn).(*resilientAudioReceiver)

	audioReceiver.receive()

	if receiver.calls != 0 {
		t.Fatalf("receiver.calls = %d, want 0", receiver.calls)
	}
	if audioReceiver.consecutiveDecryptErrors != 1 {
		t.Fatalf("consecutiveDecryptErrors = %d, want 1", audioReceiver.consecutiveDecryptErrors)
	}
}

func TestResilientAudioReceiverPassesThroughPackets(t *testing.T) {
	t.Parallel()

	udp := &fakeUDPConn{
		readPackets: []fakeReadPacketResult{
			{packet: &voice.Packet{SSRC: 42, Opus: []byte("opus")}},
		},
	}
	conn := &fakeConn{udp: udp}
	receiver := &fakeOpusReceiver{}
	audioReceiver := newResilientAudioReceiver(slog.Default(), receiver, conn).(*resilientAudioReceiver)

	audioReceiver.receive()

	if receiver.calls != 1 {
		t.Fatalf("receiver.calls = %d, want 1", receiver.calls)
	}
	if audioReceiver.consecutiveDecryptErrors != 0 {
		t.Fatalf("consecutiveDecryptErrors = %d, want 0", audioReceiver.consecutiveDecryptErrors)
	}
}

type fakeReadPacketResult struct {
	packet *voice.Packet
	err    error
}

type fakeUDPConn struct {
	readPackets []fakeReadPacketResult
}

func (f *fakeUDPConn) LocalAddr() net.Addr                             { return nil }
func (f *fakeUDPConn) RemoteAddr() net.Addr                            { return nil }
func (f *fakeUDPConn) SetSecretKey(voice.EncryptionMode, []byte) error { return nil }
func (f *fakeUDPConn) SetDeadline(_ time.Time) error                   { return nil }
func (f *fakeUDPConn) SetReadDeadline(_ time.Time) error               { return nil }
func (f *fakeUDPConn) SetWriteDeadline(_ time.Time) error              { return nil }
func (f *fakeUDPConn) Open(_ context.Context, _ string, _ int, _ uint32) (string, int, error) {
	return "", 0, nil
}
func (f *fakeUDPConn) Close() error                { return nil }
func (f *fakeUDPConn) Read(_ []byte) (int, error)  { return 0, io.EOF }
func (f *fakeUDPConn) Write(_ []byte) (int, error) { return 0, nil }
func (f *fakeUDPConn) ReadPacket() (*voice.Packet, error) {
	if len(f.readPackets) == 0 {
		return nil, io.EOF
	}
	result := f.readPackets[0]
	f.readPackets = f.readPackets[1:]
	return result.packet, result.err
}

type fakeConn struct {
	udp voice.UDPConn
}

func (f *fakeConn) Gateway() voice.Gateway                                       { return nil }
func (f *fakeConn) UDP() voice.UDPConn                                           { return f.udp }
func (f *fakeConn) ChannelID() *snowflake.ID                                     { return nil }
func (f *fakeConn) GuildID() snowflake.ID                                        { return 0 }
func (f *fakeConn) UserIDBySSRC(_ uint32) snowflake.ID                           { return 0 }
func (f *fakeConn) SetSpeaking(_ context.Context, _ voice.SpeakingFlags) error   { return nil }
func (f *fakeConn) SetOpusFrameProvider(_ voice.OpusFrameProvider)               {}
func (f *fakeConn) SetOpusFrameReceiver(_ voice.OpusFrameReceiver)               {}
func (f *fakeConn) SetEventHandlerFunc(_ voice.EventHandlerFunc)                 {}
func (f *fakeConn) Open(_ context.Context, _ snowflake.ID, _ bool, _ bool) error { return nil }
func (f *fakeConn) Close(_ context.Context)                                      {}
func (f *fakeConn) HandleVoiceStateUpdate(_ botgateway.EventVoiceStateUpdate)    {}
func (f *fakeConn) HandleVoiceServerUpdate(_ botgateway.EventVoiceServerUpdate)  {}
func (f *fakeConn) SendMLSKeyPackage(_ []byte) error                             { return nil }
func (f *fakeConn) SendMLSCommitWelcome(_ []byte) error                          { return nil }
func (f *fakeConn) SendReadyForTransition(_ uint16) error                        { return nil }
func (f *fakeConn) SendInvalidCommitWelcome(_ uint16) error                      { return nil }

type fakeOpusReceiver struct {
	calls int
}

func (f *fakeOpusReceiver) ReceiveOpusFrame(_ snowflake.ID, _ *voice.Packet) error {
	f.calls++
	return nil
}

func (f *fakeOpusReceiver) CleanupUser(_ snowflake.ID) {}
func (f *fakeOpusReceiver) Close()                     {}
