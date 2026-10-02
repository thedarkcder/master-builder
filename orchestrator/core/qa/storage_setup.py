"""Explicit local S3 identities, retention bootstrap and verified migration."""

from hashlib import sha256
import ipaddress
import json
import re

from orchestrator.core.qa.artifact_storage import validate_object_key


def validate_bucket(bucket: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or ".." in bucket:
        raise ValueError("QA storage requires a valid 3–63 character DNS bucket name")
    try:
        ipaddress.ip_address(bucket)
    except ValueError:
        return bucket
    raise ValueError("QA storage bucket must not be an IP address")


def identity_config(*, bucket: str) -> dict:
    bucket = validate_bucket(bucket)
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            },
        ],
    }
    return {
        "identities": [
            {
                "name": "storage-administrator",
                "credentials": [
                    {
                        "accessKey": "${SEAWEEDFS_ADMIN_ACCESS_KEY}",
                        "secretKey": "${SEAWEEDFS_ADMIN_SECRET_KEY}",
                    }
                ],
                "actions": ["Admin"],
            },
            {
                "name": "qa-artifacts",
                "credentials": [
                    {
                        "accessKey": "${ORCHESTRATOR_QA_DEMO_ARTIFACT_ACCESS_KEY}",
                        "secretKey": "${ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY}",
                    }
                ],
                "policyNames": ["qa-artifacts"],
            },
        ],
        "policies": [{"name": "qa-artifacts", "content": json.dumps(policy)}],
    }


def bootstrap_bucket(*, client, bucket: str, retention_days: int) -> None:
    from minio.lifecycleconfig import (
        Expiration,
        LifecycleConfig,
        NoncurrentVersionExpiration,
        Rule,
    )
    from minio.commonconfig import ENABLED, Filter
    from minio.error import S3Error

    bucket = validate_bucket(bucket)
    if not 1 <= retention_days <= 365:
        raise ValueError("QA retention must be 1–365 days")
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    # This dedicated QA bucket belongs to this bootstrap. Remove any policy that
    # could re-enable anonymous access; do not copy legacy provider policies.
    try:
        client.delete_bucket_policy(bucket)
    except S3Error as exc:
        if exc.code != "NoSuchBucketPolicy":
            raise
    client.set_bucket_lifecycle(
        bucket,
        LifecycleConfig(
            [
                Rule(
                    ENABLED,
                    rule_filter=Filter(prefix=""),
                    rule_id="master-builder-qa-retention",
                    expiration=Expiration(days=retention_days),
                    noncurrent_version_expiration=NoncurrentVersionExpiration(
                        noncurrent_days=retention_days
                    ),
                )
            ]
        ),
    )
    configured = client.get_bucket_lifecycle(bucket).rules
    if (
        len(configured) != 1
        or configured[0].expiration.days != retention_days
        or configured[0].noncurrent_version_expiration.noncurrent_days != retention_days
    ):
        raise RuntimeError("QA retention policy did not persist exactly")


def _digest(client, bucket: str, key: str, *, expected_length: int) -> str:
    response = client.get_object(bucket, key)
    try:
        digest = sha256()
        total = 0
        for chunk in iter(
            lambda: response.read(min(64 * 1024, expected_length + 1)), b""
        ):
            total += len(chunk)
            if total > expected_length:
                raise RuntimeError(
                    "Object stream length exceeds the verified stat size"
                )
            digest.update(chunk)
        if total != expected_length:
            raise RuntimeError(
                "Object stream length is shorter than the verified stat size"
            )
        return digest.hexdigest()
    finally:
        response.close()
        response.release_conn()


def _object_metadata(stat) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in stat.metadata.items()
        if key.lower().startswith("x-amz-meta-")
    }


class ObjectTagContractError(RuntimeError):
    """A safe, owned diagnostic when object-tag preservation cannot be proved."""


def _require_untagged_object(client, bucket: str, key: str) -> None:
    try:
        tags = client.get_object_tags(bucket, key)
    except Exception:
        # Provider errors may include request data. Fail closed without copying
        # or claiming equivalence when this preservation contract is unverifiable.
        raise ObjectTagContractError(
            "Object tag inspection failed; migration requires supported and authorized GetObjectTagging"
        ) from None
    if tags:
        raise ObjectTagContractError(
            "Tagged objects require a separate tag-preserving migration; no equivalence is claimed"
        )


def migrate_objects(
    *, source, destination, bucket: str, max_object_bytes: int
) -> dict[str, int]:
    from minio.error import S3Error

    bucket = validate_bucket(bucket)
    if max_object_bytes < 1:
        raise ValueError("Migration object size limit must be positive")
    for client in (source, destination):
        if client.get_bucket_versioning(bucket).status in {"Enabled", "Suspended"}:
            raise RuntimeError(
                "Versioned storage requires an explicit version-preserving migration; no versions were copied"
            )
    counts = {"copied": 0, "already_verified": 0, "bytes": 0}
    for item in source.list_objects(bucket, recursive=True):
        key = validate_object_key(item.object_name)
        before = source.stat_object(bucket, key)
        if before.size < 1 or before.size > max_object_bytes:
            raise RuntimeError("Source object exceeds the migration size contract")
        _require_untagged_object(source, bucket, key)
        metadata = _object_metadata(before)
        expected = _digest(source, bucket, key, expected_length=before.size)
        try:
            existing = destination.stat_object(bucket, key)
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchObject"}:
                raise
        else:
            _require_untagged_object(destination, bucket, key)
            if (
                existing.content_type != before.content_type
                or _object_metadata(existing) != metadata
            ):
                raise RuntimeError(
                    "Existing destination metadata differs; refusing overwrite"
                )
            if (
                existing.size != before.size
                or _digest(destination, bucket, key, expected_length=before.size)
                != expected
            ):
                raise RuntimeError(
                    "Existing destination object differs; refusing overwrite"
                )
            after = source.stat_object(bucket, key)
            if after.size != before.size or after.etag != before.etag:
                raise RuntimeError(
                    "Source changed during verification; stop writers before retrying"
                )
            _require_untagged_object(source, bucket, key)
            counts["already_verified"] += 1
            continue
        response = source.get_object(bucket, key)
        try:
            destination.put_object(
                bucket,
                key,
                response,
                before.size,
                content_type=before.content_type,
                metadata=metadata,
            )
        finally:
            response.close()
            response.release_conn()
        after = source.stat_object(bucket, key)
        if after.size != before.size or after.etag != before.etag:
            raise RuntimeError(
                "Source changed during migration; stop writers before retrying"
            )
        _require_untagged_object(source, bucket, key)
        copied = destination.stat_object(bucket, key)
        _require_untagged_object(destination, bucket, key)
        if (
            copied.content_type != before.content_type
            or _object_metadata(copied) != metadata
        ):
            raise RuntimeError("Destination metadata differs; stop cutover")
        if _digest(destination, bucket, key, expected_length=before.size) != expected:
            raise RuntimeError("Destination digest does not match source; stop cutover")
        counts["copied"] += 1
        counts["bytes"] += before.size
    return counts
