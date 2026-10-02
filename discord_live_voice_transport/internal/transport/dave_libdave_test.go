//go:build libdave

package transport

import (
	"bytes"
	"runtime"
	"testing"

	"github.com/disgoorg/godave/libdave"
)

// This exercises the selected native implementation, including its OpenSSL
// randomness and signing, without a Discord connection or any credentials.
func TestNativeDaveSessionsProduceIndependentKeyPackages(t *testing.T) {
	// Match golibdave.NewSession: empty context/auth ID selects transient keys.
	// Nonempty auth IDs select the separate persisted-key storage interface.
	first := libdave.NewSession("", "")
	second := libdave.NewSession("", "")
	t.Cleanup(func() {
		first.Reset()
		second.Reset()
		runtime.KeepAlive(first)
		runtime.KeepAlive(second)
	})
	first.Init(1, 42, "42")
	second.Init(1, 42, "42")
	if first.GetProtocolVersion() != 1 || second.GetProtocolVersion() != 1 {
		t.Fatal("native session did not retain requested DAVE protocol version")
	}
	firstPackage := first.GetMarshalledKeyPackage()
	secondPackage := second.GetMarshalledKeyPackage()
	if len(firstPackage) == 0 || len(secondPackage) == 0 {
		t.Fatal("native DAVE key package is empty")
	}
	if bytes.Equal(firstPackage, secondPackage) {
		t.Fatal("independent native sessions reused a DAVE key package")
	}
}
