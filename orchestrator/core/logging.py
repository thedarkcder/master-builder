import logging
import logging.config

from orchestrator.core.guardrails import SensitiveDataRedactionFilter


def configure_logging(level: str = "INFO") -> None:
    normalized_level = level.upper()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "sensitive_data_redaction": {
                    "()": SensitiveDataRedactionFilter,
                }
            },
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                }
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "level": normalized_level,
                    "formatter": "standard",
                    "filters": ["sensitive_data_redaction"],
                    "stream": "ext://sys.stdout",
                },
                "stderr": {
                    "class": "logging.StreamHandler",
                    "level": "ERROR",
                    "formatter": "standard",
                    "filters": ["sensitive_data_redaction"],
                    "stream": "ext://sys.stderr",
                },
            },
            "root": {
                "level": normalized_level,
                "handlers": ["stdout", "stderr"],
            },
            "loggers": {
                "master_builder.request": {
                    "level": normalized_level,
                    "handlers": ["stdout", "stderr"],
                    "propagate": False,
                }
            },
        }
    )
