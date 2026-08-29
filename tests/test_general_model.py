"""通用本地模型（单个自部署 OpenAI 兼容端点）配置与接线测试。

后端复用 vision_api 的解析客户端，但配置独立、允许空 API Key，并通过保留的
provider_id 让导入经现有 vision 路径路由到它。
"""

from __future__ import annotations

import json
import tempfile
from unittest import mock
import unittest
from pathlib import Path

from src.me_finder import general_model as gm
from src.me_finder import vision_api as va
from src.me_finder.app_context import AppContext
from src.me_finder.application.document_import_coordinator import (
    DocumentImportCoordinator,
)
from src.me_finder.parser_settings_controller import ParserSettingsController


class GeneralModelConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.cfg = self.dir / "general_model.local.json"

    def test_empty_summary_is_unconfigured(self) -> None:
        summary = gm.general_model_summary(self.cfg)
        self.assertEqual(summary["provider_id"], "general-local-model")
        self.assertFalse(summary["configured"])
        self.assertFalse(summary["enabled"])
        self.assertFalse(summary["has_key"])

    def test_save_allows_empty_api_key(self) -> None:
        summary = gm.save_general_model_config(
            {
                "api_base": "http://127.0.0.1:8000/v1",
                "model": "qwen2.5-vl",
                "enabled": True,
            },
            self.cfg,
        )
        self.assertTrue(summary["configured"])
        self.assertTrue(summary["enabled"])
        self.assertFalse(summary["has_key"])
        # The summary never leaks the key material.
        self.assertNotIn("api_key", summary)

    def test_blank_key_on_edit_preserves_stored_key(self) -> None:
        gm.save_general_model_config(
            {
                "api_base": "http://127.0.0.1:8000/v1",
                "model": "qwen2.5-vl",
                "enabled": True,
                "api_key": "sk-local",
            },
            self.cfg,
        )
        summary = gm.save_general_model_config(
            {
                "api_base": "http://127.0.0.1:8000/v1",
                "model": "minicpm-v",
                "enabled": True,
            },
            self.cfg,
        )
        self.assertTrue(summary["has_key"])
        self.assertEqual(summary["model"], "minicpm-v")
        self.assertEqual(
            json.loads(self.cfg.read_text(encoding="utf-8"))["api_key"], "sk-local"
        )

    def test_save_rejects_missing_model_and_bad_url(self) -> None:
        with self.assertRaises(va.VisionAPIError):
            gm.save_general_model_config(
                {"api_base": "http://127.0.0.1:8000/v1", "model": ""}, self.cfg
            )
        with self.assertRaises(va.VisionAPIError):
            gm.save_general_model_config(
                {"api_base": "not-a-url", "model": "m"}, self.cfg
            )

    def test_load_provider_requires_enabled_and_complete(self) -> None:
        with self.assertRaises(va.VisionAPIError):
            gm.load_general_model_provider(self.cfg)  # unconfigured
        gm.save_general_model_config(
            {
                "api_base": "http://127.0.0.1:8000/v1",
                "model": "qwen2.5-vl",
                "enabled": False,
            },
            self.cfg,
        )
        with self.assertRaises(va.VisionAPIError):
            gm.load_general_model_provider(self.cfg)  # disabled
        gm.save_general_model_config(
            {
                "api_base": "http://127.0.0.1:8000/v1",
                "model": "qwen2.5-vl",
                "enabled": True,
            },
            self.cfg,
        )
        provider = gm.load_general_model_provider(self.cfg)
        self.assertEqual(provider.provider_id, "general-local-model")
        self.assertEqual(provider.model, "qwen2.5-vl")
        self.assertEqual(provider.api_key, "")


