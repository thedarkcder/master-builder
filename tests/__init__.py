import os


# Test package marker for direct module imports such as `tests.production_path_support`.
os.environ.setdefault("ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS", "true")
