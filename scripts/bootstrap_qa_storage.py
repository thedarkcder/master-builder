"""Create the dedicated private QA bucket and verify its retention policy."""

import os
from minio import Minio
from urllib3 import PoolManager, Timeout
from orchestrator.core.qa.storage_setup import bootstrap_bucket


def main() -> None:
    client = Minio(
        os.environ["QA_STORAGE_ENDPOINT"],
        access_key=os.environ["SEAWEEDFS_ADMIN_ACCESS_KEY"],
        secret_key=os.environ["SEAWEEDFS_ADMIN_SECRET_KEY"],
        secure=False,
        http_client=PoolManager(timeout=Timeout(connect=10, read=10), retries=False),
    )
    bootstrap_bucket(
        client=client,
        bucket=os.environ["ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET"],
        retention_days=int(os.environ["ORCHESTRATOR_QA_DEMO_ARTIFACT_RETENTION_DAYS"]),
    )
    print("Private QA bucket and retention policy verified")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Provider error details may contain credential-bearing request data.
        raise SystemExit(
            f"QA storage initialization failed ({type(exc).__name__}); verify required configuration and provider health"
        ) from None
