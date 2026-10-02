import hashlib
import io
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tarfile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_libdave.py"


def _module():
    return runpy.run_path(str(SCRIPT))


def _archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, value in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            tar.addfile(member, io.BytesIO(value))
    return stream.getvalue()


def test_extracts_verified_immutable_source_bytes(tmp_path):
    data = _archive(
        {
            "upstream/LICENSE": b"Example license",
            "upstream/CMakeLists.txt": b"project(example)",
        }
    )
    root = _module()["extract_source"](
        data, hashlib.sha256(data).hexdigest(), tmp_path / "source"
    )
    assert (root / "LICENSE").read_bytes() == b"Example license"


def test_rejects_wrong_source_checksum_before_extraction(tmp_path):
    data = _archive({"upstream/LICENSE": b"fixture"})
    with pytest.raises(ValueError, match="checksum"):
        _module()["extract_source"](data, "0" * 64, tmp_path / "source")
    assert not (tmp_path / "source").exists()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "upstream/../../outside"])
def test_rejects_unsafe_source_paths_before_extraction(tmp_path, name):
    data = _archive({"upstream/LICENSE": b"fixture", name: b"unsafe"})
    with pytest.raises(ValueError, match="Unsafe"):
        _module()["extract_source"](
            data, hashlib.sha256(data).hexdigest(), tmp_path / "source"
        )
    assert not (tmp_path / "source").exists()


def test_rejects_source_links_before_extraction(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        member = tarfile.TarInfo("upstream/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/outside"
        tar.addfile(member)
    data = stream.getvalue()
    with pytest.raises(ValueError, match="link"):
        _module()["extract_source"](
            data, hashlib.sha256(data).hexdigest(), tmp_path / "source"
        )
    assert not (tmp_path / "source").exists()


@pytest.mark.parametrize("version", ["1.1.1", "4.0.0", "BoringSSL"])
def test_rejects_incompatible_crypto_before_build(tmp_path, version):
    with pytest.raises(ValueError, match="OpenSSL 3"):
        _module()["verify_crypto"](version, tmp_path)


def test_boringssl_header_is_rejected_even_with_version_three(tmp_path):
    (tmp_path / "openssl").mkdir()
    (tmp_path / "openssl/is_boringssl.h").write_text("fixture")
    with pytest.raises(ValueError, match="BoringSSL"):
        _module()["verify_crypto"]("3.5.1", tmp_path)


def test_requires_real_openssl3_dynamic_linkage():
    verify = _module()["verify_linkage"]
    verify(
        "Shared library: [libcrypto.so.3]", "libcrypto.so.3 => /usr/lib/libcrypto.so.3"
    )
    for dynamic, dependencies in [
        ("Shared library: [libcrypto.so.1.1]", ""),
        ("", ""),
        ("Shared library: [libcrypto.so.3]", "not found"),
        ("Shared library: [libcrypto.so.3]\nShared library: [libcrypto.so.1.1]", ""),
        ("Shared library: [libcrypto.so.3]\nShared library: [libssl.so.1.1]", ""),
    ]:
        with pytest.raises(ValueError, match="link"):
            verify(dynamic, dependencies)


def test_unsupported_native_architecture_fails_before_network(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--architecture",
            "unknown",
            "--prefix",
            str(tmp_path / "native"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "architecture" in result.stderr
    assert not (tmp_path / "native").exists()


def test_pkg_config_resolves_header_used_by_native_go_binding(tmp_path):
    prefix = tmp_path / "native"
    header = prefix / "include/dave/dave.h"
    header.parent.mkdir(parents=True)
    header.write_text("int dave_fixture(void);\n")
    pkg_dir = prefix / "lib/pkgconfig"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "dave.pc").write_text(_module()["pkg_config"](prefix))
    flags = subprocess.run(
        ["pkg-config", "--cflags", "dave"],
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "PKG_CONFIG_LIBDIR": str(pkg_dir)},
    ).stdout
    subprocess.run(
        ["cc", "-fsyntax-only", "-x", "c", *shlex.split(flags), "-"],
        input="#include <dave.h>\n",
        text=True,
        capture_output=True,
        check=True,
    )


def test_source_build_contract_has_no_binary_or_vcpkg_fallback():
    script = SCRIPT.read_text()
    assert "boringssl.zip" not in script
    assert "zipfile" not in script
    docker = (SCRIPT.parents[1] / "orchestrator/Dockerfile").read_text()
    assert "LIBDAVE_FORCE_BUILD" not in docker
    assert "--notices /tmp/libdave-notices" in docker
    assert "libssl3t64" in docker
    assert (
        "COPY --from=live-voice-transport-builder /root/.local /root/.local" in docker
    )


def test_voice_docker_build_preserves_go_runtime_module_notices_and_source_evidence():
    builder = (
        (SCRIPT.parents[1] / "orchestrator/Dockerfile")
        .read_text()
        .split("FROM python:", 1)[0]
    )
    assert "$(go env GOROOT)/LICENSE" in builder
    assert "go mod verify" in builder
    assert "go mod vendor" in builder
    assert "go build -mod=vendor -tags=libdave" in builder
    assert "go version -m /tmp/live-voice-transport" in builder
    assert "vendor-source.tar.gz" in builder
    assert "transport-source.tar.gz" in builder
    assert "voice-go" in builder
    assert "-iname '*notice*'" in builder
    assert builder.index("go list -m -json all") < builder.index("go mod vendor")
