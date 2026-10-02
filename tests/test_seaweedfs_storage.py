"""Local S3 ownership/configuration contracts and migration integrity."""

from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


def test_seaweed_identity_has_only_explicit_s3_object_operations():
    from orchestrator.core.qa.storage_setup import identity_config

    config = identity_config(bucket="qa-demos")
    app = next(i for i in config["identities"] if i["name"] == "qa-artifacts")
    assert app.get("actions", []) == []
    assert app["policyNames"] == ["qa-artifacts"]
    policy = json.loads(config["policies"][0]["content"])
    assert policy["Statement"] == [
        {
            "Effect": "Allow",
            "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
            "Resource": ["arn:aws:s3:::qa-demos"],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject"],
            "Resource": ["arn:aws:s3:::qa-demos/*"],
        },
    ]
    assert "${SEAWEEDFS_ADMIN_SECRET_KEY}" in json.dumps(config)
    assert "${ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY}" in json.dumps(config)


@pytest.mark.parametrize(
    "bucket", ["", "UPPER", "../../other", "qa-*", "a", "127.0.0.1"]
)
def test_storage_bucket_contract_rejects_invalid_names(bucket):
    from orchestrator.core.qa.storage_setup import identity_config

    with pytest.raises(ValueError):
        identity_config(bucket=bucket)


def test_seaweed_backend_has_no_application_network_or_published_internal_ports():
    services = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]
    assert "minio" not in services and "minio-init" not in services
    backend = services["seaweedfs"]
    assert backend["networks"] == ["storage-backend"]
    assert not backend.get("ports")
    assert backend["user"] == "1000:1000"
    gateway = services["seaweedfs-s3"]
    assert gateway["networks"] == ["default", "storage-backend"]
    assert gateway["ports"] == ["127.0.0.1:${MASTER_BUILDER_S3_PORT:-60015}:8333"]
    assert "@sha256:" in gateway["image"]
    assert (
        services["run-worker"]["depends_on"]["seaweedfs-init"]["condition"]
        == "service_completed_successfully"
    )


