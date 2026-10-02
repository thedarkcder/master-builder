#!/usr/bin/env python3
"""Collect release evidence from a Docker export without executing or extracting it.

The result is an inventory, not legal clearance or a complete corresponding-source
bundle. See docs/distribution-material.md for the remaining release obligations.
"""

from __future__ import annotations

import argparse
from email import message_from_bytes
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile


MAX_NOTICE_BYTES = 16 * 1024 * 1024
NOTICE_NAME = re.compile(
    r"^(?:licen[cs]es?|copying|notice|copyright|authors|credits)(?:[._-].*)?$", re.I
)
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")


def archive_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ValueError("Unsafe archive path")
    return str(path)


class Archive:
    def __init__(self, source: tarfile.TarFile):
        self.source = source
        self.members: dict[str, tarfile.TarInfo] = {}
        for member in source.getmembers():
            name = archive_path(member.name)
            if member.isdir():
                continue
            if name in self.members:
                raise ValueError("Duplicate archive path")
            self.members[name] = member

    def read(self, name: str, visited: frozenset[str] = frozenset()) -> bytes:
        name = archive_path(name)
        if name in visited or len(visited) > 40:
            raise ValueError("Cyclic or excessive archive link")
        member = self.members.get(name)
        if member is None:
            raise ValueError("Missing archive link target or required metadata")
        if member.issym() or member.islnk():
            link = PurePosixPath(member.linkname)
            parts = (
                []
                if link.is_absolute() or member.islnk()
                else list(PurePosixPath(name).parent.parts)
            )
            for part in link.parts:
                if part in ("/", "."):
                    continue
                if part == "..":
                    if not parts:
                        raise ValueError("Link points outside archive")
                    parts.pop()
                else:
                    parts.append(part)
            return self.read("/".join(parts), visited | {name})
        if not member.isfile():
            raise ValueError("Notice or package metadata is not a regular archive file")
        if member.size > MAX_NOTICE_BYTES:
            raise ValueError("Notice or metadata exceeds 16 MiB; review it separately")
        stream = self.source.extractfile(member)
        if stream is None:
            raise ValueError("Cannot read archive member")
        with stream:
            return stream.read()


def paragraphs(text: str, separator: str = ":") -> list[dict[str, str]]:
    records = []
    for block in text.strip().split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if not line or line[0].isspace():
                continue
            key, found, value = line.partition(separator)
            if not found:
                raise ValueError("Malformed package database")
            fields[key] = value.strip()
        if fields:
            records.append(fields)
    return records


