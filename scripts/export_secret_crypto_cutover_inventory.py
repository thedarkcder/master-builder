from __future__ import annotations

import argparse
from pathlib import Path

from orchestrator.core.secret_crypto_cutover import (
    build_secret_crypto_cutover_report,
    render_secret_crypto_cutover_inventory,
)
from orchestrator.storage.db import create_session_factory


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a one-time inventory of managed secret refs and Jira OAuth connections before secret crypto cutover."
    )
    parser.add_argument(
        "--output",
        default=".codex/tmp/secret-crypto-cutover-inventory.md",
        help="Path to the markdown inventory output file.",
    )
    args = parser.parse_args()

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    session_factory = create_session_factory()
    with session_factory() as session:
        report = build_secret_crypto_cutover_report(session)

    output_path.write_text(render_secret_crypto_cutover_inventory(report), encoding="utf-8")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
