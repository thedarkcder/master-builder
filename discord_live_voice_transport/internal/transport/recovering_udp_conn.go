package transport

import (
	"context"
	"encoding/binary"
	"fmt"
	"log/slog"
	"net"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/disgoorg/disgo/voice"
	"github.com/disgoorg/godave"
)

const (
	maxPendingDavePackets    = 128
	daveDecryptWarningBurst  = 16
	pendingDecryptRetryDelay = 40 * time.Millisecond
	maxPendingPacketAge      = 3 * time.Second
)

func newRecoveringUDPConnCreateFunc(logger *slog.Logger) voice.UDPConnCreateFunc {
	return func(daveSession godave.Session, ssrcLookup voice.SsrcLookupFunc, _ ...voice.UDPConnConfigOpt) voice.UDPConn {
		return newRecoveringUDPConn(logger, daveSession, ssrcLookup)
	}
}

func newRecoveringUDPConn(logger *slog.Logger, daveSession godave.Session, ssrcLookup voice.SsrcLookupFunc) voice.UDPConn {
	if logger == nil {
		logger = slog.Default()
	}
	return &recoveringUDPConn{
		logger:        logger.With(slog.String("name", "voice_udp")),
		daveSession:   daveSession,
		ssrcLookup:    ssrcLookup,
		dialer:        &net.Dialer{Timeout: voice.UDPTimeout},
		receiveBuffer: make([]byte, 1400),
		decryptBuffer: make([]byte, 512),
		encryptBuffer: make([]byte, 512),
		pending:       make([]pendingDavePacket, 0, 8),
		now:           time.Now,
	}
}

type pendingDavePacket struct {
	packet    voice.Packet
	decrypted []byte
	queuedAt  time.Time
	lastTried time.Time
}

type recoveringUDPConn struct {
	logger *slog.Logger
	dialer *net.Dialer

	conn   net.Conn
	connMu sync.Mutex

	encrypter   voice.Encrypter
	daveSession godave.Session
	ssrcLookup  voice.SsrcLookupFunc

	header    [voice.RTPHeaderSize]byte
	ssrc      uint32
	sequence  uint16
	timestamp uint32

	receiveBuffer []byte
	decryptBuffer []byte
	encryptBuffer []byte

	pending []pendingDavePacket
	now     func() time.Time
}

func (u *recoveringUDPConn) LocalAddr() net.Addr {
	u.connMu.Lock()
	defer u.connMu.Unlock()
	if u.conn == nil {
		return nil
	}
	return u.conn.LocalAddr()
}

func (u *recoveringUDPConn) RemoteAddr() net.Addr {
	u.connMu.Lock()
	defer u.connMu.Unlock()
	if u.conn == nil {
		return nil
	}
	return u.conn.RemoteAddr()
}

func (u *recoveringUDPConn) SetSecretKey(encryptionMode voice.EncryptionMode, secretKey []byte) error {
	encrypter, err := voice.NewEncrypter(encryptionMode, secretKey)
	if err != nil {
		return fmt.Errorf("failed to create encrypter: %w", err)
	}
	u.encrypter = encrypter
	return nil
}

func (u *recoveringUDPConn) SetDeadline(t time.Time) error {
	u.connMu.Lock()
	defer u.connMu.Unlock()
	if u.conn == nil {
		return net.ErrClosed
	}
	return u.conn.SetDeadline(t)
}

func (u *recoveringUDPConn) SetReadDeadline(t time.Time) error {
	u.connMu.Lock()
	defer u.connMu.Unlock()
	if u.conn == nil {
		return net.ErrClosed
	}
	return u.conn.SetReadDeadline(t)
}

func (u *recoveringUDPConn) SetWriteDeadline(t time.Time) error {
	u.connMu.Lock()
	defer u.connMu.Unlock()
	if u.conn == nil {
		return net.ErrClosed
	}
	return u.conn.SetWriteDeadline(t)
}

