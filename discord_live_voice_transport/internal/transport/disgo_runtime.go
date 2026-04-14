package transport

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"discord_live_voice_transport/internal/protocol"

	"github.com/disgoorg/disgo"
	"github.com/disgoorg/disgo/bot"
	"github.com/disgoorg/disgo/cache"
	"github.com/disgoorg/disgo/discord"
	"github.com/disgoorg/disgo/events"
	"github.com/disgoorg/disgo/gateway"
	"github.com/disgoorg/disgo/voice"
	"github.com/disgoorg/snowflake/v2"
)

type DisgoRuntime struct {
	logger *slog.Logger
	events *eventSink

	mu            sync.Mutex
	session       protocol.Frame
	client        *bot.Client
	rooms         *roomTracker
	conns         map[protocol.RoomRef]voice.Conn
	connecting    map[protocol.RoomRef]context.CancelFunc
	roomOps       map[protocol.RoomRef]uint64
	userRooms     map[string]protocol.RoomRef
	retryAttempts map[protocol.RoomRef]int
	retryTimers   map[protocol.RoomRef]*time.Timer
	playback      map[protocol.RoomRef]context.CancelFunc
}

var errVoiceConnOpening = errors.New("voice conn opening")

type roomOperationError struct {
	stage     string
	retryable bool
	err       error
}

func (e *roomOperationError) Error() string {
	return e.err.Error()
}

func (e *roomOperationError) Unwrap() error {
	return e.err
}

func NewDisgoRuntime(logger *slog.Logger) *DisgoRuntime {
	if logger == nil {
		logger = slog.Default()
	}
	return &DisgoRuntime{
		logger:        logger,
		events:        newEventSink(256),
		conns:         make(map[protocol.RoomRef]voice.Conn),
		connecting:    make(map[protocol.RoomRef]context.CancelFunc),
		roomOps:       make(map[protocol.RoomRef]uint64),
		userRooms:     make(map[string]protocol.RoomRef),
		retryAttempts: make(map[protocol.RoomRef]int),
		retryTimers:   make(map[protocol.RoomRef]*time.Timer),
		playback:      make(map[protocol.RoomRef]context.CancelFunc),
	}
}

func (r *DisgoRuntime) Events() <-chan protocol.Frame {
	return r.events.Events()
}

func (r *DisgoRuntime) OpenSession(ctx context.Context, req protocol.Frame) error {
	if err := req.Validate(); err != nil {
		return err
	}

	r.mu.Lock()
	if r.client != nil {
		r.mu.Unlock()
		err := errors.New("session already open")
		r.emitFailure(req.SessionID, "open_session", err.Error(), false)
		return err
	}
	r.session = req
	r.rooms = newRoomTracker(req.Rooms)
	r.userRooms = make(map[string]protocol.RoomRef)
	r.retryAttempts = make(map[protocol.RoomRef]int)
	r.retryTimers = make(map[protocol.RoomRef]*time.Timer)
	r.playback = make(map[protocol.RoomRef]context.CancelFunc)
	r.mu.Unlock()

	cfg, err := disgo.New(
		req.Token,
		bot.WithLogger(r.logger),
		bot.WithGatewayConfigOpts(
			gateway.WithIntents(
				gateway.IntentGuilds,
				gateway.IntentGuildMembers,
				gateway.IntentGuildVoiceStates,
			),
		),
		bot.WithCacheConfigOpts(
			cache.WithVoiceStateCachePolicy(cache.PolicyAll[discord.VoiceState]),
		),
		bot.WithVoiceManagerConfigOpts(
			voice.WithDaveSessionCreateFunc(newDaveSession),
			voice.WithConnConfigOpts(
				voice.WithUDPConnCreateFunc(newRecoveringUDPConnCreateFunc(r.logger)),
				voice.WithConnAudioReceiverCreateFunc(newResilientAudioReceiver),
				voice.WithConnEventHandlerFunc(func(_ voice.Gateway, op voice.Opcode, sequenceNumber int, data voice.GatewayMessageData) {
					if op != voice.OpcodeSpeaking {
						return
					}
					speaking, ok := data.(voice.GatewayMessageDataSpeaking)
					if !ok {
						return
					}
					r.logger.Debug(
						"discord_live_voice_transport_gateway_speaking",
						slog.Int("sequence_number", sequenceNumber),
						slog.String("user_id", speaking.UserID.String()),
						slog.Uint64("ssrc", uint64(speaking.SSRC)),
					)
				}),
			),
		),
		bot.WithEventListenerFunc(r.handleVoiceServerUpdate),
		bot.WithEventListenerFunc(r.handleGuildVoiceStateUpdate),
	)
	if err != nil {
		r.emitFailure(req.SessionID, "client_new", err.Error(), false)
		return err
	}

	r.mu.Lock()
	r.client = cfg
	r.mu.Unlock()

	if err := cfg.OpenGateway(ctx); err != nil {
		r.emitFailure(req.SessionID, "open_gateway", err.Error(), false)
		cfg.Close(ctx)
		r.mu.Lock()
		r.client = nil
		r.mu.Unlock()
		return err
	}

	if err := r.reconcileAll(ctx); err != nil {
		r.emitFailure(req.SessionID, "reconcile", err.Error(), false)
	}

	clientID := ""
	if id := cfg.ID(); id != 0 {
		clientID = id.String()
	}
	r.events.emit(protocol.Ready(req.SessionID, "disgo", clientID))
	return nil
}

