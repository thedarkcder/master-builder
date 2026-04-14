import sys

from orchestrator.cli import main


def run(argv: list[str] | None = None) -> int:
    return main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(run())