func (u *recoveringUDPConn) Open(ctx context.Context, ip string, port int, ssrc uint32) (string, int, error) {
	u.connMu.Lock()
	defer u.connMu.Unlock()

	host := net.JoinHostPort(ip, strconv.Itoa(port))
	u.logger.Debug("opening recovering UDPConn connection", slog.String("host", host))
	conn, err := u.dialer.DialContext(ctx, "udp", host)
	if err != nil {
		return "", 0, fmt.Errorf("failed to open UDPConn connection: %w", err)
	}
	u.conn = conn

	discovery := make([]byte, 74)
	binary.BigEndian.PutUint16(discovery[:2], 1)
	binary.BigEndian.PutUint16(discovery[2:4], 70)
	binary.BigEndian.PutUint32(discovery[4:74], ssrc)

	if err = u.conn.SetWriteDeadline(time.Now().Add(5 * time.Second)); err != nil {
		return "", 0, fmt.Errorf("failed to set write deadline on UDPConn connection: %w", err)
	}
	defer func() {
		_ = u.conn.SetWriteDeadline(time.Time{})
	}()
	if _, err = u.conn.Write(discovery); err != nil {
		return "", 0, fmt.Errorf("failed to write ssrc to UDPConn connection: %w", err)
	}

	response := make([]byte, 74)
	if err = u.conn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
		return "", 0, fmt.Errorf("failed to set read deadline on UDPConn connection: %w", err)
	}
	defer func() {
		_ = u.conn.SetReadDeadline(time.Time{})
	}()
	if _, err = u.conn.Read(response); err != nil {
		return "", 0, fmt.Errorf("failed to read ip discovery from UDPConn connection: %w", err)
	}

	if binary.BigEndian.Uint16(response[0:2]) != 2 {
		return "", 0, fmt.Errorf("invalid ip discovery response")
	}
	if size := binary.BigEndian.Uint16(response[2:4]); size != 70 {
		return "", 0, fmt.Errorf("invalid ip discovery response size")
	}
	returnedSSRC := binary.BigEndian.Uint32(response[4:8])
	if returnedSSRC != ssrc {
		return "", 0, fmt.Errorf("invalid ssrc in ip discovery response")
	}

	ourAddress := strings.TrimSpace(string(response[8:72]))
	ourPort := int(binary.BigEndian.Uint16(response[72:74]))

	u.header[0] = voice.RTPVersionPadExtend
	u.header[1] = voice.RTPPayloadType
	binary.BigEndian.PutUint32(u.header[8:], ssrc)

	u.ssrc = ssrc
	u.daveSession.AssignSsrcToCodec(ssrc, godave.CodecOpus)
	return ourAddress, ourPort, nil
}

func (u *recoveringUDPConn) Close() error {
	u.connMu.Lock()
	defer u.connMu.Unlock()
	if u.conn == nil {
		return nil
	}
	return u.conn.Close()
}

func (u *recoveringUDPConn) Read(p []byte) (int, error) {
	packet, err := u.ReadPacket()
	if err != nil {
		return 0, err
	}
	return copy(p, packet.Opus), nil
}

func (u *recoveringUDPConn) ReadPacket() (*voice.Packet, error) {
	u.connMu.Lock()
	conn := u.conn
	u.connMu.Unlock()
	if conn == nil {
		return nil, net.ErrClosed
	}

	for {
		if packet, ok := u.dequeueReadyPendingPacket(); ok {
			return packet, nil
		}

		n, err := conn.Read(u.receiveBuffer)
		if err != nil {
			return nil, fmt.Errorf("failed to read packet: %w", err)
		}
		packet, decrypted, ok, err := u.parseTransportPacket(u.receiveBuffer[:n])
		if err != nil {
			return nil, err
		}
		if !ok {
			continue
		}
		if delivered, ok := u.deliverOrQueuePendingPacket(pendingDavePacket{
			packet:    *packet,
			decrypted: append([]byte(nil), decrypted...),
		}); ok {
			return delivered, nil
		}
	}
}

