package transport

import (
	"errors"
	"log/slog"
	"testing"
	"time"

	"discord_live_voice_transport/internal/protocol"

	"github.com/disgoorg/disgo/voice"
	"github.com/disgoorg/godave"
	"github.com/disgoorg/snowflake/v2"
)

func TestRecoveringUDPConnQueuesUntilSSRCMappingExists(t *testing.T) {
	t.Parallel()

	var mappedUserID snowflake.ID
	conn := newRecoveringUDPConn(
		slog.Default(),
		&fakeDaveSession{},
		func(_ uint32) snowflake.ID { return mappedUserID },
	).(*recoveringUDPConn)
	now := time.Unix(1, 0)
	conn.now = func() time.Time { return now }

	delivered, ok := conn.deliverOrQueuePendingPacket(pendingDavePacket{
		packet:    voice.Packet{SSRC: 42},
		decrypted: []byte("opus"),
	})
	if ok || delivered != nil {
		t.Fatal("expected packet to remain pending while speaker mapping is unknown")
	}
	if len(conn.pending) != 1 {
		t.Fatalf("len(conn.pending) = %d, want 1", len(conn.pending))
	}

	mappedUserID = snowflake.ID(1234)
	now = now.Add(pendingDecryptRetryDelay)
	delivered, ok = conn.dequeueReadyPendingPacket()
	if !ok || delivered == nil {
		t.Fatal("expected pending packet to decrypt once the speaker mapping exists")
	}
	if got := string(delivered.Opus); got != "opus" {
		t.Fatalf("delivered.Opus = %q, want %q", got, "opus")
	}
	if len(conn.pending) != 0 {
		t.Fatalf("len(conn.pending) = %d, want 0", len(conn.pending))
	}
}

func TestRecoveringUDPConnRetriesTransientDaveDecryptFailures(t *testing.T) {
	t.Parallel()

	decryptCalls := 0
	conn := newRecoveringUDPConn(
		slog.Default(),
		&fakeDaveSession{
			decryptFunc: func(_ godave.UserID, frame []byte, decryptedFrame []byte) (int, error) {
				decryptCalls++
				if decryptCalls == 1 {
					return 0, errors.New("failed to decrypt frame")
				}
				copy(decryptedFrame, frame)
				return len(frame), nil
			},
		},
		func(_ uint32) snowflake.ID { return snowflake.ID(1234) },
	).(*recoveringUDPConn)
	now := time.Unix(1, 0)
	conn.now = func() time.Time { return now }

	delivered, ok := conn.deliverOrQueuePendingPacket(pendingDavePacket{
		packet:    voice.Packet{SSRC: 42},
		decrypted: []byte("opus"),
	})
	if ok || delivered != nil {
		t.Fatal("expected first transient decrypt failure to keep the packet pending")
	}
	if len(conn.pending) != 1 {
		t.Fatalf("len(conn.pending) = %d, want 1", len(conn.pending))
	}

	now = now.Add(pendingDecryptRetryDelay)
	delivered, ok = conn.dequeueReadyPendingPacket()
	if !ok || delivered == nil {
		t.Fatal("expected pending packet to decrypt on retry")
	}
	if decryptCalls != 2 {
		t.Fatalf("decryptCalls = %d, want 2", decryptCalls)
	}
	if got := string(delivered.Opus); got != "opus" {
		t.Fatalf("delivered.Opus = %q, want %q", got, "opus")
	}
}

func TestRecoveringUDPConnRetainsRetryablePacketsUntilReady(t *testing.T) {
	t.Parallel()

	decryptCalls := 0
	conn := newRecoveringUDPConn(
		slog.Default(),
		&fakeDaveSession{
			decryptFunc: func(_ godave.UserID, frame []byte, decryptedFrame []byte) (int, error) {
				decryptCalls++
				if decryptCalls <= 12 {
					return 0, errors.New("failed to decrypt frame")
				}
				copy(decryptedFrame, frame)
				return len(frame), nil
			},
		},
		func(_ uint32) snowflake.ID { return snowflake.ID(1234) },
	).(*recoveringUDPConn)
	now := time.Unix(1, 0)
	conn.now = func() time.Time { return now }

	delivered, ok := conn.deliverOrQueuePendingPacket(pendingDavePacket{
		packet:    voice.Packet{SSRC: 42},
		decrypted: []byte("opus"),
	})
	if ok || delivered != nil {
		t.Fatal("expected packet to remain pending while cryptor readiness is delayed")
	}

	for i := 0; i < 11; i++ {
		now = now.Add(pendingDecryptRetryDelay)
		delivered, ok = conn.dequeueReadyPendingPacket()
		if ok || delivered != nil {
			t.Fatal("expected packet to remain pending until decrypt finally succeeds")
		}
		if len(conn.pending) != 1 {
			t.Fatalf("len(conn.pending) = %d, want 1", len(conn.pending))
		}
	}

	now = now.Add(pendingDecryptRetryDelay)
	delivered, ok = conn.dequeueReadyPendingPacket()
	if !ok || delivered == nil {
		t.Fatal("expected delayed cryptor readiness to eventually deliver the packet")
	}
	if decryptCalls != 13 {
		t.Fatalf("decryptCalls = %d, want 13", decryptCalls)
	}
}

