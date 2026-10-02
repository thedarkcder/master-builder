"""Explicit verified QA object copy; never deletes source data or overwrites differences."""

import argparse
import json
import os
from minio import Minio
from urllib3 import PoolManager, Timeout
from orchestrator.core.qa.storage_setup import ObjectTagContractError, migrate_objects


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-endpoint", required=True)
    parser.add_argument("--destination-endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--confirm-source-quiesced", action="store_true", required=True)
    parser.add_argument("--source-insecure", action="store_true")
    parser.add_argument("--destination-insecure", action="store_true")
    parser.add_argument("--max-object-bytes", type=int, default=104857600)
    args = parser.parse_args()
    if args.source_endpoint == args.destination_endpoint:
        parser.error("Source and destination must be different endpoints")

    def client(endpoint: str, prefix: str, secure: bool):
        return Minio(
            endpoint,
            access_key=os.environ[f"{prefix}_ACCESS_KEY"],
            secret_key=os.environ[f"{prefix}_SECRET_KEY"],
            secure=secure,
            http_client=PoolManager(
                timeout=Timeout(connect=10, read=30), retries=False
            ),
        )

    evidence = migrate_objects(
        source=client(
            args.source_endpoint, "QA_MIGRATION_SOURCE", not args.source_insecure
        ),
        destination=client(
            args.destination_endpoint,
            "QA_MIGRATION_DESTINATION",
            not args.destination_insecure,
        ),
        bucket=args.bucket,
        max_object_bytes=args.max_object_bytes,
    )
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except ObjectTagContractError as exc:
        # These messages are owned constants, never raw provider response data.
        raise SystemExit(
            f"QA storage migration failed: {exc}; no cutover is authorized and source data was retained"
        ) from None
    except Exception as exc:
        raise SystemExit(
            f"QA storage migration failed ({type(exc).__name__}); no cutover is authorized and source data was retained"
        ) from None
