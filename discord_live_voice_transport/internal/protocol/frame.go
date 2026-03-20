package protocol

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

const (
	TypeOpenSession      = "open_session"
	TypeCloseSession     = "close_session"
	TypePlayAudio        = "play_audio"
	TypeStopAudio        = "stop_audio"
	TypeTransportReady   = "transport_ready"
	TypeTransportFailed  = "transport_failed"
	TypeRoomState        = "room_state"
	TypeOpusFrame        = "opus_frame"
	TypePlaybackFinished = "playback_finished"
)

var ErrEmptyFrame = errors.New("empty frame")

// RoomRef identifies a Discord voice room by guild/channel pair.
type RoomRef struct {
	GuildID   string `json:"guild_id"`
	ChannelID string `json:"channel_id"`
}

// Frame is the JSON-lines envelope used between Python and the sidecar.
//
// Commands:
//   - open_session
//   - close_session
//   - play_audio
//   - stop_audio
//
// Events:
//   - transport_ready
//   - transport_failed
//   - room_state
//   - opus_frame
//   - playback_finished
type Frame struct {
	Type string `json:"type"`

	SessionID string    `json:"session_id,omitempty"`
	Token     string    `json:"token,omitempty"`
	Rooms     []RoomRef `json:"rooms,omitempty"`
	Room      *RoomRef  `json:"room,omitempty"`

	AutoJoin    bool `json:"auto_join,omitempty"`
	ReceiveOpus bool `json:"receive_opus,omitempty"`
	SendAudio   bool `json:"send_audio,omitempty"`

	OpusFrameBase64  string   `json:"opus_frame_base64,omitempty"`
	OpusFramesBase64 []string `json:"opus_frames_base64,omitempty"`

	Backend        string   `json:"backend,omitempty"`
	ClientID       string   `json:"client_id,omitempty"`
	GuildID        string   `json:"guild_id,omitempty"`
	ChannelID      string   `json:"channel_id,omitempty"`
	UserID         string   `json:"user_id,omitempty"`
	SSRC           uint32   `json:"ssrc,omitempty"`
	Sequence       uint16   `json:"sequence,omitempty"`
	Timestamp      uint32   `json:"timestamp,omitempty"`
	HumanCount     int      `json:"human_count,omitempty"`
	HumanUserIDs   []string `json:"human_user_ids,omitempty"`
	Joined         bool     `json:"joined,omitempty"`
	PlaybackFrames int      `json:"playback_frames,omitempty"`

	Stage     string `json:"stage,omitempty"`
	Reason    string `json:"reason,omitempty"`
	Error     string `json:"error,omitempty"`
	Retryable bool   `json:"retryable,omitempty"`
}

func (f Frame) Validate() error {
	switch f.Type {
	case TypeOpenSession:
		if f.SessionID == "" {
			return errors.New("open_session requires session_id")
		}
		if f.Token == "" {
			return errors.New("open_session requires token")
		}
		if len(f.Rooms) == 0 {
			return errors.New("open_session requires rooms")
		}
		for i, room := range f.Rooms {
			if room.GuildID == "" || room.ChannelID == "" {
				return fmt.Errorf("open_session room[%d] requires guild_id and channel_id", i)
			}
		}
	case TypeCloseSession:
		if f.SessionID == "" {
			return errors.New("close_session requires session_id")
		}
	case TypePlayAudio:
		if f.SessionID == "" {
			return errors.New("play_audio requires session_id")
		}
		if f.Room == nil {
			return errors.New("play_audio requires room")
		}
		if f.Room.GuildID == "" || f.Room.ChannelID == "" {
			return errors.New("play_audio room requires guild_id and channel_id")
		}
		if len(f.OpusFramesBase64) == 0 && f.OpusFrameBase64 == "" {
			return errors.New("play_audio requires opus_frame_base64 or opus_frames_base64")
		}
		for _, frame := range f.OpusFramesBase64 {
			if _, err := base64.StdEncoding.DecodeString(frame); err != nil {
				return fmt.Errorf("play_audio opus_frames_base64 must contain valid base64: %w", err)
			}
		}
		if f.OpusFrameBase64 != "" {
			if _, err := base64.StdEncoding.DecodeString(f.OpusFrameBase64); err != nil {
				return fmt.Errorf("play_audio opus_frame_base64 must be valid base64: %w", err)
			}
		}
	case TypeStopAudio:
		if f.SessionID == "" {
			return errors.New("stop_audio requires session_id")
		}
		if f.Room == nil {
			return errors.New("stop_audio requires room")
		}
		if f.Room.GuildID == "" || f.Room.ChannelID == "" {
			return errors.New("stop_audio room requires guild_id and channel_id")
		}
	case TypeTransportReady:
		if f.SessionID == "" {
			return errors.New("transport_ready requires session_id")
		}
	case TypeTransportFailed:
		if f.Stage == "" {
			return errors.New("transport_failed requires stage")
		}
		if f.Error == "" {
			return errors.New("transport_failed requires error")
		}
	case TypeRoomState:
		if f.SessionID == "" {
			return errors.New("room_state requires session_id")
		}
		if f.GuildID == "" || f.ChannelID == "" {
			return errors.New("room_state requires guild_id and channel_id")
		}
	case TypeOpusFrame:
		if f.SessionID == "" {
			return errors.New("opus_frame requires session_id")
		}
		if f.GuildID == "" || f.ChannelID == "" {
			return errors.New("opus_frame requires guild_id and channel_id")
		}
		if f.UserID == "" {
			return errors.New("opus_frame requires user_id")
		}
		if f.OpusFrameBase64 == "" {
			return errors.New("opus_frame requires opus_frame_base64")
		}
		if _, err := base64.StdEncoding.DecodeString(f.OpusFrameBase64); err != nil {
			return fmt.Errorf("opus_frame opus_frame_base64 must be valid base64: %w", err)
		}
	case TypePlaybackFinished:
		if f.SessionID == "" {
			return errors.New("playback_finished requires session_id")
		}
		if f.GuildID == "" || f.ChannelID == "" {
			return errors.New("playback_finished requires guild_id and channel_id")
		}
		if f.PlaybackFrames < 0 {
			return errors.New("playback_finished requires non-negative playback_frames")
		}
	default:
		return fmt.Errorf("unknown frame type %q", f.Type)
	}

	return nil
}

