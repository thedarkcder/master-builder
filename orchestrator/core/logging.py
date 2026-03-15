import logging
import logging.config

from orchestrator.core.guardrails import SensitiveDataRedactionFilter
from orchestrator.core.observability import ObservabilityJsonFormatter


def configure_logging(level: str = "INFO", *, environment: str = "dev", platform_version: str = "unknown") -> None:
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
                "structured_json": {
                    "()": ObservabilityJsonFormatter,
                    "environment": environment,
                    "platform_version": platform_version,
                }
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "level": normalized_level,
                    "formatter": "structured_json",
                    "filters": ["sensitive_data_redaction"],
                    "stream": "ext://sys.stdout",
                },
                "stderr": {
                    "class": "logging.StreamHandler",
                    "level": "ERROR",
                    "formatter": "structured_json",
                    "filters": ["sensitive_data_redaction"],
                    "stream": "ext://sys.stderr",
                },
            },
            "root": {
                "level": normalized_level,
                "handlers": ["stdout", "stderr"],
            },
            "loggers": {
                "uvicorn": {
                    "level": normalized_level,
                    "handlers": ["stdout", "stderr"],
                    "propagate": False,
                },
                "uvicorn.error": {
                    "level": normalized_level,
                    "handlers": ["stdout", "stderr"],
                    "propagate": False,
                },
                "uvicorn.access": {
                    "level": normalized_level,
                    "handlers": ["stdout", "stderr"],
                    "propagate": False,
                }
            },
        }
    )
