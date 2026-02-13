import logging
import unittest

from orchestrator.core.logging import configure_logging


class LoggingConfigTests(unittest.TestCase):
    def test_configure_logging_sets_uvicorn_loggers_to_structured_handlers(self) -> None:
        configure_logging("INFO", environment="test", platform_version="v-test")

        for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            target_logger = logging.getLogger(logger_name)
            self.assertFalse(target_logger.propagate)
            handler_names = {type(handler).__name__ for handler in target_logger.handlers}
            self.assertIn("StreamHandler", handler_names)

    def test_configure_logging_keeps_module_logger_using_root_handlers(self) -> None:
        configure_logging("INFO", environment="test", platform_version="v-test")

        module_logger = logging.getLogger("orchestrator.api.main")
        root_logger = logging.getLogger()
        self.assertTrue(module_logger.propagate)
        self.assertEqual(module_logger.handlers, [])
        self.assertGreaterEqual(len(root_logger.handlers), 1)
