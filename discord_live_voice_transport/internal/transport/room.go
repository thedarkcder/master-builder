package transport

import (
	"sort"

	"discord_live_voice_transport/internal/protocol"
)

type roomState struct {
	ref            protocol.RoomRef
	humanMemberIDs map[string]struct{}
	humanCount     int
	joined         bool
}

type roomTracker struct {
	rooms map[protocol.RoomRef]*roomState
}

func newRoomTracker(rooms []protocol.RoomRef) *roomTracker {
	tracker := &roomTracker{rooms: make(map[protocol.RoomRef]*roomState, len(rooms))}
	for _, room := range rooms {
		tracker.rooms[room] = &roomState{ref: room, humanMemberIDs: make(map[string]struct{})}
	}
	return tracker
}

func (t *roomTracker) setHumanMembers(room protocol.RoomRef, memberIDs []string) (previous roomState, current roomState, changed bool) {
	state := t.rooms[room]
	if state == nil {
		state = &roomState{ref: room, humanMemberIDs: make(map[string]struct{})}
		t.rooms[room] = state
	}
	previous = *state
	previous.humanMemberIDs = cloneMemberSet(previous.humanMemberIDs)

	nextMembers := normalizeMemberSet(memberIDs)
	state.humanMemberIDs = nextMembers
	state.humanCount = len(nextMembers)
	state.joined = state.humanCount > 0
	current = *state
	current.humanMemberIDs = cloneMemberSet(current.humanMemberIDs)
	return previous, current, !equalMemberSets(previous.humanMemberIDs, current.humanMemberIDs) || previous.joined != current.joined
}

func (t *roomTracker) addHumanMember(room protocol.RoomRef, userID string) (previous roomState, current roomState, changed bool) {
	memberIDs := t.memberIDs(room)
	memberIDs = append(memberIDs, userID)
	return t.setHumanMembers(room, memberIDs)
}

func (t *roomTracker) removeHumanMember(room protocol.RoomRef, userID string) (previous roomState, current roomState, changed bool) {
	memberIDs := t.memberIDs(room)
	filtered := make([]string, 0, len(memberIDs))
	for _, existingUserID := range memberIDs {
		if existingUserID == userID {
			continue
		}
		filtered = append(filtered, existingUserID)
	}
	return t.setHumanMembers(room, filtered)
}

func (t *roomTracker) memberIDs(room protocol.RoomRef) []string {
	state := t.rooms[room]
	if state == nil {
		return nil
	}
	return memberSetKeys(state.humanMemberIDs)
}

func (t *roomTracker) count(room protocol.RoomRef) int {
	if state := t.rooms[room]; state != nil {
		return state.humanCount
	}
	return 0
}

func normalizeMemberSet(memberIDs []string) map[string]struct{} {
	normalized := make(map[string]struct{}, len(memberIDs))
	for _, memberID := range memberIDs {
		if memberID == "" {
			continue
		}
		normalized[memberID] = struct{}{}
	}
	return normalized
}

func cloneMemberSet(source map[string]struct{}) map[string]struct{} {
	if len(source) == 0 {
		return map[string]struct{}{}
	}
	cloned := make(map[string]struct{}, len(source))
	for memberID := range source {
		cloned[memberID] = struct{}{}
	}
	return cloned
}

func equalMemberSets(left map[string]struct{}, right map[string]struct{}) bool {
	if len(left) != len(right) {
		return false
	}
	for memberID := range left {
		if _, ok := right[memberID]; !ok {
			return false
		}
	}
	return true
}

func memberSetKeys(source map[string]struct{}) []string {
	keys := make([]string, 0, len(source))
	for memberID := range source {
		keys = append(keys, memberID)
	}
	sort.Strings(keys)
	return keys
}
