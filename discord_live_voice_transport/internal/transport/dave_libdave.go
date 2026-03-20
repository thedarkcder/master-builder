//go:build libdave

package transport

import (
	"log/slog"

	"github.com/disgoorg/godave"
	"github.com/disgoorg/godave/golibdave"
)

func newDaveSession(logger *slog.Logger, userID godave.UserID, callbacks godave.Callbacks) godave.Session {
	return golibdave.NewSession(logger, userID, callbacks)
}