func (r *DisgoRuntime) CloseSession(ctx context.Context, req protocol.Frame) error {
	if req.SessionID != "" && !r.sessionMatches(req.SessionID) {
		err := fmt.Errorf("session %q is not open", req.SessionID)
		r.emitFailure(req.SessionID, "close_session", err.Error(), false)
		return err
	}

	r.mu.Lock()
	client := r.client
	if client == nil {
		r.mu.Unlock()
		return nil
	}
	r.client = nil
	r.rooms = nil
	conns := r.conns
	r.conns = make(map[protocol.RoomRef]voice.Conn)
	connecting := r.connecting
	r.connecting = make(map[protocol.RoomRef]context.CancelFunc)
	retryTimers := r.retryTimers
	r.retryTimers = make(map[protocol.RoomRef]*time.Timer)
	playback := r.playback
	r.playback = make(map[protocol.RoomRef]context.CancelFunc)
	r.userRooms = make(map[string]protocol.RoomRef)
	r.mu.Unlock()

	for room, conn := range conns {
		if conn != nil {
			conn.Close(ctx)
			r.events.emit(protocol.RoomUpdated(req.SessionID, room, 0, nil, false))
		}
	}
	for _, cancel := range connecting {
		if cancel != nil {
			cancel()
		}
	}
	for _, timer := range retryTimers {
		if timer != nil {
			timer.Stop()
		}
	}
	for _, cancel := range playback {
		if cancel != nil {
			cancel()
		}
	}

	client.Close(ctx)
	return nil
}

func (r *DisgoRuntime) PlayAudio(ctx context.Context, req protocol.Frame) error {
	if err := req.Validate(); err != nil {
		return err
	}
	if !r.sessionMatches(req.SessionID) {
		err := fmt.Errorf("session %q is not open", req.SessionID)
		r.emitFailure(req.SessionID, "play_audio", err.Error(), false)
		return err
	}

	room := *req.Room
	conn, err := r.ensureConn(ctx, room)
	if err != nil {
		r.emitFailure(req.SessionID, "ensure_voice_conn", err.Error(), false)
		return err
	}

	frames := req.OpusFramesBase64
	if req.OpusFrameBase64 != "" {
		frames = append(frames, req.OpusFrameBase64)
	}

	if err := conn.SetSpeaking(ctx, voice.SpeakingFlagMicrophone); err != nil {
		r.emitFailure(req.SessionID, "set_speaking", err.Error(), false)
		return err
	}

	decoded := make([][]byte, 0, len(frames))
	for _, encoded := range frames {
		frame, decodeErr := base64.StdEncoding.DecodeString(encoded)
		if decodeErr != nil {
			err = decodeErr
			r.emitFailure(req.SessionID, "decode_audio", err.Error(), false)
			return err
		}
		decoded = append(decoded, frame)
	}

	r.startPlayback(room, req.SessionID, conn, decoded)
	return nil
}

func (r *DisgoRuntime) StopAudio(ctx context.Context, req protocol.Frame) error {
	if err := req.Validate(); err != nil {
		return err
	}
	if !r.sessionMatches(req.SessionID) {
		err := fmt.Errorf("session %q is not open", req.SessionID)
		r.emitFailure(req.SessionID, "stop_audio", err.Error(), false)
		return err
	}
	if req.Room == nil {
		return errors.New("stop_audio requires room")
	}
	r.stopPlayback(*req.Room)
	return nil
}

func (r *DisgoRuntime) sessionMatches(sessionID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.session.SessionID == sessionID && r.client != nil
}

