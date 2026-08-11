from __future__ import annotations

import json
import io
import os
import stat
import sqlite3
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.me_finder.pdf_import_service import (
    parse_pdf_with_provider,
    register_pdf,
)
from src.me_finder.database import build_database
from src.me_finder.pdf_extractors import (
    import_run_record,
    load_mineru_segments,
    mineru_profile,
)
from src.me_finder.vision_api import (
    OpenAICompatibleVisionClient,
    VisionAPIError,
    VisionProviderConfig,
    _chat_endpoint,
    _message_text,
    _models_endpoint,
    _responses_endpoint,
    _responses_text,
    default_fallback_provider,
    delete_vision_provider,
    discover_vision_models,
    load_vision_provider,
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


class _CompatibilityOpener:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout: int):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(
                request.full_url,
                outcome,
                "fixture error",
                {},
                io.BytesIO(b'{"error":"fixture"}'),
            )
        return _FakeModelsResponse(outcome)


class VisionAPIConfigTests(unittest.TestCase):
    @staticmethod
    def _save_provider(
        path: Path,
        *,
        name: str,
        host: str,
        enabled: bool = True,
        provider_id: str = "",
        api_key: str = "secret-key",
    ) -> dict[str, object]:
        return save_vision_provider(
            {
                "id": provider_id,
                "name": name,
                "api_base": f"https://{host}/v1",
                "model": f"{name}-vision-model",
                "api_key": api_key,
                "enabled": enabled,
            },
            path,
        )

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
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode),
                    0o600,
                )

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

    def test_paid_fallback_automatically_uses_the_first_configured_provider(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vision_api.local.json"
            with self.assertRaises(VisionAPIError):
                save_vision_policy(
                    {
                        "auto_fallback_from_mineru": True,
                    },
                    path,
                )

            saved = self._save_provider(
                path,
                name="首个备用接口",
                host="first.example.test",
            )
            first_provider_id = saved["providers"][0]["id"]
            saved = self._save_provider(
                path,
                name="第二备用接口",
                host="second.example.test",
            )
            second_provider_id = saved["providers"][1]["id"]
            policy = save_vision_policy(
                {
                    # A stale or older client cannot override the ordered fallback.
                    "default_provider_id": second_provider_id,
                    "auto_fallback_from_mineru": True,
                },
                path,
            )
            self.assertTrue(policy["auto_fallback_from_mineru"])
            self.assertEqual(policy["default_provider_id"], first_provider_id)

    def test_disabling_first_provider_moves_fallback_to_second_and_reenabling_restores_first(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vision_api.local.json"
            summary = self._save_provider(
                path,
                name="首个接口",
                host="first.example.test",
            )
            first_provider_id = summary["providers"][0]["id"]
            summary = self._save_provider(
                path,
                name="第二接口",
                host="second.example.test",
            )
            second_provider_id = summary["providers"][1]["id"]
            save_vision_policy({"auto_fallback_from_mineru": True}, path)

            disabled = self._save_provider(
                path,
                provider_id=first_provider_id,
                name="首个接口",
                host="first.example.test",
                api_key="",
                enabled=False,
            )
            self.assertTrue(disabled["auto_fallback_from_mineru"])
            self.assertEqual(disabled["default_provider_id"], second_provider_id)

            reenabled = self._save_provider(
                path,
                provider_id=first_provider_id,
                name="首个接口",
                host="first.example.test",
                api_key="",
                enabled=True,
            )
            self.assertTrue(reenabled["auto_fallback_from_mineru"])
            self.assertEqual(reenabled["default_provider_id"], first_provider_id)

    def test_deleting_first_provider_moves_fallback_to_second_and_keeps_automatic_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vision_api.local.json"
            summary = self._save_provider(
                path,
                name="首个接口",
                host="first.example.test",
            )
            first_provider_id = summary["providers"][0]["id"]
            summary = self._save_provider(
                path,
                name="第二接口",
                host="second.example.test",
            )
            second_provider_id = summary["providers"][1]["id"]
            save_vision_policy({"auto_fallback_from_mineru": True}, path)

            deleted = delete_vision_provider(first_provider_id, path)

            self.assertTrue(deleted["auto_fallback_from_mineru"])
            self.assertEqual(deleted["default_provider_id"], second_provider_id)

    def test_last_unavailable_provider_turns_automatic_fallback_off(self) -> None:
        for operation in ("disable", "delete"):
            with self.subTest(operation=operation), TemporaryDirectory() as tmp:
                path = Path(tmp) / "vision_api.local.json"
                summary = self._save_provider(
                    path,
                    name="唯一接口",
                    host="only.example.test",
                )
                provider_id = summary["providers"][0]["id"]
                save_vision_policy({"auto_fallback_from_mineru": True}, path)

                if operation == "disable":
                    result = self._save_provider(
                        path,
                        provider_id=provider_id,
                        name="唯一接口",
                        host="only.example.test",
                        api_key="",
                        enabled=False,
                    )
                else:
                    result = delete_vision_provider(provider_id, path)

                self.assertFalse(result["auto_fallback_from_mineru"])
                self.assertIsNone(result["default_provider_id"])
                stored = read_vision_config_data(path)
                self.assertFalse(stored["auto_fallback_from_mineru"])

    def test_legacy_default_pointing_to_second_provider_is_normalized_to_first(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vision_api.local.json"
            path.write_text(
                json.dumps(
                    {
                        "providers": [
                            {
                                "id": "provider-first",
                                "name": "首个接口",
                                "api_base": "https://first.example.test/v1",
                                "model": "first-vision-model",
                                "api_key": "first-secret",
                                "enabled": True,
                            },
                            {
                                "id": "provider-second",
                                "name": "第二接口",
                                "api_base": "https://second.example.test/v1",
                                "model": "second-vision-model",
                                "api_key": "second-secret",
                                "enabled": True,
                            },
                        ],
                        "default_provider_id": "provider-second",
                        "auto_fallback_from_mineru": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            summary = vision_config_summary(path)
            runtime_provider = load_vision_provider(None, path)
            fallback_provider = default_fallback_provider(path)

            self.assertEqual(summary["default_provider_id"], "provider-first")
            self.assertTrue(summary["auto_fallback_from_mineru"])
            self.assertEqual(runtime_provider.provider_id, "provider-first")
            self.assertIsNotNone(fallback_provider)
            self.assertEqual(fallback_provider.provider_id, "provider-first")

    def test_automatic_fallback_policy_rejects_non_boolean_values(self) -> None:
        for invalid in (None, 0, 1, "false", "true"):
            with self.subTest(value=invalid), TemporaryDirectory() as tmp:
                path = Path(tmp) / "vision_api.local.json"
                with self.assertRaises(VisionAPIError):
                    save_vision_policy(
                        {"auto_fallback_from_mineru": invalid},
                        path,
                    )
                self.assertFalse(path.exists())

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
            _chat_endpoint("https://example.test"),
            "https://example.test/v1/chat/completions",
        )
        self.assertEqual(
            _responses_endpoint("https://example.test"),
            "https://example.test/v1/responses",
        )
        self.assertEqual(
            _responses_endpoint("https://example.test/v1"),
            "https://example.test/v1/responses",
        )
        self.assertEqual(
            _responses_endpoint("https://example.test/v1/responses"),
            "https://example.test/v1/responses",
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
        self.assertEqual(
            _responses_text(
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "第一页"},
                                {"type": "output_text", "text": "第二段"},
                            ],
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
        self.assertEqual(
            _models_endpoint("https://example.test"),
            "https://example.test/v1/models",
        )

    def test_model_discovery_falls_back_to_x_api_key_for_relay(self) -> None:
        with TemporaryDirectory() as tmp:
            opener = _CompatibilityOpener(
                [401, {"data": [{"id": "qwen3-vl-plus"}]}]
            )
            with patch(
                "src.me_finder.vision_api.urllib.request.build_opener",
                return_value=opener,
            ):
                result = discover_vision_models(
                    {
                        "name": "中转接口",
                        "api_base": "https://relay.example.test",
                        "api_key": "Bearer relay-secret",
                    },
                    Path(tmp) / "vision_api.local.json",
                )

        self.assertEqual(result["models"][0]["id"], "qwen3-vl-plus")
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(
            [request.full_url for request in opener.requests],
            [
                "https://relay.example.test/v1/models",
                "https://relay.example.test/v1/models",
            ],
        )
        self.assertEqual(
            opener.requests[0].get_header("Authorization"),
            "Bearer relay-secret",
        )
        self.assertIsNone(opener.requests[1].get_header("Authorization"))
        self.assertEqual(
            opener.requests[1].get_header("X-api-key"),
            "relay-secret",
        )

    def test_model_discovery_falls_back_to_unversioned_root(self) -> None:
        with TemporaryDirectory() as tmp:
            opener = _CompatibilityOpener(
                [404, {"models": ["vision-relay-model"]}]
            )
            with patch(
                "src.me_finder.vision_api.urllib.request.build_opener",
                return_value=opener,
            ):
                result = discover_vision_models(
                    {
                        "name": "旧式中转接口",
                        "api_base": "https://relay.example.test",
                        "api_key": "relay-secret",
                    },
                    Path(tmp) / "vision_api.local.json",
                )

        self.assertEqual(result["count"], 1)
        self.assertEqual(
            [request.full_url for request in opener.requests],
            [
                "https://relay.example.test/v1/models",
                "https://relay.example.test/models",
            ],
        )

    def test_model_discovery_reports_forbidden_as_list_permission(self) -> None:
        with TemporaryDirectory() as tmp:
            opener = _CompatibilityOpener([403] * 6)
            with patch(
                "src.me_finder.vision_api.urllib.request.build_opener",
                return_value=opener,
            ):
                with self.assertRaises(VisionAPIError) as raised:
                    discover_vision_models(
                        {
                            "name": "Responses 中转",
                            "api_base": "https://relay.example.test",
                            "api_key": "relay-secret",
                        },
                        Path(tmp) / "vision_api.local.json",
                    )

        self.assertIn("无权枚举模型", str(raised.exception))
        self.assertIn("不代表推理接口不可用", str(raised.exception))
        self.assertNotIn("API Key 无效", str(raised.exception))

    def test_model_discovery_normalizes_models_without_saving_the_key(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vision_api.local.json"
            opener = _FakeModelsOpener(
                {
                    "data": [
                        {"id": "deepseek-v4-flash", "owned_by": "deepseek"},
                        {"id": "qwen-long", "owned_by": "qwen"},
                        {"id": "qwen3-omni-flash", "owned_by": "qwen"},
                        {"id": "qwen3-vl-flash", "owned_by": "qwen"},
                        {"id": "qwen3.8-max", "owned_by": "qwen"},
                        {"id": "qwen-vl-ocr-2025-11-20", "owned_by": "qwen"},
                        {"id": "qwen-vl-ocr-latest", "owned_by": "qwen"},
                        {"id": "qwen3.5-ocr", "owned_by": "qwen"},
                        {"id": "vendor-document-ocr", "owned_by": "vendor"},
                        {
                            "id": "vendor-multimodal-model",
                            "owned_by": "vendor",
                            "input_modalities": ["text", "image"],
                        },
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
            self.assertEqual(result["count"], 12)
            self.assertEqual(
                [item["id"] for item in result["models"]],
                [
                    "qwen3.5-ocr",
                    "qwen-vl-ocr-latest",
                    "qwen-vl-ocr-2025-11-20",
                    "vendor-document-ocr",
                    "qwen3-vl-plus",
                    "qwen3-vl-flash",
                    "qwen3.8-max",
                    "vendor-multimodal-model",
                    "qwen3-omni-flash",
                    "qwen-long",
                    "text-model",
                    "deepseek-v4-flash",
                ],
            )
            by_id = {item["id"]: item for item in result["models"]}
            self.assertEqual(
                by_id["qwen3.5-ocr"]["capability_label"],
                "OCR专用 · 推荐",
            )
            self.assertEqual(
                by_id["qwen-vl-ocr-2025-11-20"]["capability_label"],
                "OCR专用 · 固定版本",
            )
            self.assertEqual(
                by_id["qwen3-vl-flash"]["capability_label"],
                "通用视觉 · 快速",
            )
            self.assertEqual(
                by_id["qwen3.8-max"]["capability_label"],
                "通用视觉",
            )
            self.assertEqual(
                by_id["qwen3-omni-flash"]["capability_label"],
                "全模态",
            )
            self.assertEqual(by_id["qwen-long"]["capability"], "text")
            self.assertFalse(by_id["qwen-long"]["likely_vision"])
            self.assertEqual(
                by_id["deepseek-v4-flash"]["capability_label"],
                "不支持图片",
            )
            self.assertFalse(by_id["deepseek-v4-flash"]["likely_vision"])
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

    def test_chat_completion_reuses_successful_relay_authentication(self) -> None:
        opener = _CompatibilityOpener(
            [
                401,
                {"choices": [{"message": {"content": "OK"}}]},
                {"choices": [{"message": {"content": "OK"}}]},
            ]
        )
        with patch(
            "src.me_finder.vision_api.urllib.request.build_opener",
            return_value=opener,
        ):
            client = OpenAICompatibleVisionClient(
                VisionProviderConfig(
                    provider_id="relay",
                    name="中转接口",
                    api_base="https://relay.example.test/v1",
                    api_key="relay-secret",
                    model="vision-model",
                )
            )
            self.assertEqual(client.test_connection(), "OK")
            self.assertEqual(client.test_connection(), "OK")

        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(
            [request.full_url for request in opener.requests],
            ["https://relay.example.test/v1/chat/completions"] * 3,
        )
        self.assertEqual(
            opener.requests[1].get_header("X-api-key"),
            "relay-secret",
        )
        self.assertEqual(
            opener.requests[2].get_header("X-api-key"),
            "relay-secret",
        )

    def test_responses_api_converts_image_input_and_reuses_protocol(self) -> None:
        response = {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}],
                }
            ]
        }
        opener = _CompatibilityOpener([response, response])
        with patch(
            "src.me_finder.vision_api.urllib.request.build_opener",
            return_value=opener,
        ):
            client = OpenAICompatibleVisionClient(
                VisionProviderConfig(
                    provider_id="responses-relay",
                    name="Responses 中转",
                    api_base="https://relay.example.test",
                    api_key="relay-secret",
                    model="gpt-5.6-sol",
                )
            )
            self.assertEqual(client.test_connection(), "OK")
            self.assertEqual(client.test_connection(), "OK")

        self.assertEqual(
            [request.full_url for request in opener.requests],
            ["https://relay.example.test/v1/responses"] * 2,
        )
        for request in opener.requests:
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(payload["model"], "gpt-5.6-sol")
            self.assertIn("input", payload)
            self.assertNotIn("messages", payload)
            self.assertNotIn("max_tokens", payload)
            image_item = next(
                part
                for item in payload["input"]
                for part in item["content"]
                if part["type"] == "input_image"
            )
            self.assertTrue(
                image_item["image_url"].startswith("data:image/png;base64,")
            )


class VisionAPIParserTests(unittest.TestCase):
    def test_relative_mineru_manifest_resolves_from_runtime_root(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as other:
            root = Path(tmp)
            relative_manifest = Path(
                "corpus/processed/mineru/manifests/segments-source.json"
            )
            relative_result = Path(
                "corpus/processed/mineru/results/segment-source"
            )
            manifest_path = root / relative_manifest
            manifest_path.parent.mkdir(parents=True)
            (root / relative_result).mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "api": "precision",
                        "segments": [
                            {
                                "data_id": "source-p1-1",
                                "page_ranges": "1-1",
                                "result_dir": relative_result.as_posix(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(other)
                segments = load_mineru_segments(
                    {"mineru": {"manifest": relative_manifest.as_posix()}},
                    root=root,
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(len(segments), 1)
            self.assertEqual(
                Path(str(segments[0]["result_dir"])),
                root / relative_result,
            )

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
            import_run = import_run_record(
                str(document["source_file_id"]),
                profile,
                "2026-07-29T00:00:00+00:00",
                "success",
            )
            self.assertEqual(
                import_run["import_resume"]["completed_pages"],
                [1, 2],
            )
            database_path = root / "data" / "resume.sqlite3"
            build_database(
                {"pdf_import_runs": [import_run]},
                database_path,
            )
            # sqlite3's context manager does not close the underlying handle;
            # explicitly close it so Windows can clean up TemporaryDirectory.
            with closing(sqlite3.connect(str(database_path))) as connection:
                payload_json = connection.execute(
                    "SELECT payload_json FROM pdf_import_runs"
                ).fetchone()[0]
            persisted_run = json.loads(payload_json)
            self.assertEqual(
                persisted_run["import_resume"]["completed_pages"],
                [1, 2],
            )
            imports = json.loads(
                (root / "config" / "pdf_imports.json").read_text(encoding="utf-8")
            )
            attached = imports["documents"][0]["parser_results"]
            self.assertEqual(attached["provider_id"], provider_id)
            self.assertEqual(attached["resume"]["completed_pages"], [1, 2])
            self.assertEqual(attached["resume"]["failed_pages"], [])
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
