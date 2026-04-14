package transport

import (
	"context"

	"discord_live_voice_transport/internal/protocol"
)

type OpenSessionRequest = protocol.Frame
type CloseSessionRequest = protocol.Frame
type PlayAudioRequest = protocol.Frame
type StopAudioRequest = protocol.Frame

type Runtime interface {
	OpenSession(context.Context, OpenSessionRequest) error
	CloseSession(context.Context, CloseSessionRequest) error
	PlayAudio(context.Context, PlayAudioRequest) error
	StopAudio(context.Context, StopAudioRequest) error
	Events() <-chan protocol.Frame
	Close(context.Context) error
}