func TestRecoveringUDPConnDropsStalePendingPackets(t *testing.T) {
	t.Parallel()

	conn := newRecoveringUDPConn(
		slog.Default(),
		&fakeDaveSession{
			decryptFunc: func(_ godave.UserID, _ []byte, _ []byte) (int, error) {
				return 0, errors.New("failed to decrypt frame")
			},
		},
		func(_ uint32) snowflake.ID { return snowflake.ID(1234) },
	).(*recoveringUDPConn)
	now := time.Unix(1, 0)
	conn.now = func() time.Time { return now }

	delivered, ok := conn.deliverOrQueuePendingPacket(pendingDavePacket{
		packet:    voice.Packet{SSRC: 42},
		decrypted: []byte("opus"),
	})
	if ok || delivered != nil {
		t.Fatal("expected packet to remain pending while decrypt keeps failing")
	}
	if len(conn.pending) != 1 {
		t.Fatalf("len(conn.pending) = %d, want 1", len(conn.pending))
	}

	now = now.Add(maxPendingPacketAge + time.Millisecond)
	delivered, ok = conn.dequeueReadyPendingPacket()
	if ok || delivered != nil {
		t.Fatal("expected stale pending packet to be dropped")
	}
	if len(conn.pending) != 0 {
		t.Fatalf("len(conn.pending) = %d, want 0", len(conn.pending))
	}
}

func TestOpusSinkIgnoresUnknownUsers(t *testing.T) {
	t.Parallel()

	events := newEventSink(1)
	sink := newOpusSink(events, "session-a", protocol.RoomRef{GuildID: "guild-a", ChannelID: "channel-a"})

	if err := sink.ReceiveOpusFrame(0, &voice.Packet{SSRC: 42, Opus: []byte("opus")}); err != nil {
		t.Fatalf("ReceiveOpusFrame returned error: %v", err)
	}

	select {
	case frame := <-events.Events():
		t.Fatalf("unexpected emitted frame: %#v", frame)
	default:
	}
}

type fakeDaveSession struct {
	decryptFunc func(userID godave.UserID, frame []byte, decryptedFrame []byte) (int, error)
}

func (f *fakeDaveSession) MaxSupportedProtocolVersion() int { return 1 }

func (f *fakeDaveSession) SetChannelID(_ godave.ChannelID) {}

func (f *fakeDaveSession) AssignSsrcToCodec(_ uint32, _ godave.Codec) {}

func (f *fakeDaveSession) MaxEncryptedFrameSize(frameSize int) int { return frameSize }

func (f *fakeDaveSession) Encrypt(_ uint32, frame []byte, encryptedFrame []byte) (int, error) {
	copy(encryptedFrame, frame)
	return len(frame), nil
}

func (f *fakeDaveSession) MaxDecryptedFrameSize(_ godave.UserID, frameSize int) int { return frameSize }

func (f *fakeDaveSession) Decrypt(userID godave.UserID, frame []byte, decryptedFrame []byte) (int, error) {
	if f.decryptFunc != nil {
		return f.decryptFunc(userID, frame, decryptedFrame)
	}
	copy(decryptedFrame, frame)
	return len(frame), nil
}

func (f *fakeDaveSession) AddUser(_ godave.UserID) {}

func (f *fakeDaveSession) RemoveUser(_ godave.UserID) {}

func (f *fakeDaveSession) OnSelectProtocolAck(_ uint16) {}

func (f *fakeDaveSession) OnDavePrepareTransition(_ uint16, _ uint16) {}

func (f *fakeDaveSession) OnDaveExecuteTransition(_ uint16) {}

func (f *fakeDaveSession) OnDavePrepareEpoch(_ int, _ uint16) {}

func (f *fakeDaveSession) OnDaveMLSExternalSenderPackage(_ []byte) {}

func (f *fakeDaveSession) OnDaveMLSProposals(_ []byte) {}

func (f *fakeDaveSession) OnDaveMLSPrepareCommitTransition(_ uint16, _ []byte) {}

func (f *fakeDaveSession) OnDaveMLSWelcome(_ uint16, _ []byte) {}
