from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.me_finder.pdf_import_service import (
    parse_pdf_with_provider,
    register_pdf,
)
from src.me_finder.pdf_extractors import load_mineru_segments, mineru_profile
from src.me_finder.vision_api import (
    OpenAICompatibleVisionClient,
    VisionAPIError,
    VisionProviderConfig,
    _chat_endpoint,
    _message_text,
    _models_endpoint,
    discover_vision_models,
    read_vision_config_data,
    resolve_vision_config_path,
    save_vision_policy,
    save_vision_provider,
    vision_config_summary,
)


class _FakePixmap:
    def tobytes(self, output: str) -> bytes:
        assert output == "png"
        return b"fake-png"


class _FakeRect:
    width = 600
    height = 900


class _FakePage:
    rect = _FakeRect()

    def get_pixmap(self, **_kwargs):
        return _FakePixmap()


class _FakeDocument:
    def __init__(self, page_count: int = 2) -> None:
        self.page_count = page_count
        self.closed = False

    def __len__(self) -> int:
        return self.page_count

    def load_page(self, _index: int):
        return _FakePage()

    def close(self) -> None:
        self.closed = True


class _FakeFitz:
    @staticmethod
    def Matrix(x: float, y: float):
        return (x, y)

    @staticmethod
    def open(_path: str):
        return _FakeDocument()


class _FakeModelsResponse:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.body


class _FakeModelsOpener:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.request = None
        self.timeout = None

    def open(self, request, timeout: int):
        self.request = request
        self.timeout = timeout
        return _FakeModelsResponse(self.payload)


