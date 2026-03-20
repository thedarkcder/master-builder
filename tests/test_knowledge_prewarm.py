from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.config import Settings
from orchestrator.core.knowledge_prewarm import prewarm_knowledge_dependencies


class KnowledgePrewarmTests(unittest.TestCase):
    def test_prewarm_prefers_local_cache_before_network_fallback(self) -> None:
        settings = Settings()

        with patch(
            "orchestrator.core.knowledge_prewarm.ensure_knowledge_embedding_model_ready",
            side_effect=[RuntimeError("missing cache"), "BAAI/bge-small-en-v1.5"],
        ) as prewarm_mock:
            result = prewarm_knowledge_dependencies(settings=settings)

        assert prewarm_mock.call_args_list == [
            unittest.mock.call(local_files_only=True),
            unittest.mock.call(local_files_only=False),
        ]
        self.assertEqual(result.embedding_model, "BAAI/bge-small-en-v1.5")

    def test_prewarm_uses_local_cache_when_available(self) -> None:
        settings = Settings()

        with patch(
            "orchestrator.core.knowledge_prewarm.ensure_knowledge_embedding_model_ready",
            return_value="BAAI/bge-small-en-v1.5",
        ) as prewarm_mock:
            result = prewarm_knowledge_dependencies(settings=settings)

        prewarm_mock.assert_called_once_with(local_files_only=True)
        self.assertEqual(result.embedding_model, "BAAI/bge-small-en-v1.5")


if __name__ == "__main__":
    unittest.main()