func (r *DisgoRuntime) emitFailure(sessionID, stage string, reason string, retryable bool) {
	r.events.emit(protocol.Failed(sessionID, stage, reason, retryable))
}

func (r *DisgoRuntime) reconcileAll(ctx context.Context) error {
	r.mu.Lock()
	rooms := make([]protocol.RoomRef, 0, len(r.rooms.rooms))
	for room := range r.rooms.rooms {
		rooms = append(rooms, room)
	}
	r.mu.Unlock()

	for _, room := range rooms {
		if err := r.reconcileRoom(ctx, room); err != nil {
			return err
		}
	}
	return nil
}

func (r *DisgoRuntime) reconcileRoom(ctx context.Context, room protocol.RoomRef) error {
	memberIDs, err := r.humanMemberIDs(room)
	if err != nil {
		return err
	}
	if len(memberIDs) <= 0 {
		return nil
	}
	r.applyObservedRoomMembers(room, memberIDs)
	return nil
}

func (r *DisgoRuntime) ensureConn(ctx context.Context, room protocol.RoomRef) (voice.Conn, error) {
	r.mu.Lock()
	if conn := r.conns[room]; conn != nil {
		r.mu.Unlock()
		return conn, nil
	}
	if _, opening := r.connecting[room]; opening {
		r.mu.Unlock()
		return nil, errVoiceConnOpening
	}
	client := r.client
	r.mu.Unlock()
	if client == nil {
		return nil, &roomOperationError{stage: "ensure_voice_conn", retryable: false, err: errors.New("client is not open")}
	}

	guildID, err := snowflake.Parse(room.GuildID)
	if err != nil {
		return nil, &roomOperationError{stage: "ensure_voice_conn", retryable: false, err: fmt.Errorf("parse guild_id: %w", err)}
	}
	channelID, err := snowflake.Parse(room.ChannelID)
	if err != nil {
		return nil, &roomOperationError{stage: "ensure_voice_conn", retryable: false, err: fmt.Errorf("parse channel_id: %w", err)}
	}

	conn := client.VoiceManager.CreateConn(guildID)

	r.mu.Lock()
	if existing := r.conns[room]; existing != nil {
		r.mu.Unlock()
		return existing, nil
	}
	if _, opening := r.connecting[room]; opening {
		r.mu.Unlock()
		return nil, errVoiceConnOpening
	}
	openCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	r.connecting[room] = cancel
	r.mu.Unlock()

	r.logger.Info(
		"discord_live_voice_transport_open_voice_conn_started",
		slog.String("guild_id", room.GuildID),
		slog.String("channel_id", room.ChannelID),
	)
	defer cancel()
	if err := conn.Open(openCtx, channelID, false, false); err != nil {
		r.logger.Error(
			"discord_live_voice_transport_open_voice_conn_failed",
			slog.String("guild_id", room.GuildID),
			slog.String("channel_id", room.ChannelID),
			slog.Any("error", err),
		)
		r.mu.Lock()
		delete(r.connecting, room)
		currentClient := r.client
		r.mu.Unlock()
		if currentClient != nil && currentClient.VoiceManager != nil {
			currentClient.VoiceManager.RemoveConn(guildID)
		}
		return nil, &roomOperationError{stage: "open_voice_conn", retryable: true, err: err}
	}
	r.mu.Lock()
	delete(r.connecting, room)
	r.conns[room] = conn
	r.mu.Unlock()
	r.logger.Info(
		"discord_live_voice_transport_open_voice_conn_succeeded",
		slog.String("guild_id", room.GuildID),
		slog.String("channel_id", room.ChannelID),
	)
	conn.SetOpusFrameReceiver(newOpusSink(r.events, r.session.SessionID, room))

	return conn, nil
}

func (r *DisgoRuntime) humanMemberIDs(room protocol.RoomRef) ([]string, error) {
	client := r.clientSnapshot()
	if client == nil {
		return nil, errors.New("client is not open")
	}

	guildID, err := snowflake.Parse(room.GuildID)
	if err != nil {
		return nil, fmt.Errorf("parse guild_id: %w", err)
	}
	channelID, err := snowflake.Parse(room.ChannelID)
	if err != nil {
		return nil, fmt.Errorf("parse channel_id: %w", err)
	}

	memberIDs := make(map[string]struct{})
	for state := range client.Caches.VoiceStates(guildID) {
		if state.ChannelID != nil && *state.ChannelID == channelID {
			if member, ok := client.Caches.Member(guildID, state.UserID); ok && member.User.Bot {
				continue
			}
			memberIDs[state.UserID.String()] = struct{}{}
		}
	}
	return memberSetKeys(memberIDs), nil
}

