import argparse

from orchestrator.storage.migrations import run_migrations
from orchestrator.worker import main as worker_main


def main() -> None:
    parser = argparse.ArgumentParser(description="master-builder orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("worker", help="Run background worker loop")
    subparsers.add_parser("migrate", help="Apply DB migrations")

    args = parser.parse_args()
    if args.command == "worker":
        worker_main()
        return

    if args.command == "migrate":
        run_migrations()
        return


if __name__ == "__main__":
    main()