class GeneralModelDiscoveryTests(unittest.TestCase):
    def test_keyless_endpoint_can_list_models(self) -> None:
        """自部署端点通常没有 API Key，取模型不能因此被拒。

        vision 的 discover_vision_models 会先要求填 Key（面向托管服务商），
        通用本地模型必须绕过它、直接调用配置无关的 list_models。
        """

        d = Path(tempfile.mkdtemp())
        cfg = d / "general_model.local.json"
        seen: dict[str, object] = {}

        def fake_list_models(api_base, api_key, name, **kwargs):
            seen.update(
                {"api_base": api_base, "api_key": api_key, "name": name}
            )
            return {"models": [{"id": "qwen2.5-vl"}], "count": 1}

        with mock.patch.object(gm, "list_models", fake_list_models):
            result = gm.discover_general_model_models(
                {"api_base": "http://127.0.0.1:8000/v1", "model": ""}, cfg
            )

        self.assertEqual(result["count"], 1)
        self.assertEqual(seen["api_key"], "")  # 空 Key 被放行
        self.assertEqual(seen["api_base"], "http://127.0.0.1:8000/v1")

    def test_discovery_requires_an_address(self) -> None:
        d = Path(tempfile.mkdtemp())
        with self.assertRaises(va.VisionAPIError):
            gm.discover_general_model_models(
                {"api_base": "", "model": ""}, d / "general_model.local.json"
            )


class GeneralModelRoutingTests(unittest.TestCase):
    def test_load_vision_provider_delegates_reserved_id(self) -> None:
        d = Path(tempfile.mkdtemp())
        vision_cfg = d / "vision_api.local.json"
        gm.save_general_model_config(
            {
                "api_base": "http://127.0.0.1:8000/v1",
                "model": "qwen2.5-vl",
                "enabled": True,
            },
            d / "general_model.local.json",
        )
        provider = va.load_vision_provider("general-local-model", vision_cfg)
        self.assertEqual(provider.provider_id, "general-local-model")
        self.assertEqual(provider.model, "qwen2.5-vl")

    def test_import_accepts_reserved_id_as_vision_selection(self) -> None:
        mode, provider_id = DocumentImportCoordinator.validate_parse_options(
            "vision", "general-local-model"
        )
        self.assertEqual(mode, "vision")
        self.assertEqual(provider_id, "general-local-model")

    def test_general_model_mode_maps_to_vision_with_reserved_id(self) -> None:
        # The import radio sends mode="general-local-model" and no provider
        # header; the coordinator supplies the reserved id and routes to vision.
        mode, provider_id = DocumentImportCoordinator.validate_parse_options(
            "general-local-model", ""
        )
        self.assertEqual(mode, "vision")
        self.assertEqual(provider_id, "general-local-model")

    def test_general_model_is_a_valid_preference_parse_mode(self) -> None:
        from src.me_finder.preferences import VALID_PDF_PARSE_MODES

        self.assertIn("general-local-model", VALID_PDF_PARSE_MODES)


class GeneralModelControllerTests(unittest.TestCase):
    def _controller(self, root: Path) -> ParserSettingsController:
        context = AppContext.create(root)

        def _noop(*_args: object, **_kwargs: object) -> dict:
            return {}

        # The general-model methods use none of the injected mineru/vision ops.
        return ParserSettingsController(
            context.paths,
            mineru_account_service=None,
            test_mineru_credential=_noop,
            test_mineru_connection=_noop,
            discover_vision_models=_noop,
            test_vision_provider=_noop,
        )

    def test_controller_config_save_roundtrip(self) -> None:
        root = Path(tempfile.mkdtemp())
        controller = self._controller(root)

        status, empty = controller.general_model_config()
        self.assertEqual(status, 200)
        self.assertFalse(empty["configured"])

        status, saved = controller.save_general_model(
            {
                "api_base": "http://127.0.0.1:8000/v1",
                "model": "qwen2.5-vl",
                "enabled": True,
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(saved["ok"])
        self.assertTrue(saved["enabled"])

        status, reread = controller.general_model_config()
        self.assertEqual(status, 200)
        self.assertTrue(reread["configured"])
        self.assertEqual(reread["model"], "qwen2.5-vl")

    def test_controller_rejects_incomplete_test_and_models(self) -> None:
        root = Path(tempfile.mkdtemp())
        controller = self._controller(root)
        status, body = controller.test_general_model_connection(
            {"api_base": "", "model": ""}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, body = controller.general_model_models({"api_base": "", "model": ""})
        self.assertEqual(status, 400)
        self.assertTrue(body["manual_entry_allowed"])


if __name__ == "__main__":
    unittest.main()