func (r *DisgoRuntime) clientSnapshot() *bot.Client {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.client
}

func (r *DisgoRuntime) handleGuildVoiceStateUpdate(event *events.GuildVoiceStateUpdate) {
	if event == nil {
		return
	}
	userID := event.VoiceState.UserID.String()
	guildID := event.VoiceState.GuildID.String()
	oldChannelID := ""
	if event.OldVoiceState.ChannelID != nil {
		oldChannelID = event.OldVoiceState.ChannelID.String()
	}
	newChannelID := ""
	if event.VoiceState.ChannelID != nil {
		newChannelID = event.VoiceState.ChannelID.String()
	}
	if client := r.clientSnapshot(); client != nil && event.VoiceState.UserID == client.ID() {
		r.logger.Info(
			"discord_live_voice_transport_bot_voice_state_update",
			slog.String("guild_id", guildID),
			slog.String("old_channel_id", oldChannelID),
			slog.String("new_channel_id", newChannelID),
			slog.String("user_id", userID),
			slog.String("session_id", event.VoiceState.SessionID),
		)
	}
	if r.shouldIgnoreVoiceState(event) {
		return
	}
	r.logger.Info(
		"discord_live_voice_transport_voice_state_update",
		slog.String("guild_id", guildID),
		slog.String("old_channel_id", oldChannelID),
		slog.String("new_channel_id", newChannelID),
		slog.String("user_id", userID),
	)
	var nextRoom *protocol.RoomRef
	if newChannelID != "" {
		if room, ok := r.roomForChannel(guildID, newChannelID); ok {
			nextRoom = &room
		}
	}
	r.transitionTrackedUser(userID, nextRoom)
}

func (r *DisgoRuntime) handleVoiceServerUpdate(event *events.VoiceServerUpdate) {
	if event == nil || event.Endpoint == nil {
		return
	}
	r.logger.Info(
		"discord_live_voice_transport_voice_server_update",
		slog.String("guild_id", event.GuildID.String()),
		slog.String("endpoint", *event.Endpoint),
	)
}

func (r *DisgoRuntime) roomForChannel(guildID string, channelID string) (protocol.RoomRef, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.rooms == nil {
		return protocol.RoomRef{}, false
	}
	for room := range r.rooms.rooms {
		if room.GuildID == guildID && room.ChannelID == channelID {
			return room, true
		}
	}
	return protocol.RoomRef{}, false
}

func (r *DisgoRuntime) shouldIgnoreVoiceState(event *events.GuildVoiceStateUpdate) bool {
	if event.Member.User.ID != 0 {
		return event.Member.User.Bot
	}
	client := r.clientSnapshot()
	if client == nil {
		return false
	}
	if client.ID() == event.VoiceState.UserID {
		return true
	}
	member, ok := client.Caches.Member(event.VoiceState.GuildID, event.VoiceState.UserID)
	return ok && member.User.Bot
}

func (r *DisgoRuntime) refreshTrackedRoomMembers(room protocol.RoomRef) {
	memberIDs, err := r.humanMemberIDs(room)
	if err != nil {
		r.emitFailure(r.session.SessionID, "refresh_room_members", err.Error(), false)
		return
	}
	r.applyObservedRoomMembers(room, memberIDs)
}

func (r *DisgoRuntime) transitionTrackedUser(userID string, nextRoom *protocol.RoomRef) {
	if userID == "" {
		return
	}

	type roomUpdate struct {
		room    protocol.RoomRef
		state   roomState
		changed bool
	}

	var updates []roomUpdate

	r.mu.Lock()
	if r.rooms == nil {
		r.mu.Unlock()
		return
	}
	previousRoom, hadPrevious := r.userRooms[userID]
	if hadPrevious && (nextRoom == nil || previousRoom != *nextRoom) {
		_, current, changed := r.rooms.removeHumanMember(previousRoom, userID)
		if changed {
			updates = append(updates, roomUpdate{room: previousRoom, state: current, changed: true})
		}
		delete(r.userRooms, userID)
	}
	if nextRoom != nil {
		r.userRooms[userID] = *nextRoom
		if !hadPrevious || previousRoom != *nextRoom {
			_, current, changed := r.rooms.addHumanMember(*nextRoom, userID)
			if changed {
				updates = append(updates, roomUpdate{room: *nextRoom, state: current, changed: true})
			}
		}
	}
	r.mu.Unlock()

	for _, update := range updates {
		r.logRoomMembership(update.room, update.state)
		r.scheduleApplyRoomCount(update.room, update.state.humanCount)
	}
}