class VisionAPIConfigTests(unittest.TestCase):
    def test_provider_secrets_are_saved_but_never_returned(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vision_api.local.json"
            summary = save_vision_provider(
                {
                    "name": "通义千问",
                    "api_base": "https://example.test/v1",
                    "model": "qwen-vl-test",
                    "api_key": "secret-key",
                    "enabled": True,
                },
                path,
            )
            self.assertEqual(len(summary["providers"]), 1)
            provider = summary["providers"][0]
            self.assertTrue(provider["configured"])
            self.assertTrue(provider["has_api_key"])
            self.assertNotIn("api_key", provider)
            self.assertNotIn("secret-key", json.dumps(summary, ensure_ascii=False))

            provider_id = provider["id"]
            save_vision_provider(
                {
                    "id": provider_id,
                    "name": "千问（中转）",
                    "api_base": "https://relay.example.test/v1",
                    "model": "qwen-vl-test",
                    "api_key": "",
                    "enabled": True,
                },
                path,
            )
            stored = read_vision_config_data(path)
            self.assertEqual(stored["providers"][0]["api_key"], "secret-key")

    def test_paid_fallback_requires_an_explicit_default_provider(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vision_api.local.json"
            with self.assertRaises(VisionAPIError):
                save_vision_policy(
                    {
                        "default_provider_id": "",
                        "auto_fallback_from_mineru": True,
                    },
                    path,
                )

            saved = save_vision_provider(
                {
                    "name": "备用接口",
                    "api_base": "https://example.test/v1",
                    "model": "vision-model",
                    "api_key": "secret-key",
                    "enabled": True,
                },
                path,
            )
            provider_id = saved["providers"][0]["id"]
            policy = save_vision_policy(
                {
                    "default_provider_id": provider_id,
                    "auto_fallback_from_mineru": True,
                },
                path,
            )
            self.assertTrue(policy["auto_fallback_from_mineru"])
            self.assertEqual(policy["default_provider_id"], provider_id)

    def test_desktop_config_override_uses_private_local_path(self) -> None:
        override = Path(
            "C:/Users/example/AppData/Local/MEFinder/vision_api.local.json"
        )
        with patch.dict(
            "os.environ",
            {"ME_FINDER_VISION_CONFIG": str(override)},
        ):
            self.assertEqual(
                resolve_vision_config_path(Path("D:/portable/MEFinder")),
                override,
            )

    def test_openai_compatible_endpoint_and_content_variants(self) -> None:
        self.assertEqual(
            _chat_endpoint("https://example.test/v1"),
            "https://example.test/v1/chat/completions",
        )
        self.assertEqual(
            _chat_endpoint("https://example.test/v1/chat/completions"),
            "https://example.test/v1/chat/completions",
        )
        self.assertEqual(
            _message_text(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {"type": "text", "text": "第一页"},
                                    {"type": "text", "text": "第二段"},
                                ]
                            }
                        }
                    ]
                }
            ),
            "第一页\n第二段",
        )

    def test_openai_compatible_models_endpoint_variants(self) -> None:
        self.assertEqual(
            _models_endpoint("https://example.test/v1"),
            "https://example.test/v1/models",
        )
        self.assertEqual(
            _models_endpoint("https://example.test/v1/chat/completions"),
            "https://example.test/v1/models",
        )
        self.assertEqual(
            _models_endpoint("https://example.test/v1/models"),
            "https://example.test/v1/models",
        )

    def test_model_discovery_normalizes_models_without_saving_the_key(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vision_api.local.json"
            opener = _FakeModelsOpener(
                {
                    "data": [
                        {"id": "text-model", "owned_by": "relay"},
                        {"id": "qwen3-vl-plus", "owned_by": "qwen"},
                        {"id": "qwen3-vl-plus", "owned_by": "duplicate"},
                        {"id": ""},
                        None,
                    ]
                }
            )
            with patch(
                "src.me_finder.vision_api.urllib.request.build_opener",
                return_value=opener,
            ):
                result = discover_vision_models(
                    {
                        "name": "测试中转",
                        "api_base": "https://example.test/v1",
                        "api_key": "transient-secret",
                    },
                    path,
                )

            self.assertFalse(path.exists())
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["models"][0]["id"], "qwen3-vl-plus")
            self.assertTrue(result["models"][0]["likely_vision"])
            self.assertEqual(result["models"][1]["id"], "text-model")
            self.assertEqual(opener.request.get_method(), "GET")
            self.assertEqual(
                opener.request.full_url,
                "https://example.test/v1/models",
            )
            self.assertEqual(
                opener.request.get_header("Authorization"),
                "Bearer transient-secret",
            )
            self.assertNotIn(
                "transient-secret",
                json.dumps(result, ensure_ascii=False),
            )

    def test_model_discovery_can_reuse_an_existing_provider_key(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vision_api.local.json"
            saved = save_vision_provider(
                {
                    "name": "已保存接口",
                    "api_base": "https://example.test/v1",
                    "model": "old-model",
                    "api_key": "stored-secret",
                    "enabled": True,
                },
                path,
            )
            provider_id = saved["providers"][0]["id"]
            opener = _FakeModelsOpener({"models": ["vision-new", "text-new"]})
            with patch(
                "src.me_finder.vision_api.urllib.request.build_opener",
                return_value=opener,
            ):
                result = discover_vision_models(
                    {
                        "id": provider_id,
                        "api_base": "https://relay.example.test/v1",
                        "api_key": "",
                    },
                    path,
                )

            self.assertEqual(result["count"], 2)
            self.assertEqual(
                opener.request.get_header("Authorization"),
                "Bearer stored-secret",
            )
            self.assertEqual(
                opener.request.full_url,
                "https://relay.example.test/v1/models",
            )
            self.assertNotIn(
                "stored-secret",
                json.dumps(result, ensure_ascii=False),
            )

    def test_connection_uses_an_actual_image_input(self) -> None:
        opener = _FakeModelsOpener(
            {"choices": [{"message": {"content": "OK"}}]}
        )
        with patch(
            "src.me_finder.vision_api.urllib.request.build_opener",
            return_value=opener,
        ):
            client = OpenAICompatibleVisionClient(
                VisionProviderConfig(
                    provider_id="test",
                    name="测试接口",
                    api_base="https://example.test/v1",
                    api_key="secret",
                    model="vision-model",
                )
            )
            self.assertEqual(client.test_connection(), "OK")

        payload = json.loads(opener.request.data.decode("utf-8"))
        content = payload["messages"][0]["content"]
        image_item = next(item for item in content if item["type"] == "image_url")
        self.assertTrue(
            image_item["image_url"]["url"].startswith("data:image/png;base64,")
        )


class VisionAPIParserTests(unittest.TestCase):
    def test_parser_writes_page_results_and_attaches_generic_manifest(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "corpus" / "raw_pdf" / "sample.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-fake")
            document = register_pdf(root, pdf)
            provider_summary = save_vision_provider(
                {
                    "name": "测试视觉接口",
                    "api_base": "https://example.test/v1",
                    "model": "vision-model",
                    "api_key": "secret-key",
                    "enabled": True,
                },
                root / "config" / "vision_api.local.json",
            )
            provider_id = provider_summary["providers"][0]["id"]
            recognized = iter(["第一页文字", "第二页文字"])

            with patch(
                "src.me_finder.vision_api.load_pymupdf",
                return_value=_FakeFitz,
            ):
                with patch(
                    "src.me_finder.vision_api.OpenAICompatibleVisionClient.extract_page",
                    side_effect=lambda _image: next(recognized),
                ):
                    result = parse_pdf_with_provider(
                        root,
                        pdf,
                        str(document["source_file_id"]),
                        provider_id,
                    )

            manifest_path = Path(str(result["manifest_path"]))
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["total_pages"], 2)
            result_dir = root / manifest["segments"][0]["result_dir"]
            content = json.loads(
                (result_dir / "content_list.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["text"] for item in content],
                ["第一页文字", "第二页文字"],
            )
            segments = load_mineru_segments(
                {"parser_results": {"manifest": str(manifest_path)}}
            )
            with patch(
                "src.me_finder.pdf_extractors.load_pymupdf",
                return_value=None,
            ):
                profile = mineru_profile(pdf, segments)
            self.assertEqual(profile["detected_pdf_type"], "api_structured")
            self.assertEqual(profile["parser_label"], "测试视觉接口")
            imports = json.loads(
                (root / "config" / "pdf_imports.json").read_text(encoding="utf-8")
            )
            attached = imports["documents"][0]["parser_results"]
            self.assertEqual(attached["provider_id"], provider_id)
            self.assertNotIn("mineru", imports["documents"][0])
            public_summary = vision_config_summary(
                root / "config" / "vision_api.local.json"
            )
            self.assertNotIn(
                "secret-key",
                json.dumps(public_summary, ensure_ascii=False),
            )


if __name__ == "__main__":
    unittest.main()
