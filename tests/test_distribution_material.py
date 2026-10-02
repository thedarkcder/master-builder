import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts/collect_distribution_material.py"
)
REFERENCE = "example/image@sha256:" + "a" * 64


def _archive(tmp_path, files, links=()):
    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as tar:
        for name, contents in files.items():
            value = contents.encode()
            member = tarfile.TarInfo(name)
            member.size = len(value)
            tar.addfile(member, io.BytesIO(value))
        for name, target in links:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            tar.addfile(member)
    return archive


def _run(archive, output):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--archive",
            str(archive),
            "--output",
            str(output),
            "--artifact-reference",
            REFERENCE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _debian():
    return {
        "var/lib/dpkg/status": (
            "Package: example-client\nStatus: install ok installed\nVersion: 1.2.3-1\n"
            "Architecture: amd64\nSource: example-source (1.2.3-1)\n\n"
        ),
        "usr/share/doc/example-client/copyright": "Copyright example fixture\n",
        "usr/share/common-licenses/GPL-3": "Example common-license fixture\n",
    }


def test_collects_actual_package_notice_bytes_and_source_versions(tmp_path):
    files = _debian() | {
        "opt/venv/lib/python3.11/site-packages/example_python-1.0.dist-info/METADATA": "Name: example-python\nVersion: 1.0\nLicense-Expression: LGPL-3.0-only\n",
        "opt/venv/lib/python3.11/site-packages/example_python-1.0.dist-info/licenses/LICENSE": "Example Python license fixture\n",
        "app/node_modules/@example/package/package.json": json.dumps(
            {"name": "@example/package", "version": "1.0.0", "license": "MIT"}
        ),
        "app/node_modules/@example/package/LICENSE": "Example npm license fixture\n",
        "root/.cache/ms-playwright/chromium-1/LICENSES.chromium.html": "Example browser attribution fixture\n",
        "app/LICENSE": "Example project license fixture\n",
        "app/.env": "PRIVATE_TEST_CONFIGURATION=DO_NOT_COPY\n",
    }
    archive = _archive(tmp_path, files)
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode == 0, result.stderr
    inventory = json.loads((output / "inventory.json").read_text())
    assert inventory["status"] == "collected-not-cleared"
    assert inventory["artifact_reference"] == REFERENCE
    assert len(inventory["archive_sha256"]) == 64
    assert inventory["os_packages"][0]["source_package"] == "example-source"
    assert inventory["os_packages"][0]["source_version"] == "1.2.3-1"
    assert inventory["python_packages"][0]["license_expression"] == "LGPL-3.0-only"
    assert inventory["npm_packages"][0]["name"] == "@example/package"
    copied = {notice["archive_path"]: notice for notice in inventory["notices"]}
    assert "app/.env" not in copied
    for name in files:
        if name.rsplit("/", 1)[-1].startswith(("LICENSE", "copyright", "GPL-")):
            assert (output / copied[name]["output_path"]).read_text() == files[name]
    plan = json.loads((output / "source-retrieval-plan.json").read_text())
    assert plan["debian_sources"] == [
        {"package": "example-source", "version": "1.2.3-1"}
    ]
    assert plan["python_sources"] == [{"package": "example-python", "version": "1.0"}]


def test_alpine_inventory_reports_unresolved_missing_notices(tmp_path):
    archive = _archive(
        tmp_path,
        {
            "lib/apk/db/installed": "P:example-package\nV:1.0-r0\nA:aarch64\no:example-source\nc:"
            + "b" * 40
            + "\n\n"
        },
    )
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode == 0, result.stderr
    inventory = json.loads((output / "inventory.json").read_text())
    assert inventory["os_packages"][0]["origin"] == "example-source"
    assert inventory["os_packages"][0]["aports_revision"] == "b" * 40
    assert any(item["kind"] == "missing-os-notice" for item in inventory["unresolved"])
    assert inventory["status"] == "collected-not-cleared"


def test_collects_copied_binary_credits_without_copying_source_or_config(tmp_path):
    name = "usr/local/share/master-builder/minio/CREDITS"
    archive = _archive(tmp_path, _debian() | {name: "Example upstream credits\n"})
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode == 0, result.stderr
    inventory = json.loads((output / "inventory.json").read_text())
    notice = next(item for item in inventory["notices"] if item["archive_path"] == name)
    assert (output / notice["output_path"]).read_text() == "Example upstream credits\n"


