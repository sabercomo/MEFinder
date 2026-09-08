"""Runtime safety for embedding: thread headroom and cooperative cancel."""

from __future__ import annotations

import unittest
from unittest import mock

from src.me_finder import embedding_runtime, semantic_alignment


class EmbeddingThreadCountTests(unittest.TestCase):
    def test_apple_silicon_leaves_a_performance_core_free(self) -> None:
        # M4 base: 4 performance cores. Oversubscribing them beach-balls the UI,
        # so the cap must stay strictly below the P-core count.
        with mock.patch.object(
            embedding_runtime, "_macos_performance_core_count", return_value=4
        ):
            self.assertEqual(embedding_runtime.embedding_thread_count(), 3)

    def test_performance_core_cap_is_bounded(self) -> None:
        with mock.patch.object(
            embedding_runtime, "_macos_performance_core_count", return_value=16
        ):
            self.assertEqual(embedding_runtime.embedding_thread_count(), 6)

    def test_non_apple_platform_leaves_two_logical_cores(self) -> None:
        with mock.patch.object(
            embedding_runtime, "_macos_performance_core_count", return_value=None
        ), mock.patch("src.me_finder.embedding_runtime.os.cpu_count", return_value=12):
            self.assertEqual(embedding_runtime.embedding_thread_count(), 8)

    def test_thread_count_is_always_at_least_one(self) -> None:
        with mock.patch.object(
            embedding_runtime, "_macos_performance_core_count", return_value=None
        ), mock.patch("src.me_finder.embedding_runtime.os.cpu_count", return_value=1):
            self.assertEqual(embedding_runtime.embedding_thread_count(), 1)


class EmbeddingCancellationTests(unittest.TestCase):
    def tearDown(self) -> None:
        embedding_runtime.begin_embedding_run()

    def test_flag_lifecycle(self) -> None:
        embedding_runtime.begin_embedding_run()
        self.assertFalse(embedding_runtime.embedding_cancel_requested())
        embedding_runtime.request_embedding_cancel()
        self.assertTrue(embedding_runtime.embedding_cancel_requested())
        embedding_runtime.begin_embedding_run()
        self.assertFalse(embedding_runtime.embedding_cancel_requested())

    def test_provider_stops_between_batches_when_cancelled(self) -> None:
        model = semantic_alignment.embedding_model_config(
            semantic_alignment.DEFAULT_EMBEDDING_MODEL_ID
        )
        provider = semantic_alignment.FastEmbedEmbeddingProvider(model)

        emitted = []

        def fake_embed(texts, batch_size):
            # Cancel is requested after the first vector is produced; the loop
            # must raise before consuming the rest.
            for index in range(len(texts)):
                emitted.append(index)
                embedding_runtime.request_embedding_cancel()
                yield [float(index)]

        class _FakeTextEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def embed(self, texts, batch_size):
                return fake_embed(texts, batch_size)

        embedding_runtime.begin_embedding_run()
        with mock.patch.dict(
            "sys.modules",
            {"fastembed": mock.Mock(TextEmbedding=_FakeTextEmbedding)},
        ), mock.patch.object(semantic_alignment, "_write_model_receipt"):
            with self.assertRaises(semantic_alignment.SemanticAlignmentCancelled):
                provider(["one", "two", "three"], cache_dir=None)
        self.assertEqual(emitted, [0])

    def test_embed_texts_surfaces_cancellation_not_model_failure(self) -> None:
        # A cancel raised mid-run must reach the caller as a cancellation, not
        # be masked as "模型加载失败" by embed_texts' broad failure wrapper.
        def fake_embed(texts, batch_size):
            for index in range(len(texts)):
                embedding_runtime.request_embedding_cancel()
                yield [float(index)]

        class _FakeTextEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def embed(self, texts, batch_size):
                return fake_embed(texts, batch_size)

        with mock.patch.dict(
            "sys.modules",
            {"fastembed": mock.Mock(TextEmbedding=_FakeTextEmbedding)},
        ), mock.patch.object(semantic_alignment, "_write_model_receipt"):
            with self.assertRaises(semantic_alignment.SemanticAlignmentCancelled):
                semantic_alignment.embed_texts(
                    ["a", "b"], None, model_id="minilm-l12-v2"
                )

    def test_embed_texts_clears_stale_cancel_before_running(self) -> None:
        # A stale flag left by a prior shutdown must not fail a fresh run.
        embedding_runtime.request_embedding_cancel()

        def fake_embed(texts, batch_size):
            for index in range(len(texts)):
                yield [float(index)]

        class _FakeTextEmbedding:
            def __init__(self, *args, **kwargs):
                pass

            def embed(self, texts, batch_size):
                return fake_embed(texts, batch_size)

        with mock.patch.dict(
            "sys.modules",
            {"fastembed": mock.Mock(TextEmbedding=_FakeTextEmbedding)},
        ), mock.patch.object(semantic_alignment, "_write_model_receipt"):
            vectors = semantic_alignment.embed_texts(
                ["a", "b"], None, model_id="minilm-l12-v2"
            )
        self.assertEqual(vectors.shape, (2, 1))


if __name__ == "__main__":
    unittest.main()
