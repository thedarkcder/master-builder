//go:build !libdave

package transport

import (
	"log/slog"

	"github.com/disgoorg/godave"
)

func newDaveSession(logger *slog.Logger, userID godave.UserID, callbacks godave.Callbacks) godave.Session {
	panic("discord-live-voice-transport must be built with -tags=libdave")
}