func (r *DisgoRuntime) applyObservedRoomMembers(room protocol.RoomRef, memberIDs []string) {
	r.mu.Lock()
	if r.rooms == nil {
		r.mu.Unlock()
		return
	}
	previousMemberIDs := r.rooms.memberIDs(room)
	_, current, changed := r.rooms.setHumanMembers(room, memberIDs)
	for _, previousMemberID := range previousMemberIDs {
		if trackedRoom, ok := r.userRooms[previousMemberID]; ok && trackedRoom == room {
			delete(r.userRooms, previousMemberID)
		}
	}
	for _, currentMemberID := range memberSetKeys(current.humanMemberIDs) {
		r.userRooms[currentMemberID] = room
	}
	r.mu.Unlock()
	if !changed {
		return
	}
	r.logRoomMembership(room, current)
	r.scheduleApplyRoomCount(room, current.humanCount)
}

func (r *DisgoRuntime) scheduleApplyRoomCount(room protocol.RoomRef, humans int) {
	r.mu.Lock()
	r.roomOps[room]++
	op := r.roomOps[room]
	r.mu.Unlock()

	go r.applyRoomCountAsync(room, humans, op)
}

func (r *DisgoRuntime) applyRoomCountAsync(room protocol.RoomRef, humans int, op uint64) {
	if !r.isCurrentRoomOp(room, op) {
		return
	}
	joined, err := r.applyRoomCount(context.Background(), room, humans)
	if err != nil {
		if errors.Is(err, errVoiceConnOpening) || errors.Is(err, context.Canceled) {
			return
		}
		var roomErr *roomOperationError
		if errors.As(err, &roomErr) {
			if roomErr.retryable && humans > 0 && r.scheduleRetry(room, humans, op) {
				if r.isCurrentRoomOp(room, op) {
					r.emitFailure(r.session.SessionID, roomErr.stage, roomErr.Error(), true)
				}
				return
			}
			if r.isCurrentRoomOp(room, op) {
				r.emitFailure(r.session.SessionID, roomErr.stage, roomErr.Error(), roomErr.retryable)
			}
			return
		}
		if r.isCurrentRoomOp(room, op) {
			r.emitFailure(r.session.SessionID, "apply_room_count", err.Error(), false)
		}
		return
	}
	r.clearRetryState(room)
	if !r.isCurrentRoomOp(room, op) {
		return
	}
	r.events.emit(protocol.RoomUpdated(r.session.SessionID, room, humans, r.roomMemberIDs(room), joined))
}

func (r *DisgoRuntime) isCurrentRoomOp(room protocol.RoomRef, op uint64) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.roomOps[room] == op
}

func (r *DisgoRuntime) applyRoomCount(ctx context.Context, room protocol.RoomRef, humans int) (bool, error) {
	var joined bool
	if humans > 0 {
		if _, err := r.ensureConn(ctx, room); err != nil {
			return false, err
		}
		joined = true
	} else {
		r.stopPlayback(room)
		r.mu.Lock()
		conn := r.conns[room]
		cancel := r.connecting[room]
		if conn != nil {
			delete(r.conns, room)
		}
		if cancel != nil {
			delete(r.connecting, room)
		}
		currentClient := r.client
		r.mu.Unlock()
		if cancel != nil {
			cancel()
			if currentClient != nil && currentClient.VoiceManager != nil {
				if guildID, err := snowflake.Parse(room.GuildID); err == nil {
					currentClient.VoiceManager.RemoveConn(guildID)
				}
			}
		}
		if conn != nil {
			conn.Close(ctx)
		}
		joined = false
	}

	return joined, nil
}

func (r *DisgoRuntime) roomMemberIDs(room protocol.RoomRef) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.rooms == nil {
		return nil
	}
	return r.rooms.memberIDs(room)
}

func (r *DisgoRuntime) logRoomMembership(room protocol.RoomRef, state roomState) {
	r.logger.Info(
		"discord_live_voice_transport_room_count_updated",
		slog.String("guild_id", room.GuildID),
		slog.String("channel_id", room.ChannelID),
		slog.Int("human_count", state.humanCount),
		slog.Any("human_user_ids", memberSetKeys(state.humanMemberIDs)),
		slog.Bool("joined", state.joined),
	)
}

