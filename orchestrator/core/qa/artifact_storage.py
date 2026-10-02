"""Private QA object storage operations; delivery URLs are never bearer credentials."""

from math import isfinite
from urllib.parse import unquote, urlsplit


def validate_object_key(key: str) -> str:
    parts = key.split("/")
    if (
        len(parts) < 4
        or any(part in {"", ".", ".."} for part in parts)
        or any(character in key for character in ("\\", "\x00", "%"))
        or any(ord(c) < 32 for c in key)
    ):
        raise ValueError("QA artifact requires an exact tenant/project/run object path")
    return key


def object_key_from_delivery_url(url: str, *, public_base_url: str) -> str:
    base = public_base_url.rstrip("/") + "/"
    parsed = urlsplit(url)
    if not url.startswith(base) or parsed.query or parsed.fragment:
        raise ValueError(
            "QA artifact URL does not belong to configured authenticated delivery"
        )
    return validate_object_key(unquote(url[len(base) :]))


def private_storage_client(storage, *, timeout_seconds: float):  # noqa: ANN001
    from minio import Minio
    from urllib3 import PoolManager, Timeout

    if not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("QA storage timeout must be positive")
    return Minio(
        storage.endpoint,
        access_key=storage.access_key,
        secret_key=storage.secret_key,
        secure=storage.secure,
        http_client=PoolManager(
            timeout=Timeout(connect=timeout_seconds, read=timeout_seconds),
            retries=False,
        ),
    )
