#!/usr/bin/env python3
"""Build pinned libdave sources against Debian OpenSSL 3; no binary fallback."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

VERSION = "v1.1.0"
SOURCE_REVISION = "d6874165b9a7c8d2cc59712c7aceaa8dffb189b4"
SOURCES = {
    "libdave": (
        "discord/libdave",
        SOURCE_REVISION,
        "0185d417fdaec74efa5d6cf7f06192bed8309a227050c0aba94f50c2311fb6db",
    ),
    "mlspp": (
        "cisco/mlspp",
        "1cc50a124a3bc4e143a787ec934280dc70c1034d",
        "7a9d6318627e548903bc65c3dc5a4de4a90290983efea9a49af8561ed3f999f5",
    ),
    "nlohmann-json": (
        "nlohmann/json",
        "9cca280a4d0ccf0c08f47a99aa71d1b0e52f8d03",
        "0dbc5e40a01ff142e7e68c03e85247a4dcede2f592d12d3677dee3664d17975a",
    ),
}
ARCHITECTURES = {
    "amd64": ("x86_64", "Advanced Micro Devices X86-64"),
    "arm64": ("aarch64", "AArch64"),
}
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
BOOST_SHA256 = "c9bff75738922193e67fa726fa225535870d2aa1059f91452c411736284ad566"


def extract_source(data: bytes, expected: str, destination: Path) -> Path:
    if len(data) > MAX_SOURCE_BYTES or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Native source checksum or size verification failed")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Source destination already exists")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        roots, seen, total = set(), set(), 0
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or "\x00" in member.name
            ):
                raise ValueError("Unsafe source archive path")
            if not (member.isfile() or member.isdir()):
                raise ValueError(
                    "Source archive links or special files are not supported"
                )
            if str(path) in seen:
                raise ValueError("Duplicate source archive member")
            seen.add(str(path))
            roots.add(path.parts[0])
            total += member.size
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("Source archive exceeds extraction bound")
        if len(roots) != 1:
            raise ValueError("Source archive must contain one upstream root")
        destination.mkdir(parents=True)
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Cannot read source archive member")
                with stream:
                    target.write_bytes(stream.read())
        return destination / next(iter(roots))


def verify_crypto(version: str, include: Path) -> None:
    if re.fullmatch(r"3\.[0-9]+\.[0-9]+(?:[-+].*)?", version) is None:
        raise ValueError(
            "OpenSSL 3 is required; other crypto implementations are unsupported"
        )
    if (include / "openssl/is_boringssl.h").exists():
        raise ValueError("BoringSSL headers are not supported")


def verify_linkage(dynamic: str, dependencies: str) -> None:
    crypto_sonames = re.findall(
        r"lib(?:crypto|ssl)\.so(?:\.[0-9.]+)?", dynamic + "\n" + dependencies
    )
    if (
        "Shared library: [libcrypto.so.3]" not in dynamic
        or "not found" in dependencies
        or "boring" in dynamic.lower()
        or any(name not in {"libcrypto.so.3", "libssl.so.3"} for name in crypto_sonames)
    ):
        raise ValueError(
            "Native linkage must resolve OpenSSL 3 libcrypto.so.3 without missing libraries"
        )


def command(args: list[str]) -> str:
    return subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE, timeout=120
    ).stdout.strip()


def pkg_config(prefix: Path) -> str:
    return (
        f"prefix={prefix}\n"
        "libdir=${prefix}/lib\nincludedir=${prefix}/include/dave\n\n"
        "Name: dave\nDescription: Discord Audio and Video End-to-End Encryption\n"
        "Version: 1.1.0\nLibs: -L${libdir} -ldave -Wl,-rpath,${libdir}\n"
        "Cflags: -I${includedir}\n"
    )


def build(prefix: Path, architecture: str, notices: Path, parallel: int) -> None:
    if prefix.exists() or prefix.is_symlink():
        raise ValueError("Native installation already exists; choose a fresh prefix")
    if not prefix.is_absolute() or prefix.parent.resolve() != prefix.parent:
        raise ValueError("Native prefix must be absolute with no symlink parent")
    if (
        architecture not in ARCHITECTURES
        or platform.system() != "Linux"
        or platform.machine() != ARCHITECTURES[architecture][0]
    ):
        raise ValueError(
            "Unsupported architecture or host; use matching Linux amd64/arm64 builder"
        )
    if parallel not in range(1, 5):
        raise ValueError("Build parallelism must be between 1 and 4")
    boost = (notices / "BSL-1.0.txt").read_bytes()
    if hashlib.sha256(boost).hexdigest() != BOOST_SHA256:
        raise ValueError("Boost license checksum differs from verified upstream text")
    version = command(["pkg-config", "--modversion", "openssl"])
    include = Path(command(["pkg-config", "--variable=includedir", "openssl"]))
    crypto = (
        Path(command(["pkg-config", "--variable=libdir", "openssl"])) / "libcrypto.so"
    )
    verify_crypto(version, include)
    if not crypto.is_file():
        raise ValueError("OpenSSL 3 shared development library is missing")
    openssl_notice = Path("/usr/share/doc/libssl3t64/copyright").read_bytes()
    with tempfile.TemporaryDirectory(prefix="libdave-openssl3-") as temporary:
        work = Path(temporary)
        stage = work / "installed"
        sources, archives = {}, {}
        for name, (repository, revision, digest) in SOURCES.items():
            url = f"https://codeload.github.com/{repository}/tar.gz/{revision}"
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read(MAX_SOURCE_BYTES + 1)
            sources[name] = extract_source(data, digest, work / name)
            archives[name] = data
        settings = [
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_INSTALL_LIBDIR=lib",
            f"-DCMAKE_INSTALL_PREFIX={stage}",
            f"-DCMAKE_PREFIX_PATH={stage}",
            "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
            "-DTESTING=OFF",
            "-DREQUIRE_BORINGSSL=OFF",
            f"-DOPENSSL_INCLUDE_DIR={include}",
            f"-DOPENSSL_CRYPTO_LIBRARY={crypto}",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ]
        for name, extra in [
            ("nlohmann-json", ["-DJSON_BuildTests=OFF"]),
            (
                "mlspp",
                [
                    "-DBUILD_SHARED_LIBS=OFF",
                    "-DDISABLE_GREASE=ON",
                    "-DMLS_CXX_NAMESPACE=mlspp",
                ],
            ),
            ("libdave", ["-DBUILD_SHARED_LIBS=ON", "-DINSTALL_VCPKG_LICENSES=OFF"]),
        ]:
            source = sources[name] / "cpp" if name == "libdave" else sources[name]
            output = work / (name + "-build")
            if shutil.disk_usage(work).free < 2 * 1024**3:
                raise ValueError("Native build requires at least 2 GiB free disk")
            for args in [
                ["cmake", "-S", str(source), "-B", str(output), *settings, *extra],
                ["cmake", "--build", str(output), "--parallel", str(parallel)],
                ["cmake", "--install", str(output)],
            ]:
                subprocess.run(args, check=True, timeout=1800)
            if name in {"mlspp", "libdave"}:
                compile_commands = (output / "compile_commands.json").read_text()
                if (
                    "WITH_OPENSSL3" not in compile_commands
                    or "WITH_BORINGSSL" in compile_commands
                ):
                    raise ValueError("Native compilation did not select only OpenSSL 3")
        library = stage / "lib/libdave.so"
        dynamic = command(["readelf", "-d", str(library)])
        dependencies = command(["ldd", str(library)])
        verify_linkage(dynamic, dependencies)
        if ARCHITECTURES[architecture][1] not in command(
            ["readelf", "-h", str(library)]
        ):
            raise ValueError(
                "Compiled library architecture does not match requested target"
            )
        license_dir = stage / "licenses"
        license_dir.mkdir(exist_ok=True)
        for name, filename in [
            ("libdave", "LICENSE"),
            ("mlspp", "LICENSE"),
            ("nlohmann-json", "LICENSE.MIT"),
        ]:
            (license_dir / name).write_bytes((sources[name] / filename).read_bytes())
        (license_dir / "openssl-debian-copyright").write_bytes(openssl_notice)
        (license_dir / "BSL-1.0.txt").write_bytes(boost)
        variant = (sources["mlspp"] / "third_party/variant.hpp").read_bytes()
        (license_dir / "MPark-variant-NOTICE").write_bytes(
            b"\n".join(variant.splitlines()[:6]) + b"\n"
        )
        material = stage / "share/master-builder/libdave-source"
        material.mkdir(parents=True)
        for name, data in archives.items():
            (material / (name + ".tar.gz")).write_bytes(data)
        (material / "linkage.txt").write_text(dynamic + "\n" + dependencies + "\n")
        (material / "openssl-packages.txt").write_text(
            command(
                [
                    "dpkg-query",
                    "-W",
                    "-f=${Package}\t${Version}\t${source:Package}\t${source:Version}\n",
                    "libssl3t64",
                    "libssl-dev",
                ]
            )
            + "\n"
        )
        shutil.copyfile(Path(__file__), material / "install_libdave.py")
        pkg = stage / "lib/pkgconfig/dave.pc"
        pkg.parent.mkdir(parents=True, exist_ok=True)
        pkg.write_text(pkg_config(prefix))
        files = [
            stage / "lib/libdave.so",
            stage / "include/dave/dave.h",
            *sorted(license_dir.iterdir()),
        ]
        receipt = {
            "version": VERSION,
            "source_revision": SOURCE_REVISION,
            "architecture": architecture,
            "crypto": "OpenSSL3",
            "openssl_version": version,
            "sources": {
                name: {"repository": repo, "revision": rev, "archive_sha256": digest}
                for name, (repo, rev, digest) in SOURCES.items()
            },
            "files": {
                str(path.relative_to(stage)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in files
            },
        }
        (stage / "share/master-builder/libdave-receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
        prefix.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage), str(prefix))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--version", default=VERSION)
    parser.add_argument(
        "--notices",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "third_party/licenses/libdave-openssl3",
    )
    parser.add_argument("--parallel", type=int, default=1)
    args = parser.parse_args()
    try:
        if args.version != VERSION:
            raise ValueError("Unsupported source version; libdave v1.1.0 is required")
        if args.architecture not in ARCHITECTURES:
            raise ValueError("Unsupported architecture; use amd64 or arm64")
        build(args.prefix, args.architecture, args.notices, args.parallel)
        print(
            "Built pinned libdave sources with verified OpenSSL 3 linkage and retained original source/notices"
        )
        return 0
    except (ValueError, OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"libdave source build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