func (r *DisgoRuntime) clearRetryState(room protocol.RoomRef) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if timer := r.retryTimers[room]; timer != nil {
		timer.Stop()
		delete(r.retryTimers, room)
	}
	delete(r.retryAttempts, room)
}

func (r *DisgoRuntime) scheduleRetry(room protocol.RoomRef, humans int, op uint64) bool {
	r.mu.Lock()
	if r.roomOps[room] != op || humans <= 0 {
		r.mu.Unlock()
		return false
	}
	if existing := r.retryTimers[room]; existing != nil {
		r.mu.Unlock()
		return true
	}
	attempt := r.retryAttempts[room] + 1
	r.retryAttempts[room] = attempt
	delay := retryBackoff(attempt, op)
	timer := time.AfterFunc(delay, func() {
		r.mu.Lock()
		delete(r.retryTimers, room)
		r.mu.Unlock()
		go r.applyRoomCountAsync(room, humans, op)
	})
	r.retryTimers[room] = timer
	r.mu.Unlock()

	r.logger.Warn(
		"discord_live_voice_transport_retry_scheduled",
		slog.String("guild_id", room.GuildID),
		slog.String("channel_id", room.ChannelID),
		slog.Int("attempt", attempt),
		slog.Duration("delay", delay),
	)
	return true
}

func retryBackoff(attempt int, op uint64) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	delay := 500 * time.Millisecond
	for i := 1; i < attempt; i++ {
		delay *= 2
		if delay >= 8*time.Second {
			delay = 8 * time.Second
			break
		}
	}
	jitter := time.Duration(op%5) * 125 * time.Millisecond
	return delay + jitter
}

func (r *DisgoRuntime) startPlayback(room protocol.RoomRef, sessionID string, conn voice.Conn, frames [][]byte) {
	r.stopPlayback(room)
	playbackCtx, cancel := context.WithCancel(context.Background())
	r.mu.Lock()
	r.playback[room] = cancel
	r.mu.Unlock()

	go func() {
		defer func() {
			r.mu.Lock()
			if current := r.playback[room]; current != nil {
				if fmt.Sprintf("%p", current) == fmt.Sprintf("%p", cancel) {
					delete(r.playback, room)
				}
			}
			r.mu.Unlock()
		}()
		defer func() {
			_ = conn.SetSpeaking(context.Background(), voice.SpeakingFlagNone)
		}()

		count := 0
		for idx, frame := range frames {
			select {
			case <-playbackCtx.Done():
				return
			default:
			}
			if _, err := conn.UDP().Write(frame); err != nil {
				r.emitFailure(sessionID, "write_audio", err.Error(), false)
				return
			}
			count++
			if idx < len(frames)-1 {
				select {
				case <-playbackCtx.Done():
					return
				case <-time.After(20 * time.Millisecond):
				}
			}
		}
		r.events.emit(protocol.PlaybackFinished(sessionID, room, count))
	}()
}

func (r *DisgoRuntime) stopPlayback(room protocol.RoomRef) {
	r.mu.Lock()
	cancel := r.playback[room]
	delete(r.playback, room)
	r.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (r *DisgoRuntime) Close(ctx context.Context) error {
	r.mu.Lock()
	if r.client == nil {
		r.mu.Unlock()
		return nil
	}
	client := r.client
	r.client = nil
	retryTimers := r.retryTimers
	r.retryTimers = make(map[protocol.RoomRef]*time.Timer)
	playback := r.playback
	r.playback = make(map[protocol.RoomRef]context.CancelFunc)
	r.mu.Unlock()
	for _, timer := range retryTimers {
		if timer != nil {
			timer.Stop()
		}
	}
	for _, cancel := range playback {
		if cancel != nil {
			cancel()
		}
	}
	client.Close(ctx)
	return nil
}

type opusSink struct {
	events    *eventSink
	sessionID string
	room      protocol.RoomRef
}

func newOpusSink(events *eventSink, sessionID string, room protocol.RoomRef) *opusSink {
	return &opusSink{events: events, sessionID: sessionID, room: room}
}

func (s *opusSink) ReceiveOpusFrame(userID snowflake.ID, packet *voice.Packet) error {
	if packet == nil {
		return nil
	}
	if userID == 0 {
		return nil
	}
	s.events.emit(protocol.Opus(s.sessionID, s.room, userID.String(), packet.SSRC, packet.Sequence, packet.Timestamp, packet.Opus))
	return nil
}

func (s *opusSink) CleanupUser(userID snowflake.ID) {}

func (s *opusSink) Close() {}