func (u *recoveringUDPConn) Write(p []byte) (int, error) {
	u.connMu.Lock()
	conn := u.conn
	u.connMu.Unlock()
	if conn == nil {
		return 0, net.ErrClosed
	}

	binary.BigEndian.PutUint16(u.header[2:4], u.sequence)
	u.sequence++
	binary.BigEndian.PutUint32(u.header[4:8], u.timestamp)
	u.timestamp += voice.OpusFrameSize

	bufferCap := u.daveSession.MaxEncryptedFrameSize(len(p))
	if cap(u.encryptBuffer) < bufferCap {
		u.encryptBuffer = slices.Grow(u.encryptBuffer, bufferCap)
	}
	n, err := u.daveSession.Encrypt(u.ssrc, p, u.encryptBuffer)
	if err != nil {
		return 0, fmt.Errorf("failed to encrypt packet: %w", err)
	}
	if _, err = conn.Write(u.encrypter.Encrypt(u.header, u.encryptBuffer[:n])); err != nil {
		return 0, fmt.Errorf("failed to write packet: %w", err)
	}
	return len(p), nil
}

func (u *recoveringUDPConn) parseTransportPacket(payload []byte) (*voice.Packet, []byte, bool, error) {
	n := len(payload)
	if n < voice.RTPHeaderSize {
		return nil, nil, false, nil
	}

	packetType := payload[1]
	if packetType != voice.RTPPayloadType {
		return nil, nil, false, nil
	}

	if hasPadding := (payload[0] & 0x04) != 0; hasPadding {
		paddingLen := int(payload[n-1])
		if paddingLen <= 0 || paddingLen > n-voice.RTPHeaderSize {
			return nil, nil, false, nil
		}
		n -= paddingLen
		payload = payload[:n]
	}

	packet := voice.Packet{
		Type:         packetType,
		Sequence:     binary.BigEndian.Uint16(payload[2:4]),
		Timestamp:    binary.BigEndian.Uint32(payload[4:8]),
		SSRC:         binary.BigEndian.Uint32(payload[8:voice.RTPHeaderSize]),
		HasExtension: (payload[0] & 0x10) != 0,
	}

	cc := int(payload[0] & 0x0F)
	headerLen := voice.RTPHeaderSize + (4 * cc)
	if n < headerLen {
		return nil, nil, false, nil
	}

	packet.CSRC = make([]uint32, cc)
	for i := range cc {
		packet.CSRC[i] = binary.BigEndian.Uint32(payload[voice.RTPHeaderSize+i*4 : voice.RTPHeaderSize+i*4+4])
	}

	var extensionLenWords uint16
	if packet.HasExtension {
		if n < headerLen+4 {
			return nil, nil, false, nil
		}
		packet.ExtensionID = int(binary.BigEndian.Uint16(payload[headerLen : headerLen+2]))
		extensionLenWords = binary.BigEndian.Uint16(payload[headerLen+2 : headerLen+4])
		headerLen += 4
	}

	packet.HeaderSize = headerLen
	if n < packet.HeaderSize+4 {
		return nil, nil, false, nil
	}

	decrypted, err := u.encrypter.Decrypt(packet.HeaderSize, payload)
	if err != nil {
		return nil, nil, false, fmt.Errorf("failed to decrypt packet: %w", err)
	}

	decryptedOffset := 0
	if packet.HasExtension {
		extensionLen := int(extensionLenWords) * 4
		if decryptedOffset+extensionLen > len(decrypted) {
			return nil, nil, false, nil
		}
		packet.Extension = append([]byte(nil), decrypted[decryptedOffset:decryptedOffset+extensionLen]...)
		decryptedOffset += extensionLen
	}

	return &packet, append([]byte(nil), decrypted[decryptedOffset:]...), true, nil
}