def package_field(fields: dict[str, str], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("Missing or malformed package name/version")
    return value


def json_object(data: bytes) -> dict:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Malformed package or native receipt JSON object")
    return value


def os_inventory(archive: Archive) -> tuple[str, list[dict]]:
    debian = "var/lib/dpkg/status"
    alpine = "lib/apk/db/installed"
    if debian in archive.members and alpine in archive.members:
        raise ValueError("Ambiguous operating-system package databases")
    packages = []
    if debian in archive.members:
        for fields in paragraphs(archive.read(debian).decode()):
            if fields.get("Status") != "install ok installed":
                continue
            name = package_field(fields, "Package")
            version = package_field(fields, "Version")
            source = fields.get("Source", name)
            match = re.fullmatch(r"([a-z0-9][a-z0-9+.-]*)(?: \(([^\s()]+)\))?", source)
            if not PACKAGE_NAME.fullmatch(name) or match is None:
                raise ValueError("Malformed Debian package/source identity")
            packages.append(
                {
                    "name": name,
                    "version": version,
                    "architecture": package_field(fields, "Architecture"),
                    "source_package": match[1],
                    "source_version": match[2] or version,
                }
            )
        return "debian", packages
    if alpine in archive.members:
        for fields in paragraphs(archive.read(alpine).decode(), ":"):
            packages.append(
                {
                    "name": package_field(fields, "P"),
                    "version": package_field(fields, "V"),
                    "architecture": package_field(fields, "A"),
                    "origin": fields.get("o"),
                    "aports_revision": fields.get("c"),
                }
            )
        return "alpine", packages
    raise ValueError(
        "Expected Debian or Alpine package database in the exported artifact"
    )


def notice_path(name: str) -> bool:
    path = PurePosixPath(name)
    basename = path.name
    if name.startswith(
        (
            "usr/share/common-licenses/",
            "usr/share/licenses/",
            "app/third_party/licenses/",
            "app/.codex/third-party-licenses/",
        )
    ):
        return True
    if name in {
        "app/LICENSE",
        "app/THIRD_PARTY_NOTICES.md",
        "app/.codex/LICENSE",
        "app/.codex/THIRD_PARTY_NOTICES.md",
    }:
        return True
    contexts = (
        "usr/share/doc/",
        "root/.local/",
        "root/.cache/ms-playwright/",
        "usr/local/share/master-builder/",
    )
    package_context = "node_modules" in path.parts or "site-packages" in path.parts
    if not (name.startswith(contexts) or package_context):
        return False
    return bool(NOTICE_NAME.fullmatch(basename)) or "licenses" in path.parts


def npm_root(name: str) -> str | None:
    parts = PurePosixPath(name).parts
    if not parts or parts[-1] != "package.json" or "node_modules" not in parts:
        return None
    index = len(parts) - 1 - parts[::-1].index("node_modules")
    package_parts = parts[index + 1 : -1]
    if len(package_parts) == 1 and not package_parts[0].startswith("@"):
        return str(PurePosixPath(name).parent)
    if len(package_parts) == 2 and package_parts[0].startswith("@"):
        return str(PurePosixPath(name).parent)
    return None


def collect(
    archive: Archive, reference: str, digest: str
) -> tuple[dict, dict, dict[str, bytes]]:
    operating_system, os_packages = os_inventory(archive)
    material = {
        name: archive.read(name)
        for name, member in sorted(archive.members.items())
        if not member.isdir() and notice_path(name)
    }
    unresolved = [
        {
            "kind": "artifact-review-required",
            "detail": "Review native constituents, browser/CLI binaries, SDKs, models and voice samples separately.",
        },
        {
            "kind": "corresponding-source-required",
            "detail": "Source retrieval plans are not source bundles; retain exact source, patches and build material.",
        },
    ]
    for package in os_packages:
        prefix = "usr/share/doc/" + package["name"] + "/"
        alpine_prefix = "usr/share/licenses/" + package["name"] + "/"
        if not any(name.startswith((prefix, alpine_prefix)) for name in material):
            unresolved.append(
                {
                    "kind": "missing-os-notice",
                    "package": package["name"],
                    "version": package["version"],
                }
            )
    python_packages = []
    npm_packages = []
    for name in sorted(archive.members):
        path = PurePosixPath(name)
        if (
            path.name == "METADATA"
            and path.parent.name.endswith(".dist-info")
            and "site-packages" in path.parts
        ):
            metadata = message_from_bytes(archive.read(name))
            identity = {
                "Name": metadata.get("Name", ""),
                "Version": metadata.get("Version", ""),
            }
            package = {
                "name": package_field(identity, "Name"),
                "version": package_field(identity, "Version"),
                "license_expression": metadata.get("License-Expression"),
                "metadata_path": name,
            }
            python_packages.append(package)
            prefix = str(path.parent) + "/"
            if not any(item.startswith(prefix) for item in material):
                unresolved.append(
                    {
                        "kind": "missing-python-notice",
                        "package": package["name"],
                        "version": package["version"],
                    }
                )
        root = npm_root(name)
        if root is not None:
            metadata = json_object(archive.read(name))
            package = {
                "name": package_field(metadata, "name"),
                "version": package_field(metadata, "version"),
                "license_expression": metadata.get("license"),
                "package_path": root,
            }
            npm_packages.append(package)
            if not any(
                item.startswith(root + "/")
                and "/node_modules/" not in item[len(root) + 1 :]
                for item in material
            ):
                unresolved.append(
                    {
                        "kind": "missing-npm-notice",
                        "package": package["name"],
                        "version": package["version"],
                    }
                )
    managed_receipts = []
    receipt_path = "root/.local/share/master-builder/libdave-receipt.json"
    if receipt_path in archive.members:
        receipt = json_object(archive.read(receipt_path))
        files = receipt.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("Malformed native receipt")
        for relative, expected in files.items():
            relative = archive_path(relative)
            if relative not in {
                "lib/libdave.so",
                "include/dave/dave.h",
            } and not relative.startswith("licenses/"):
                raise ValueError("Unexpected native receipt member")
            if (
                hashlib.sha256(archive.read("root/.local/" + relative)).hexdigest()
                != expected
            ):
                raise ValueError("Native receipt checksum differs from exported file")
        managed_receipts.append(
            {
                "archive_path": receipt_path,
                "receipt": receipt,
                "verified_files": len(files),
            }
        )
    notices = [
        {
            "archive_path": name,
            "output_path": "notices/" + name,
            "sha256": hashlib.sha256(value).hexdigest(),
        }
        for name, value in material.items()
    ]
    inventory = {
        "schema_version": 1,
        "status": "collected-not-cleared",
        "artifact_reference": reference,
        "archive_sha256": digest,
        "operating_system": operating_system,
        "os_packages": os_packages,
        "python_packages": python_packages,
        "npm_packages": npm_packages,
        "notices": notices,
        "managed_receipts": managed_receipts,
        "unresolved": unresolved,
    }
    debian_sources = (
        sorted(
            {(item["source_package"], item["source_version"]) for item in os_packages}
        )
        if operating_system == "debian"
        else []
    )
    plan = {
        "schema_version": 1,
        "status": "retrieval-plan-not-source-bundle",
        "debian_sources": [
            {"package": name, "version": version} for name, version in debian_sources
        ],
        "alpine_sources": os_packages if operating_system == "alpine" else [],
        "python_sources": [
            {"package": p["name"], "version": p["version"]} for p in python_packages
        ],
        "npm_sources": [
            {"package": p["name"], "version": p["version"]} for p in npm_packages
        ],
        "limitations": "Package sources do not automatically include wheel/CLI/browser/native bundled dependencies or build patches. See docs/distribution-material.md.",
    }
    return inventory, plan, material


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        required=True,
        help="Docker export tar, never executed or extracted",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New release-evidence directory"
    )
    parser.add_argument(
        "--artifact-reference", required=True, help="Immutable image digest reference"
    )
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("Output already exists; choose a fresh directory")
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", args.artifact_reference):
            raise ValueError(
                "Artifact reference must include an immutable sha256 image digest"
            )
        digest = hashlib.sha256()
        with args.archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        with tarfile.open(args.archive, "r:*") as source:
            inventory, plan, material = collect(
                Archive(source), args.artifact_reference, digest.hexdigest()
            )
        args.output.mkdir(parents=True, exist_ok=False)
        for name, value in material.items():
            destination = args.output / "notices" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
        (args.output / "inventory.json").write_text(
            json.dumps(inventory, indent=2, sort_keys=True) + "\n"
        )
        (args.output / "source-retrieval-plan.json").write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n"
        )
        print(
            f"Collected {len(material)} notice files; {len(inventory['unresolved'])} unresolved evidence entries. Not legal clearance."
        )
        return 0
    except (ValueError, OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        print(f"Distribution material collection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
