import unittest

from src.me_finder.collection_metadata import infer_collection_metadata


class CollectionMetadataTests(unittest.TestCase):
    def test_literal_collection_and_complete_works_rules(self) -> None:
        cases = {
            "《马克思恩格斯文集》第1卷.docx": (
                "article_collection", "马克思、恩格斯", "马克思恩格斯文集"
            ),
            "马恩全集 第12卷.pdf": (
                "complete_works", "马克思、恩格斯", "马克思恩格斯全集"
            ),
            "《黑格尔全集》第3卷": (
                "complete_works", "黑格尔", "黑格尔全集"
            ),
            "杜威文集 第8卷": (
                "article_collection", "杜威", "杜威文集"
            ),
            "毛泽东选集 第1卷": (
                "selected_works", "毛泽东", "毛泽东选集"
            ),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                metadata = infer_collection_metadata(title)
                self.assertEqual(
                    (
                        metadata["primary_structure"],
                        metadata["author"],
                        metadata["collection_title"],
                    ),
                    expected,
                )

    def test_plain_title_is_not_treated_as_a_collection(self) -> None:
        self.assertEqual(infer_collection_metadata("法哲学原理.docx"), {})

    def test_thematic_collection_does_not_invent_a_personal_author(self) -> None:
        metadata = infer_collection_metadata("中国哲学文集 第1卷")
        self.assertEqual(metadata["primary_structure"], "article_collection")
        self.assertNotIn("author", metadata)


if __name__ == "__main__":
    unittest.main()