@pytest.mark.parametrize(
    "name",
    [
        "app/third_party/licenses/codex-0.160.0/ratatui/ratatui-0.30.2/LICENSE",
        "app/.codex/third-party-licenses/MIT-superpowers.txt",
    ],
)
def test_collects_retained_project_tool_and_guidance_license_directories(
    tmp_path, name
):
    archive = _archive(
        tmp_path, _debian() | {name: "Example retained upstream terms\n"}
    )
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode == 0, result.stderr
    inventory = json.loads((output / "inventory.json").read_text())
    notice = next(item for item in inventory["notices"] if item["archive_path"] == name)
    assert (
        output / notice["output_path"]
    ).read_text() == "Example retained upstream terms\n"


@pytest.mark.parametrize("metadata", [{"name": 123, "version": "1.0.0"}, [], None])
def test_rejects_malformed_npm_identity_without_partial_output_or_traceback(
    tmp_path, metadata
):
    archive = _archive(
        tmp_path,
        _debian() | {"app/node_modules/example/package.json": json.dumps(metadata)},
    )
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode != 0
    assert "malformed" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("receipt", [[], None])
def test_rejects_malformed_native_receipt_before_writing_output(tmp_path, receipt):
    archive = _archive(
        tmp_path,
        _debian()
        | {
            "root/.local/share/master-builder/libdave-receipt.json": json.dumps(receipt)
        },
    )
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode != 0
    assert "malformed" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    assert not output.exists()


def test_resolves_notice_symlink_within_archive_not_host_filesystem(tmp_path):
    files = _debian()
    files["usr/share/doc/example-source/copyright"] = files.pop(
        "usr/share/doc/example-client/copyright"
    )
    archive = _archive(
        tmp_path,
        files,
        [
            (
                "usr/share/doc/example-client/copyright",
                "/usr/share/doc/example-source/copyright",
            )
        ],
    )
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode == 0, result.stderr
    inventory = json.loads((output / "inventory.json").read_text())
    notice = next(
        x
        for x in inventory["notices"]
        if x["archive_path"] == "usr/share/doc/example-client/copyright"
    )
    assert (output / notice["output_path"]).read_text() == "Copyright example fixture\n"


@pytest.mark.parametrize("name", ["../outside", "/absolute", "usr/../../outside"])
def test_rejects_unsafe_archive_members_without_writing_output(tmp_path, name):
    archive = _archive(tmp_path, _debian() | {name: "unsafe"})
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode != 0
    assert "Unsafe archive path" in result.stderr
    assert not output.exists()


def test_rejects_escaping_notice_symlink(tmp_path):
    files = _debian()
    files.pop("usr/share/doc/example-client/copyright")
    archive = _archive(
        tmp_path,
        files,
        [("usr/share/doc/example-client/copyright", "../../../../../outside")],
    )
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode != 0
    assert "outside archive" in result.stderr
    assert not output.exists()


def test_rejects_existing_output_without_modifying_it(tmp_path):
    archive = _archive(tmp_path, _debian())
    output = tmp_path / "material"
    output.mkdir()
    (output / "keep.txt").write_text("keep")
    result = _run(archive, output)
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert (output / "keep.txt").read_text() == "keep"


def test_rejects_unknown_os_without_claiming_collection(tmp_path):
    archive = _archive(tmp_path, {"app/LICENSE": "fixture"})
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode != 0
    assert "Debian or Alpine package database" in result.stderr
    assert not output.exists()


def test_native_receipt_binds_notices_and_library_to_actual_export(tmp_path):
    import hashlib

    files = _debian() | {
        "root/.local/lib/libdave.so": "Example native binary fixture",
        "root/.local/licenses/libdave": "Example native license fixture",
    }
    receipt = {
        "version": "v1.1.0",
        "files": {
            "lib/libdave.so": hashlib.sha256(
                files["root/.local/lib/libdave.so"].encode()
            ).hexdigest(),
            "licenses/libdave": hashlib.sha256(
                files["root/.local/licenses/libdave"].encode()
            ).hexdigest(),
        },
    }
    files["root/.local/share/master-builder/libdave-receipt.json"] = json.dumps(receipt)
    archive = _archive(tmp_path, files)
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode == 0, result.stderr
    inventory = json.loads((output / "inventory.json").read_text())
    assert inventory["managed_receipts"][0]["receipt"]["version"] == "v1.1.0"
    assert inventory["managed_receipts"][0]["verified_files"] == 2


def test_modified_native_library_rejects_stale_receipt(tmp_path):
    files = _debian() | {
        "root/.local/lib/libdave.so": "Modified native binary fixture",
        "root/.local/share/master-builder/libdave-receipt.json": json.dumps(
            {"files": {"lib/libdave.so": "0" * 64}}
        ),
    }
    archive = _archive(tmp_path, files)
    output = tmp_path / "material"
    result = _run(archive, output)
    assert result.returncode != 0
    assert "receipt checksum" in result.stderr
    assert not output.exists()