func (u *recoveringUDPConn) dequeueReadyPendingPacket() (*voice.Packet, bool) {
	for idx := 0; idx < len(u.pending); idx++ {
		candidate := u.pending[idx]
		delivered, keep := u.tryDecryptCandidate(&candidate)
		if keep {
			u.pending[idx] = candidate
			continue
		}
		u.pending = append(u.pending[:idx], u.pending[idx+1:]...)
		idx--
		if delivered != nil {
			return delivered, true
		}
	}
	return nil, false
}

func (u *recoveringUDPConn) deliverOrQueuePendingPacket(candidate pendingDavePacket) (*voice.Packet, bool) {
	if candidate.queuedAt.IsZero() {
		candidate.queuedAt = u.now()
	}
	delivered, keep := u.tryDecryptCandidate(&candidate)
	if keep {
		u.enqueuePendingPacket(candidate)
		return nil, false
	}
	if delivered == nil {
		return nil, false
	}
	return delivered, true
}

func (u *recoveringUDPConn) tryDecryptCandidate(candidate *pendingDavePacket) (*voice.Packet, bool) {
	now := u.now()
	if candidate.queuedAt.IsZero() {
		candidate.queuedAt = now
	}
	if now.Sub(candidate.queuedAt) > maxPendingPacketAge {
		u.logger.Warn(
			"dropping stale pending DAVE packet while awaiting speaker readiness",
			slog.Uint64("ssrc", uint64(candidate.packet.SSRC)),
			slog.Duration("age", now.Sub(candidate.queuedAt)),
		)
		return nil, false
	}
	if !candidate.lastTried.IsZero() && now.Sub(candidate.lastTried) < pendingDecryptRetryDelay {
		return nil, true
	}
	candidate.lastTried = now

	userID := u.ssrcLookup(candidate.packet.SSRC)
	if userID == 0 {
		return nil, true
	}

	bufferCap := u.daveSession.MaxDecryptedFrameSize(godave.UserID(userID.String()), len(candidate.decrypted))
	if cap(u.decryptBuffer) < bufferCap {
		u.decryptBuffer = slices.Grow(u.decryptBuffer, bufferCap)
	}

	n, err := u.daveSession.Decrypt(godave.UserID(userID.String()), candidate.decrypted, u.decryptBuffer)
	if err == nil {
		packet := candidate.packet
		packet.Opus = u.decryptBuffer[:n]
		return &packet, false
	}

	if isRetryableDaveDecryptError(err) {
		return nil, true
	}

	u.logger.Warn(
		"dropping voice packet after non-retryable DAVE decrypt failure",
		slog.String("user_id", userID.String()),
		slog.Uint64("ssrc", uint64(candidate.packet.SSRC)),
		slog.Duration("age", now.Sub(candidate.queuedAt)),
		slog.Any("error", err),
	)
	return nil, false
}

func (u *recoveringUDPConn) enqueuePendingPacket(candidate pendingDavePacket) {
	if len(u.pending) >= maxPendingDavePackets {
		dropped := u.pending[0]
		u.pending = u.pending[1:]
		u.logger.Warn(
			"dropping oldest pending DAVE packet while awaiting speaker readiness",
			slog.Uint64("ssrc", uint64(dropped.packet.SSRC)),
			slog.Duration("age", u.now().Sub(dropped.queuedAt)),
		)
	}
	u.pending = append(u.pending, candidate)
	if len(u.pending) == daveDecryptWarningBurst {
		u.logger.Warn(
			"buffering DAVE packets while awaiting speaker mapping or cryptor readiness",
			slog.Int("pending_packets", len(u.pending)),
		)
	}
}

func isRetryableDaveDecryptError(err error) bool {
	if err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "failed to decrypt frame") || strings.Contains(message, "failed to dave decrypt packet")
}
