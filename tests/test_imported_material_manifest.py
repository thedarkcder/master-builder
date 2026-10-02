import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_imported_material_is_bound_to_current_bytes_and_immutable_license_evidence():
    manifest = ROOT / "third_party/imported-materials.json"
    assert manifest.is_file(), "Imported-material provenance manifest is missing"
    data = json.loads(manifest.read_text())
    assert data["schema_version"] == 1
    assert len(data["files"]) == 15
    for item in data["files"]:
        assert (
            hashlib.sha256((ROOT / item["local_path"]).read_bytes()).hexdigest()
            == item["local_sha256"]
        )
        assert item["original_import_revision"] is None
        assert len(item["verified_reference_revision"]) == 40
        assert item["license"] == "MIT"
        assert item["verified_reference_revision"] in item["source_url"]
        assert (
            hashlib.sha256((ROOT / item["license_file"]).read_bytes()).hexdigest()
            == item["license_sha256"]
        )
        assert len(item["license_revision"]) == 40
        assert item["license_revision"] in item["license_source_url"]
    exact = [item for item in data["files"] if item["relationship"] == "verbatim"]
    assert len(exact) == 2
    assert all(item["local_sha256"] == item["upstream_sha256"] for item in exact)


def test_guidance_manifest_matches_root_provenance_and_keeps_upstream_terms():
    manifest = ROOT / ".codex/imported-materials.json"
    assert manifest.is_file(), "Copied-guidance provenance manifest is missing"
    source = json.loads((ROOT / "third_party/imported-materials.json").read_text())
    guidance = json.loads(manifest.read_text())
    expected = [
        dict(
            item,
            license_file=".codex/third-party-licenses/"
            + Path(item["license_file"]).name,
        )
        for item in source["files"]
        if item["local_path"].startswith(".codex/")
    ]
    assert guidance["files"] == expected
    assert guidance["original_additions_license"] == "AGPL-3.0-only"
    for item in guidance["files"]:
        assert (
            hashlib.sha256((ROOT / item["license_file"]).read_bytes()).hexdigest()
            == item["license_sha256"]
        )


def test_distribution_notice_evidence_matches_retained_upstream_bytes():
    data = json.loads((ROOT / "third_party/distribution-evidence.json").read_text())
    assert data["status"] == "observed-evidence-not-distribution-clearance"
    for item in data["notices"]:
        assert (
            hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest()
            == item["sha256"]
        )
        assert len(item["revision"]) == 40
    for item in data["codex_ratatui_sources"]:
        assert len(item["crate_sha256"]) == 64
        for notice in item["notices"]:
            path = ROOT / "third_party/licenses/codex-0.160.0/ratatui" / notice["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == notice["sha256"]
    native = data["libdave_release"]
    assert len(native["assets"]) == 2
    assert len(native["license_sha256"]) == 4
    assert "historical" in native["status"]
    active = data["libdave_openssl3_source_build"]
    assert active["crypto"] == "OpenSSL3-only"
    assert len(active["sources"]) == 3
    assert all(
        len(item["revision"]) == 40 and len(item["archive_sha256"]) == 64
        for item in active["sources"]
    )