func DecodeLine(line []byte) (Frame, error) {
	trimmed := bytes.TrimSpace(line)
	if len(trimmed) == 0 {
		return Frame{}, ErrEmptyFrame
	}

	var frame Frame
	if err := json.Unmarshal(trimmed, &frame); err != nil {
		return Frame{}, fmt.Errorf("decode frame: %w", err)
	}
	if err := frame.Validate(); err != nil {
		return Frame{}, err
	}
	return frame, nil
}

func Encode(w io.Writer, frame Frame) error {
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	return enc.Encode(frame)
}

func RoomJoined(sessionID string, room RoomRef, humanCount int, humanUserIDs []string) Frame {
	return Frame{
		Type:         TypeRoomState,
		SessionID:    sessionID,
		GuildID:      room.GuildID,
		ChannelID:    room.ChannelID,
		HumanCount:   humanCount,
		HumanUserIDs: append([]string(nil), humanUserIDs...),
		Joined:       true,
	}
}

func RoomUpdated(sessionID string, room RoomRef, humanCount int, humanUserIDs []string, joined bool) Frame {
	return Frame{
		Type:         TypeRoomState,
		SessionID:    sessionID,
		GuildID:      room.GuildID,
		ChannelID:    room.ChannelID,
		HumanCount:   humanCount,
		HumanUserIDs: append([]string(nil), humanUserIDs...),
		Joined:       joined,
	}
}

func Ready(sessionID, backend, clientID string) Frame {
	return Frame{
		Type:      TypeTransportReady,
		SessionID: sessionID,
		Backend:   backend,
		ClientID:  clientID,
	}
}

func Failed(sessionID, stage, reason string, retryable bool) Frame {
	return Frame{
		Type:      TypeTransportFailed,
		SessionID: sessionID,
		Stage:     stage,
		Reason:    reason,
		Error:     reason,
		Retryable: retryable,
	}
}

func Opus(sessionID string, room RoomRef, userID string, ssrc uint32, sequence uint16, timestamp uint32, opus []byte) Frame {
	return Frame{
		Type:            TypeOpusFrame,
		SessionID:       sessionID,
		GuildID:         room.GuildID,
		ChannelID:       room.ChannelID,
		UserID:          userID,
		SSRC:            ssrc,
		Sequence:        sequence,
		Timestamp:       timestamp,
		OpusFrameBase64: base64.StdEncoding.EncodeToString(opus),
	}
}

func PlaybackFinished(sessionID string, room RoomRef, frames int) Frame {
	return Frame{
		Type:           TypePlaybackFinished,
		SessionID:      sessionID,
		GuildID:        room.GuildID,
		ChannelID:      room.ChannelID,
		PlaybackFrames: frames,
	}
}
