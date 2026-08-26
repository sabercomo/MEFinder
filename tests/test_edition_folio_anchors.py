from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from src.me_finder.edition_folio_anchors import (
    FolioBoundaryCandidate,
    _margin_folios,
    detect_folio_boundary_candidates,
    verify_folio_boundary_candidates,
)
from src.me_finder.persistence.index_schema import SCHEMA
from src.me_finder.text_alignment import align_segment_sequences


def _body(text: str, start: int, end: int, y0: int, y1: int):
    return {
        "text": text,
        "mineru_type": "text",
        "bbox": [180, y0, 820, y1],
        "page_char_start": start,
        "page_char_end": end,
        "mineru_item_index": 0,
    }


def _number(text: str, bbox, start: int, role: str = "page_number"):
    return {
        "text": text,
        "mineru_type": role,
        "bbox": bbox,
        "page_char_start": start,
        "page_char_end": start + len(text),
        "mineru_item_index": 1,
    }


class EditionFolioAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.executemany(
            "INSERT INTO source_files(source_file_id, source_type, file_name, payload_json) "
            "VALUES (?, 'pdf', ?, '{}')",
            (("pivot", "pivot.pdf"), ("translation", "translation.pdf")),
        )
        self.connection.executemany(
            "INSERT INTO segment_sets(segment_set_id, source_file_id, source_text_hash, "
            "segmenter, segmenter_version, language_code, created_at) "
            "VALUES (?, ?, 'hash', 'test', '1', 'x', 't')",
            (("pivot-set", "pivot"), ("target-set", "translation")),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def _add_page_pair(self, page_index: int, folio: int, *, marker_y: int = 300):
        pivot_text = f"{folio}\nsource body {folio}"
        pivot_payload = {
            "pdf_page_index": page_index,
            "page_width": 600,
            "page_height": 900,
            "text_raw": pivot_text,
            "printed_page": str(folio),
            "citation_page_number": folio,
            "page_mapping_confidence": 0.99,
            "blocks": [
                _number(str(folio), [480, 920, 520, 940], 0),
                _body(f"source body {folio}", len(str(folio)) + 1, len(pivot_text), 180, 780),
            ],
        }
        target_body = f"translated first {folio}. translated second {folio}."
        marker_start = len(target_body) + 1
        target_text = f"{target_body}\n{folio}\n1{page_index:02d}"
        target_payload = {
            "pdf_page_index": page_index,
            "page_width": 600,
            "page_height": 900,
            "text_raw": target_text,
            "printed_page": str(100 + page_index),
            "citation_page_number": 100 + page_index,
            "page_mapping_confidence": 0.99,
            "blocks": [
                _body(target_body, 0, len(target_body), 150, 750),
                _number(str(folio), [100, marker_y, 135, marker_y + 18], marker_start),
                _number(f"1{page_index:02d}", [470, 920, 530, 940], marker_start + len(str(folio)) + 1),
            ],
        }
        self.connection.executemany(
            "INSERT INTO pdf_pages(source_file_id, pdf_page_index, payload_json) VALUES (?, ?, ?)",
            (
                ("pivot", page_index, json.dumps(pivot_payload)),
                ("translation", page_index, json.dumps(target_payload)),
            ),
        )
        for side, set_id, source_id, text, split_at in (
            ("p", "pivot-set", "pivot", pivot_text, len(str(folio)) + 1),
            ("t", "target-set", "translation", target_text, len(target_body) // 2),
        ):
            for local_index, (start, end) in enumerate(
                ((0, split_at), (split_at, len(text)))
            ):
                order_index = page_index * 2 + local_index
                segment_id = f"{side}-{page_index}-{local_index}"
                self.connection.execute(
                    "INSERT INTO text_segments(segment_id, segment_set_id, order_index, text_raw) "
                    "VALUES (?, ?, ?, ?)",
                    (segment_id, set_id, order_index, text[start:end]),
                )
                self.connection.execute(
                    "INSERT INTO text_segment_spans(segment_id, source_file_id, pdf_page_index, "
                    "page_char_start, page_char_end, span_order) VALUES (?, ?, ?, ?, ?, 0)",
                    (segment_id, source_id, page_index, start, end),
                )

    def test_detects_sparse_margin_folios_and_ignores_edition_footer(self) -> None:
        for page_index, folio in enumerate((49, 51, 53, 55, 59)):
            self._add_page_pair(page_index, folio)
        self.connection.commit()

        result = detect_folio_boundary_candidates(
            self.connection, "pivot", "translation", "pivot-set", "target-set"
        )

        self.assertEqual([item.folio_number for item in result], [49, 51, 53, 55, 59])
        self.assertNotIn(100, [item.folio_number for item in result])

    def test_blank_role_top_folio_wins_over_margin_line_numbers(self) -> None:
        body_text = "Die Familie ist die unmittelbare Substantialität des Geistes."
        payload = {
            "page_width": 1000,
            "page_height": 1000,
            "blocks": [
                _body(body_text, 0, len(body_text), 180, 850),
                _number("144", [830, 70, 865, 90], len(body_text) + 1, ""),
                _number("5", [880, 320, 900, 340], len(body_text) + 5, ""),
                _number("10", [880, 450, 905, 470], len(body_text) + 7, ""),
                _number("15", [880, 570, 905, 590], len(body_text) + 10, ""),
                _number("164", [480, 920, 530, 940], len(body_text) + 13, ""),
            ],
        }

        self.assertEqual(
            [number for number, _bbox, _offset in _margin_folios(payload)],
            [144],
        )

    def test_visual_y_maps_marker_even_when_text_offset_is_after_body(self) -> None:
        for page_index, folio in enumerate((49, 51, 53, 55, 59)):
            self._add_page_pair(page_index, folio, marker_y=180)
        self.connection.commit()

        result = detect_folio_boundary_candidates(
            self.connection, "pivot", "translation", "pivot-set", "target-set"
        )

        self.assertEqual(result[0].target_segment_index, 0)

    def test_short_or_non_monotonic_noise_is_not_a_folio_stream(self) -> None:
        for page_index, folio in enumerate((49, 51, 50, 52)):
            self._add_page_pair(page_index, folio)
        self.connection.commit()

        result = detect_folio_boundary_candidates(
            self.connection, "pivot", "translation", "pivot-set", "target-set"
        )

        self.assertEqual(result, [])

    def test_semantics_confirm_or_reject_the_claimed_pivot_edition(self) -> None:
        for page_index, folio in enumerate((49, 51, 53, 55, 59)):
            self._add_page_pair(page_index, folio)
        self.connection.commit()
        candidates = detect_folio_boundary_candidates(
            self.connection, "pivot", "translation", "pivot-set", "target-set"
        )
        vector_count = 10
        pivot_vectors = np.tile(np.asarray([[1.0, 0.0]], dtype=np.float32), (vector_count, 1))
        matching_vectors = np.tile(
            np.asarray([[0.8, 0.2]], dtype=np.float32), (vector_count, 1)
        )
        different_edition_vectors = np.tile(
            np.asarray([[0.0, 1.0]], dtype=np.float32), (vector_count, 1)
        )

        verified = verify_folio_boundary_candidates(
            candidates, pivot_vectors, matching_vectors
        )
        rejected = verify_folio_boundary_candidates(
            candidates, pivot_vectors, different_edition_vectors
        )

        self.assertEqual(len(verified), 5)
        self.assertGreater(verified[0].similarity, 0.9)
        self.assertEqual(rejected, [])

    def test_verified_folios_are_sent_to_dp_as_partition_only_boundaries(self) -> None:
        candidates = [
            FolioBoundaryCandidate(number, index, index, index, (0.1, 0.2, 0.13, 0.22))
            for index, number in enumerate((49, 51, 53, 55), start=1)
        ]

        def matching_embeddings(texts, _cache_dir):
            return np.tile(np.asarray([[1.0, 0.0]], dtype=np.float32), (len(texts), 1))

        with tempfile.TemporaryDirectory() as temp_dir:
            links, anchors = align_segment_sequences(
                [f"source {index}" for index in range(6)],
                [f"target {index}" for index in range(6)],
                cache_dir=Path(temp_dir),
                embedding_provider=matching_embeddings,
                folio_candidates=candidates,
            )

        self.assertEqual(
            [anchor.key for anchor in anchors],
            ["folio:49", "folio:51", "folio:53", "folio:55"],
        )
        self.assertFalse(any(link.anchor_key.startswith("folio:") for link in links))


if __name__ == "__main__":
    unittest.main()
