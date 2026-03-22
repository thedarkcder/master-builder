package transport

import (
	"log/slog"
	"testing"

	"discord_live_voice_transport/internal/protocol"
)

func TestRoomTrackerTracksHumanCountsAndJoinState(t *testing.T) {
	t.Parallel()

	room := protocol.RoomRef{GuildID: "1", ChannelID: "2"}
	tracker := newRoomTracker([]protocol.RoomRef{room})

	previous, current, changed := tracker.setHumanMembers(room, []string{"u-1", "u-2"})
	if !changed {
		t.Fatalf("changed = false, want true")
	}
	if previous.humanCount != 0 || current.humanCount != 2 {
		t.Fatalf("counts = %+v %+v", previous, current)
	}
	if current.joined != true {
		t.Fatalf("joined = %v, want true", current.joined)
	}

	_, current, changed = tracker.setHumanMembers(room, nil)
	if !changed {
		t.Fatalf("changed = false, want true")
	}
	if current.joined {
		t.Fatalf("joined = true, want false")
	}
}

func TestRoomTrackerDeduplicatesHumanMembers(t *testing.T) {
	t.Parallel()

	room := protocol.RoomRef{GuildID: "1", ChannelID: "2"}
	tracker := newRoomTracker([]protocol.RoomRef{room})

	_, current, changed := tracker.setHumanMembers(room, []string{"u-1", "u-1", "u-2"})
	if !changed {
		t.Fatalf("changed = false, want true")
	}
	if current.humanCount != 2 || !current.joined {
		t.Fatalf("current = %+v", current)
	}
	if got := tracker.memberIDs(room); len(got) != 2 {
		t.Fatalf("len(memberIDs) = %d, want 2", len(got))
	}

	_, current, changed = tracker.addHumanMember(room, "u-2")
	if changed {
		t.Fatalf("changed = true, want false for duplicate member")
	}
	if current.humanCount != 2 {
		t.Fatalf("current.humanCount = %d, want 2", current.humanCount)
	}

	_, current, changed = tracker.removeHumanMember(room, "u-1")
	if !changed {
		t.Fatalf("changed = false, want true")
	}
	if current.humanCount != 1 {
		t.Fatalf("current.humanCount = %d, want 1", current.humanCount)
	}
}

func TestTransitionTrackedUserUsesCurrentTrackedRoomForBlankDisconnects(t *testing.T) {
	t.Parallel()

	roomA := protocol.RoomRef{GuildID: "guild-1", ChannelID: "voice-a"}
	roomB := protocol.RoomRef{GuildID: "guild-1", ChannelID: "voice-b"}
	runtime := &DisgoRuntime{
		logger:    slog.Default(),
		events:    newEventSink(8),
		rooms:     newRoomTracker([]protocol.RoomRef{roomA, roomB}),
		roomOps:   make(map[protocol.RoomRef]uint64),
		userRooms: make(map[string]protocol.RoomRef),
	}

	runtime.transitionTrackedUser("user-1", &roomA)
	if got := runtime.rooms.count(roomA); got != 1 {
		t.Fatalf("runtime.rooms.count(roomA) = %d, want 1", got)
	}

	runtime.transitionTrackedUser("user-1", nil)
	if got := runtime.rooms.count(roomA); got != 0 {
		t.Fatalf("runtime.rooms.count(roomA) = %d, want 0 after disconnect", got)
	}

	runtime.transitionTrackedUser("user-1", &roomA)
	runtime.transitionTrackedUser("user-1", &roomB)
	if got := runtime.rooms.count(roomA); got != 0 {
		t.Fatalf("runtime.rooms.count(roomA) = %d, want 0 after move", got)
	}
	if got := runtime.rooms.count(roomB); got != 1 {
		t.Fatalf("runtime.rooms.count(roomB) = %d, want 1 after move", got)
	}
}

func TestApplyObservedRoomCountIgnoresDuplicateJoinCount(t *testing.T) {
	t.Parallel()

	room := protocol.RoomRef{GuildID: "guild-1", ChannelID: "voice-a"}
	runtime := &DisgoRuntime{
		logger:    slog.Default(),
		events:    newEventSink(8),
		rooms:     newRoomTracker([]protocol.RoomRef{room}),
		roomOps:   make(map[protocol.RoomRef]uint64),
		userRooms: make(map[string]protocol.RoomRef),
	}
	runtime.rooms.setHumanMembers(room, []string{"user-1"})

	runtime.applyObservedRoomMembers(room, []string{"user-1", "user-1"})

	if got := runtime.rooms.count(room); got != 1 {
		t.Fatalf("runtime.rooms.count(room) = %d, want 1", got)
	}
	if got := runtime.roomOps[room]; got != 0 {
		t.Fatalf("runtime.roomOps[room] = %d, want 0", got)
	}
}