class Store:
    """External S3 boundary transport for deterministic owned migration tests."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.responses = []

    def get_bucket_versioning(self, bucket):
        return SimpleNamespace(status="")

    def get_object_tags(self, bucket, key):
        return {}

    def list_objects(self, bucket, recursive):
        return [
            SimpleNamespace(object_name=key, size=len(value))
            for key, value in self.objects.items()
        ]

    def stat_object(self, bucket, key):
        from minio.error import S3Error

        if key not in self.objects:
            raise S3Error(
                response=None,
                code="NoSuchKey",
                message="missing",
                resource=None,
                request_id=None,
                host_id=None,
            )
        return SimpleNamespace(
            size=len(self.objects[key]),
            etag=str(hash(self.objects[key])),
            content_type="video/mp4",
            metadata={},
        )

    def get_object(self, bucket, key):
        response = BytesIO(self.objects[key])
        response.release_conn = lambda: None
        self.responses.append(response)
        return response

    def put_object(self, bucket, key, data, length, content_type, metadata=None):
        self.objects[key] = data.read(length)


def test_explicit_migration_copies_verifies_and_replays_without_overwriting():
    from orchestrator.core.qa.storage_setup import migrate_objects

    source = Store({"tenant/project/run/demo.mp4": b"synthetic recording"})
    destination = Store()
    evidence = migrate_objects(
        source=source, destination=destination, bucket="qa-demos", max_object_bytes=100
    )
    assert evidence == {"copied": 1, "already_verified": 0, "bytes": 19}
    assert source.objects == destination.objects
    assert all(response.closed for response in source.responses + destination.responses)
    assert (
        migrate_objects(
            source=source,
            destination=destination,
            bucket="qa-demos",
            max_object_bytes=100,
        )["already_verified"]
        == 1
    )
    destination.objects["tenant/project/run/demo.mp4"] = b"unrelated work"
    with pytest.raises(RuntimeError, match="differs"):
        migrate_objects(
            source=source,
            destination=destination,
            bucket="qa-demos",
            max_object_bytes=100,
        )
    assert destination.objects["tenant/project/run/demo.mp4"] == b"unrelated work"


def test_migration_rejects_versioned_source_and_oversized_objects():
    from orchestrator.core.qa.storage_setup import migrate_objects

    source = Store({"tenant/project/run/demo.mp4": b"synthetic recording"})
    source.get_bucket_versioning = lambda bucket: SimpleNamespace(status="Suspended")
    with pytest.raises(RuntimeError, match="version"):
        migrate_objects(
            source=source, destination=Store(), bucket="qa-demos", max_object_bytes=100
        )
    source.get_bucket_versioning = lambda bucket: SimpleNamespace(status="")
    with pytest.raises(RuntimeError, match="size"):
        migrate_objects(
            source=source, destination=Store(), bucket="qa-demos", max_object_bytes=1
        )


@pytest.mark.parametrize("stream", [b"x", b"grown beyond the advertised size"])
def test_migration_digest_rejects_source_stream_length_mismatch(stream):
    from orchestrator.core.qa.storage_setup import migrate_objects

    source = Store({"tenant/project/run/demo.mp4": b"expected"})

    def response(bucket, key):
        value = BytesIO(stream)
        value.release_conn = lambda: None
        source.responses.append(value)
        return value

    source.get_object = response
    with pytest.raises(RuntimeError, match="length"):
        migrate_objects(
            source=source, destination=Store(), bucket="qa-demos", max_object_bytes=10
        )
    assert all(value.closed for value in source.responses)


def test_migration_replay_rechecks_source_stat_after_hash_verification():
    from orchestrator.core.qa.storage_setup import migrate_objects

    source = Store({"tenant/project/run/demo.mp4": b"expected"})
    destination = Store(source.objects)
    original_stat = source.stat_object
    calls = []

    def changing_stat(bucket, key):
        value = original_stat(bucket, key)
        calls.append(key)
        if len(calls) > 1:
            value.etag = "changed"
        return value

    source.stat_object = changing_stat
    with pytest.raises(RuntimeError, match="changed"):
        migrate_objects(
            source=source,
            destination=destination,
            bucket="qa-demos",
            max_object_bytes=10,
        )


def test_migration_rejects_destination_stream_larger_than_reported_stat():
    from orchestrator.core.qa.storage_setup import migrate_objects

    source = Store({"tenant/project/run/demo.mp4": b"expected"})
    destination = Store(source.objects)

    def response(bucket, key):
        value = BytesIO(b"unexpected oversized destination")
        value.release_conn = lambda: None
        destination.responses.append(value)
        return value

    destination.get_object = response
    with pytest.raises(RuntimeError, match="length"):
        migrate_objects(
            source=source,
            destination=destination,
            bucket="qa-demos",
            max_object_bytes=10,
        )
    assert all(value.closed for value in destination.responses)


def test_gateway_liveness_requires_authentication_denial_without_anonymous_endpoint():
    services = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]
    command = services["seaweedfs-s3"]["healthcheck"]["test"]
    assert command[0] == "CMD-SHELL"
    assert "http://127.0.0.1:8333/" in command[1]
    assert "HTTP/1.1 403 Forbidden" in command[1]


def test_bootstrap_uses_installed_sdk_and_replays_private_retention():
    from orchestrator.core.qa.storage_setup import bootstrap_bucket

    class BucketTransport:
        def __init__(self):
            self.exists = False
            self.policy_deletions = 0
            self.lifecycle = None

        def bucket_exists(self, bucket):
            return self.exists

        def make_bucket(self, bucket):
            self.exists = True

        def delete_bucket_policy(self, bucket):
            self.policy_deletions += 1

        def set_bucket_lifecycle(self, bucket, lifecycle):
            self.lifecycle = lifecycle

        def get_bucket_lifecycle(self, bucket):
            return self.lifecycle

    transport = BucketTransport()
    bootstrap_bucket(client=transport, bucket="qa-demos", retention_days=30)
    bootstrap_bucket(client=transport, bucket="qa-demos", retention_days=30)
    assert transport.exists
    assert transport.policy_deletions == 2
    rule = transport.lifecycle.rules[0]
    assert rule.expiration.days == 30
    assert rule.noncurrent_version_expiration.noncurrent_days == 30
    assert rule.rule_filter.prefix == ""


def test_bootstrap_accepts_only_absent_bucket_policy():
    from minio.error import S3Error
    from orchestrator.core.qa.storage_setup import bootstrap_bucket

    class Transport:
        def bucket_exists(self, bucket):
            return True

        def delete_bucket_policy(self, bucket):
            raise S3Error(None, self.error_code, "provider message", None, None, None)

        def set_bucket_lifecycle(self, bucket, lifecycle):
            self.lifecycle = lifecycle

        def get_bucket_lifecycle(self, bucket):
            return self.lifecycle

    transport = Transport()
    transport.error_code = "NoSuchBucketPolicy"
    bootstrap_bucket(client=transport, bucket="qa-demos", retention_days=30)
    transport.error_code = "AccessDenied"
    with pytest.raises(S3Error):
        bootstrap_bucket(client=transport, bucket="qa-demos", retention_days=30)


def test_migration_preserves_recording_metadata_and_rejects_changed_replay():
    from orchestrator.core.qa.storage_setup import migrate_objects

    class MetadataStore(Store):
        def __init__(self, objects=None):
            super().__init__(objects)
            self.metadata = {}

        def stat_object(self, bucket, key):
            result = super().stat_object(bucket, key)
            result.metadata = self.metadata.get(key, {})
            return result

        def put_object(self, bucket, key, data, length, content_type, metadata=None):
            super().put_object(bucket, key, data, length, content_type)
            self.metadata[key] = metadata

    key = "tenant/project/run/demo.mp4"
    source = MetadataStore({key: b"expected"})
    source.metadata[key] = {"X-Amz-Meta-Origin": "synthetic", "ETag": "transport"}
    destination = MetadataStore()
    migrate_objects(
        source=source, destination=destination, bucket="qa-demos", max_object_bytes=10
    )
    assert destination.metadata[key] == {"x-amz-meta-origin": "synthetic"}
    destination.metadata[key] = {"x-amz-meta-origin": "changed"}
    with pytest.raises(RuntimeError, match="metadata"):
        migrate_objects(
            source=source,
            destination=destination,
            bucket="qa-demos",
            max_object_bytes=10,
        )


@pytest.mark.parametrize("tagged_side", ["source", "destination"])
def test_migration_rejects_tagged_objects_without_copy_or_equivalence_claim(
    tagged_side,
):
    from orchestrator.core.qa.storage_setup import migrate_objects

    key = "tenant/project/run/demo.mp4"
    source = Store({key: b"expected"})
    destination = Store({key: b"expected"} if tagged_side == "destination" else {})
    source.get_object_tags = lambda bucket, key: {}
    destination.get_object_tags = lambda bucket, key: {}
    store = source if tagged_side == "source" else destination
    store.get_object_tags = lambda bucket, key: {"preserve": "synthetic"}
    before = dict(destination.objects)
    with pytest.raises(RuntimeError, match="Tagged objects require"):
        migrate_objects(
            source=source,
            destination=destination,
            bucket="qa-demos",
            max_object_bytes=10,
        )
    assert destination.objects == before
    assert source.objects[key] == b"expected"


@pytest.mark.parametrize("unsupported_side", ["source", "destination"])
def test_migration_fails_clearly_when_object_tag_inspection_is_unsupported(
    unsupported_side,
):
    from minio.error import S3Error
    from orchestrator.core.qa.storage_setup import migrate_objects

    key = "tenant/project/run/demo.mp4"
    source = Store({key: b"expected"})
    destination = Store({key: b"expected"})
    source.get_object_tags = lambda bucket, key: {}
    destination.get_object_tags = lambda bucket, key: {}

    def unsupported(bucket, key):
        raise S3Error(
            None, "NotImplemented", "provider-private-detail", None, None, None
        )

    store = source if unsupported_side == "source" else destination
    store.get_object_tags = unsupported
    with pytest.raises(RuntimeError, match="Object tag inspection failed") as failure:
        migrate_objects(
            source=source,
            destination=destination,
            bucket="qa-demos",
            max_object_bytes=10,
        )
    assert "provider-private-detail" not in str(failure.value)
    assert destination.objects[key] == b"expected"


def test_migration_cli_reports_required_tag_inspection_without_provider_details(
    monkeypatch,
):
    import runpy
    from minio.error import S3Error

    key = "tenant/project/run/demo.mp4"
    source = Store({key: b"expected"})
    destination = Store()

    def unsupported(bucket, key):
        raise S3Error(
            None, "NotImplemented", "provider-private-detail", None, None, None
        )

    source.get_object_tags = unsupported
    clients = iter([source, destination])
    monkeypatch.setattr("minio.Minio", lambda *args, **kwargs: next(clients))
    for setting in (
        "QA_MIGRATION_SOURCE_ACCESS_KEY",
        "QA_MIGRATION_SOURCE_SECRET_KEY",
        "QA_MIGRATION_DESTINATION_ACCESS_KEY",
        "QA_MIGRATION_DESTINATION_SECRET_KEY",
    ):
        monkeypatch.setenv(setting, "EXAMPLE_PLACEHOLDER")
    monkeypatch.setattr(
        "sys.argv",
        [
            "migrate_qa_storage.py",
            "--source-endpoint",
            "source.example.invalid",
            "--destination-endpoint",
            "destination.example.invalid",
            "--bucket",
            "qa-demos",
            "--confirm-source-quiesced",
        ],
    )
    with pytest.raises(SystemExit) as exit_result:
        runpy.run_path("scripts/migrate_qa_storage.py", run_name="__main__")
    assert "GetObjectTagging" in str(exit_result.value)
    assert "provider-private-detail" not in str(exit_result.value)
    assert destination.objects == {}
