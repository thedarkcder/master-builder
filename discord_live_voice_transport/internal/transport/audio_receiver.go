package transport

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"strings"
	"time"

	"github.com/disgoorg/disgo/voice"
	"github.com/disgoorg/snowflake/v2"
)

const transientDaveDecryptWarnThreshold = 25

func newResilientAudioReceiver(logger *slog.Logger, opusReceiver voice.OpusFrameReceiver, conn voice.Conn) voice.AudioReceiver {
	return &resilientAudioReceiver{
		logger:       logger,
		opusReceiver: opusReceiver,
		conn:         conn,
	}
}

type resilientAudioReceiver struct {
	logger       *slog.Logger
	cancelFunc   context.CancelFunc
	opusReceiver voice.OpusFrameReceiver
	conn         voice.Conn

	consecutiveDecryptErrors int
	lastDecryptWarningAt     time.Time
}

func (r *resilientAudioReceiver) Open() {
	go r.open()
}

func (r *resilientAudioReceiver) open() {
	defer r.logger.Debug("closing resilient audio receiver")
	ctx, cancel := context.WithCancel(context.Background())
	r.cancelFunc = cancel
	defer cancel()
	for {
		select {
		case <-ctx.Done():
			return
		default:
			r.receive()
		}
	}
}

func (r *resilientAudioReceiver) CleanupUser(userID snowflake.ID) {
	r.opusReceiver.CleanupUser(userID)
}

func (r *resilientAudioReceiver) receive() {
	packet, err := r.conn.UDP().ReadPacket()
	if errors.Is(err, net.ErrClosed) {
		r.Close()
		return
	}
	if err != nil {
		if isTransientDaveDecryptError(err) {
			r.recordTransientDecryptError(err)
			return
		}
		r.consecutiveDecryptErrors = 0
		r.logger.Error("error while reading packet", slog.Any("err", err))
		return
	}

	r.consecutiveDecryptErrors = 0
	if r.opusReceiver != nil {
		if err = r.opusReceiver.ReceiveOpusFrame(r.conn.UserIDBySSRC(packet.SSRC), packet); err != nil {
			r.logger.Error("error while receiving opus frame", slog.Any("err", err))
		}
	}
}

func (r *resilientAudioReceiver) Close() {
	if r.cancelFunc != nil {
		r.cancelFunc()
	}
	if r.opusReceiver != nil {
		r.opusReceiver.Close()
	}
}

func (r *resilientAudioReceiver) recordTransientDecryptError(err error) {
	r.consecutiveDecryptErrors++
	now := time.Now()
	if r.consecutiveDecryptErrors < transientDaveDecryptWarnThreshold {
		return
	}
	if !r.lastDecryptWarningAt.IsZero() && now.Sub(r.lastDecryptWarningAt) < 5*time.Second {
		return
	}
	r.lastDecryptWarningAt = now
	r.logger.Warn(
		"transient DAVE decrypt failures while awaiting recoverable receive state",
		slog.Int("consecutive_errors", r.consecutiveDecryptErrors),
		slog.Any("err", err),
	)
}

func isTransientDaveDecryptError(err error) bool {
	if err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "failed to dave decrypt packet") || strings.Contains(message, "failed to decrypt frame")
}
