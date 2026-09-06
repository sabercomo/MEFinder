from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.me_finder.app_context import AppPaths
from src.me_finder.mineru_api import MinerUError
from src.me_finder.parser_settings_controller import ParserSettingsController
from src.me_finder.vision_api import VisionAPIError


class FakeAccountSummary:
    def __init__(
        self,
        account_id: str = "account-1",
        *,
        configured: bool = True,
        enabled: bool = True,
    ) -> None:
        self.account_id = account_id
        self.configured = configured
        self.enabled = enabled

    def to_dict(self):
        return {
            "account_id": self.account_id,
            "configured": self.configured,
            "enabled": self.enabled,
        }


class FakeStatistics:
    def to_dict(self):
        return {"parsed_book_count": 2, "parsed_page_count": 18}


class FakeAccountService:
    def __init__(self, *, accounts=None) -> None:
        self.accounts = list(
            [FakeAccountSummary()] if accounts is None else accounts
        )
        self.saved = []
        self.statistics = FakeStatistics()
        self.config_exists = False

    def list_accounts(self):
        return list(self.accounts)

    def usage_statistics(self):
        return self.statistics

    def save_account(self, **values):
        self.config_exists = True
        self.saved.append(values)
        summary = FakeAccountSummary(values.get("account_id") or "created")
        existing = next(
            (
                item
                for item in self.accounts
                if item.account_id == summary.account_id
            ),
            None,
        )
        if existing is None:
            self.accounts.append(summary)
        return summary

    def delete_account(self, account_id):
        account = self.get_account(account_id)
        self.accounts.remove(account)
        self.config_exists = True

    def private_config_exists(self):
        return self.config_exists

    def get_account(self, account_id):
        account = next(
            (item for item in self.accounts if item.account_id == account_id),
            None,
        )
        if account is None:
            raise KeyError(account_id)
        return account

    def resolve_secret(self, secret_ref):
        return f"token-for:{secret_ref}"


class ParserSettingsControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.paths = AppPaths.create(
            root,
            index_path=root / "data" / "index.sqlite3",
        )
        self.accounts = FakeAccountService()
        self.test_credential = Mock(return_value={"ok": True})
        self.test_connection = Mock(return_value={"ok": True})
        self.discover_models = Mock(
            return_value={"models": [{"id": "vision-model"}]}
        )
        self.test_vision = Mock(return_value={"ok": True})

    def _controller(self, **overrides):
        arguments = {
            "paths": self.paths,
            "mineru_account_service": self.accounts,
            "test_mineru_credential": self.test_credential,
            "test_mineru_connection": self.test_connection,
            "discover_vision_models": self.discover_models,
            "test_vision_provider": self.test_vision,
            "resolve_mineru_config": lambda root: root / "config/mineru.json",
            "read_mineru_config": lambda _path: {
                "api_base": "https://mineru.example/"
            },
            "summarize_mineru": lambda _path: {"configured": True},
            "save_mineru": Mock(return_value={"configured": True}),
            "clear_legacy_mineru": Mock(),
            "build_statistics": Mock(return_value={"total": {"books": 2}}),
            "resolve_vision_config": lambda root: root / "config/vision.json",
            "summarize_vision": lambda _path: {"providers": []},
            "save_vision": Mock(return_value={"providers": [{"id": "p"}]}),
            "delete_vision": Mock(return_value={"providers": []}),
            "save_vision_fallback": Mock(
                return_value={"auto_fallback_from_mineru": True}
            ),
        }
        arguments.update(overrides)
        return ParserSettingsController(**arguments)

    def test_read_endpoints_return_existing_payload_shapes(self) -> None:
        statistics_builder = Mock(return_value={"total": {"books": 2}})
        controller = self._controller(build_statistics=statistics_builder)

        status, accounts = controller.mineru_accounts()
        self.assertEqual(status, 200)
        self.assertTrue(accounts["configured"])
        self.assertEqual(accounts["api_base"], "https://mineru.example")
        self.assertEqual(accounts["accounts"][0]["account_id"], "account-1")
        self.assertEqual(
            controller.mineru_statistics(),
            (200, {"parsed_book_count": 2, "parsed_page_count": 18}),
        )
        self.assertEqual(
            controller.parser_statistics(),
            (200, {"total": {"books": 2}}),
        )
        statistics_builder.assert_called_once_with(
            self.paths.index_path,
            mineru_statistics=self.accounts.statistics,
        )
        self.assertEqual(
            controller.mineru_config(), (200, {"configured": True})
        )
        self.assertEqual(
            controller.vision_providers(), (200, {"providers": []})
        )
        self.assertFalse(accounts["local_deployment"]["enabled"])

    def test_local_mineru_save_and_test_use_injected_boundaries(self) -> None:
        save_local = Mock(
            return_value={
                "enabled": True,
                "endpoint": "http://127.0.0.1:8000",
                "backend": "pipeline",
            }
        )
        test_local = Mock(return_value={"ok": True, "latency_ms": 8})
        controller = self._controller(
            save_mineru_local=save_local,
            test_mineru_local=test_local,
        )
        payload = {
            "enabled": True,
            "endpoint": "http://127.0.0.1:8000",
            "backend": "pipeline",
        }

        self.assertEqual(
            controller.save_mineru_local_config(payload),
            (200, {"ok": True, **save_local.return_value}),
        )
        self.assertEqual(
            controller.test_mineru_local_config(payload),
            (200, test_local.return_value),
        )
        expected_path = self.paths.runtime_root / "config/mineru.json"
        save_local.assert_called_once_with(payload, expected_path)
        test_local.assert_called_once_with(payload, expected_path)

    def test_local_ocr_settings_use_injected_boundaries(self) -> None:
        summarize = Mock(return_value={"available": False, "engines": []})
        save = Mock(return_value={"available": True, "engines": []})
        test = Mock(return_value={"ok": True, "provider_id": "ndlocr-lite"})
        controller = self._controller(
            resolve_local_ocr_config=lambda root: root / "config/local_ocr.json",
            summarize_local_ocr=summarize,
            save_local_ocr=save,
            test_local_ocr=test,
        )
        payload = {"engines": {}}

        self.assertEqual(
            controller.local_ocr_config(),
            (200, summarize.return_value),
        )
        self.assertEqual(
            controller.save_local_ocr_config(payload),
            (200, {"ok": True, **save.return_value}),
        )
        self.assertEqual(
            controller.test_local_ocr_config({"provider_id": "ndlocr-lite"}),
            (200, test.return_value),
        )
        expected = self.paths.runtime_root / "config/local_ocr.json"
        summarize.assert_called_once_with(expected)
        save.assert_called_once_with(payload, expected)
        test.assert_called_once_with({"provider_id": "ndlocr-lite"}, expected)

    def test_local_ocr_installer_status_and_actions_use_injected_boundary(self) -> None:
        installer_summary = Mock(
            return_value={"supported": True, "engines": []}
        )
        save_local_ocr = Mock(
            return_value={"available": True, "engines": []}
        )
        installer_action = Mock(
            return_value={"supported": True, "engines": []}
        )
        controller = self._controller(
            summarize_local_ocr=Mock(
                return_value={"available": False, "engines": []}
            ),
            save_local_ocr=save_local_ocr,
            summarize_local_ocr_installer=installer_summary,
            manage_local_ocr_installer=installer_action,
        )
        request = {"provider_id": "ndlocr-lite", "action": "install"}

        status, config = controller.local_ocr_config()
        self.assertEqual(status, 200)
        self.assertEqual(config["installer"], installer_summary.return_value)
        status, saved = controller.save_local_ocr_config({"engines": {}})
        self.assertEqual(status, 200)
        self.assertEqual(saved["installer"], installer_summary.return_value)
        self.assertEqual(
            controller.manage_local_ocr_component(request),
            (200, {"ok": True, "installer": installer_action.return_value}),
        )
        self.assertEqual(installer_summary.call_count, 2)
        installer_action.assert_called_once_with(request)

    def test_managed_component_mapping_drives_actions_and_diagnostics(self) -> None:
        local_component = Mock()
        local_component.summary.return_value = {
            "supported": True,
            "engines": [],
        }
        local_component.perform.return_value = {
            "supported": True,
            "engines": [],
        }
        local_component.diagnostics.return_value = {
            "component_id": "local-ocr"
        }
        controller = self._controller(
            summarize_local_ocr=Mock(
                return_value={"available": False, "engines": []}
            ),
            managed_components={"local-ocr": local_component},
        )
        request = {"provider_id": "ndlocr-lite", "action": "validate"}

        status, config = controller.local_ocr_config()
        self.assertEqual(status, 200)
        self.assertEqual(config["installer"], local_component.summary.return_value)
        self.assertEqual(
            controller.manage_local_ocr_component(request),
            (
                200,
                {"ok": True, "installer": local_component.perform.return_value},
            ),
        )
        self.assertEqual(
            controller.component_diagnostics(),
            (200, {"components": [{"component_id": "local-ocr"}]}),
        )
        local_component.perform.assert_called_once_with(request)

    def test_embedding_model_component_uses_managed_component_boundary(self) -> None:
        component = Mock()
        component.summary.return_value = {
            "component_id": "text-alignment-models",
            "models": [],
        }
        component.perform.return_value = component.summary.return_value
        controller = self._controller(
            managed_components={"text-alignment-models": component}
        )
        request = {
            "model_id": "multilingual-e5-large",
            "action": "download",
        }

        self.assertEqual(
            controller.text_alignment_models_component(),
            (200, component.summary.return_value),
        )
        self.assertEqual(
            controller.manage_text_alignment_models_component(request),
            (200, {"ok": True, **component.perform.return_value}),
        )
        component.perform.assert_called_once_with(request)

    def test_managed_mineru_status_and_actions_share_local_deployment_payload(self) -> None:
        runtime_summary = Mock(
            return_value={"supported": True, "profiles": [], "service": {}}
        )
        runtime_action = Mock(return_value=runtime_summary.return_value)
        controller = self._controller(
            summarize_mineru_local=Mock(
                return_value={"enabled": False, "endpoint": "http://127.0.0.1:8000"}
            ),
            summarize_managed_mineru=runtime_summary,
            manage_managed_mineru=runtime_action,
        )
        request = {"profile": "pipeline", "action": "install"}

        status, accounts = controller.mineru_accounts()
        self.assertEqual(status, 200)
        self.assertEqual(
            accounts["local_deployment"]["managed_runtime"],
            runtime_summary.return_value,
        )
        self.assertEqual(
            controller.manage_mineru_local_component(request),
            (200, {"ok": True, "managed_runtime": runtime_action.return_value}),
        )
        runtime_action.assert_called_once_with(request)

    def test_read_errors_keep_existing_status_and_messages(self) -> None:
        broken_statistics = self._controller(
            build_statistics=Mock(side_effect=sqlite3.OperationalError("damaged"))
        )
        with patch(
            "src.me_finder.parser_settings_controller.logging.exception"
        ) as logged:
            response = broken_statistics.parser_statistics()
        self.assertEqual(
            response,
            (500, {"error": "本地解析统计无法读取，请稍后重试。"}),
        )
        logged.assert_called_once_with("Local parser statistics read failed")

        broken_config = self._controller(
            summarize_mineru=Mock(side_effect=MinerUError("bad config")),
            summarize_vision=Mock(side_effect=VisionAPIError("bad vision")),
        )
        self.assertEqual(
            broken_config.mineru_config(),
            (500, {"error": "本机 MinerU 配置文件无法读取。"}),
        )
        self.assertEqual(
            broken_config.vision_providers(),
            (500, {"error": "bad vision"}),
        )

    def test_startup_migrates_legacy_single_token_before_accounts_are_returned(
        self,
    ) -> None:
        self.accounts = FakeAccountService(accounts=[])
        controller = self._controller(
            load_mineru=lambda _path: SimpleNamespace(token="legacy-token"),
            normalize_mineru=lambda token: str(token),
            read_mineru_config=lambda _path: {
                "api_base": "https://mineru.example",
                "expires_at": "2027-01-01T00:00:00Z",
            },
        )

        controller.migrate_legacy_mineru_account()
        status, payload = controller.mineru_accounts()

        self.assertEqual(status, 200)
        self.assertEqual(payload["accounts"][0]["account_id"], "mineru-default")
        self.assertEqual(
            self.accounts.saved[0],
            {
                "account_id": "mineru-default",
                "display_name": "MinerU 账号 1",
                "token": "legacy-token",
                "enabled": True,
                "expires_at": "2027-01-01T00:00:00Z",
            },
        )

    def test_mineru_accounts_read_never_runs_legacy_migration(self) -> None:
        self.accounts = FakeAccountService(accounts=[])
        legacy_loader = Mock(side_effect=AssertionError("legacy migration ran"))
        controller = self._controller(load_mineru=legacy_loader)

        status, payload = controller.mineru_accounts()

        self.assertEqual(status, 200)
        self.assertEqual(payload["accounts"], [])
        legacy_loader.assert_not_called()
        self.assertEqual(self.accounts.saved, [])

    def test_startup_account_read_failure_is_reported_by_get(self) -> None:
        self.accounts.list_accounts = Mock(
            side_effect=OSError("account config unavailable")
        )
        controller = self._controller()

        controller.migrate_legacy_mineru_account()

        self.assertEqual(
            controller.mineru_accounts(),
            (400, {"error": "account config unavailable"}),
        )
        self.accounts.list_accounts.assert_called_once_with()

    def test_successful_account_save_clears_startup_migration_failure(
        self,
    ) -> None:
        self.accounts = FakeAccountService(accounts=[])
        original_save = self.accounts.save_account
        attempts = 0

        def fail_migration_once(**values):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("credential ledger unavailable")
            return original_save(**values)

        self.accounts.save_account = Mock(side_effect=fail_migration_once)
        controller = self._controller(
            load_mineru=lambda _path: SimpleNamespace(token="legacy-token"),
            normalize_mineru=lambda token: str(token),
        )

        controller.migrate_legacy_mineru_account()
        self.assertEqual(
            controller.mineru_accounts(),
            (400, {"error": "credential ledger unavailable"}),
        )

        status, payload = controller.save_mineru_account(
            {
                "account_id": "replacement",
                "display_name": "Replacement",
                "token": "replacement-token",
                "enabled": True,
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["saved_account_id"], "replacement")
        self.assertEqual(controller.mineru_accounts()[0], 200)

    def test_missing_or_invalid_legacy_config_is_not_a_startup_error(
        self,
    ) -> None:
        for failure in (
            OSError("legacy config missing"),
            MinerUError("legacy token invalid"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.accounts = FakeAccountService(accounts=[])
                controller = self._controller(
                    load_mineru=Mock(side_effect=failure)
                )

                controller.migrate_legacy_mineru_account()

                status, payload = controller.mineru_accounts()
                self.assertEqual(status, 200)
                self.assertEqual(payload["accounts"], [])

    def test_save_and_test_mineru_account_use_injected_boundaries(self) -> None:
        save_config = Mock(return_value={"configured": True})
        controller = self._controller(save_mineru=save_config)

        status, payload = controller.save_mineru_account(
            {
                "account_id": "account-1",
                "display_name": "Primary",
                "token": "new-token",
                "enabled": True,
                "api_base": "https://mineru.example/api",
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["saved_account_id"], "account-1")
        save_config.assert_called_once_with(
            {"api_base": "https://mineru.example/api"},
            self.paths.runtime_root / "config/mineru.json",
        )

        status, result = controller.test_mineru_account(
            {"account_id": "account-1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["account_id"], "account-1")
        self.test_credential.assert_called_once_with(
            "token-for:mineru-account:account-1",
            api_base="https://mineru.example/",
        )

    def test_delete_mineru_account_returns_refreshed_account_payload(self) -> None:
        controller = self._controller()

        status, payload = controller.save_mineru_account(
            {"action": "delete_account", "account_id": "account-1"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["deleted_account_id"], "account-1")
        self.assertEqual(payload["accounts"], [])

    def test_delete_mineru_account_validates_identity(self) -> None:
        controller = self._controller()

        self.assertEqual(
            controller.save_mineru_account({"action": "delete_account"}),
            (400, {"error": "请选择要删除的 MinerU 账号。"}),
        )
        self.assertEqual(
            controller.save_mineru_account(
                {"action": "delete_account", "account_id": "missing"}
            ),
            (404, {"error": "该 MinerU 账号不存在或已删除。"}),
        )

    def test_mineru_account_boundary_validation_is_unchanged(self) -> None:
        controller = self._controller()

        self.assertEqual(
            controller.save_mineru_account([]),
            (400, {"error": "MinerU 账号请求必须是 JSON 对象。"}),
        )
        self.assertEqual(
            controller.save_mineru_account(
                {"display_name": "Name", "token": "t", "enabled": "yes"}
            ),
            (400, {"error": "enabled 必须是布尔值。"}),
        )
        self.assertEqual(
            controller.save_mineru_service({"api_base": "not-a-url"}),
            (
                400,
                {
                    "error": (
                        "API 地址必须是以 http:// 或 https:// "
                        "开头的网址。"
                    )
                },
            ),
        )
        self.assertEqual(
            controller.test_mineru_account({}),
            (400, {"error": "请选择要测试的 MinerU 账号。"}),
        )

    def test_mineru_service_and_legacy_config_actions_keep_contracts(self) -> None:
        save_config = Mock(return_value={"configured": True})
        controller = self._controller(save_mineru=save_config)

        self.assertEqual(
            controller.save_mineru_service(
                {"api_base": "https://mineru.example/v1"}
            )[0],
            200,
        )
        self.assertEqual(
            controller.save_mineru_config({"token": "secret"}),
            (200, {"ok": True, "configured": True}),
        )
        self.assertEqual(controller.test_mineru_config(), (200, {"ok": True}))
        self.test_connection.assert_called_once_with(
            self.paths.runtime_root / "config/mineru.json"
        )

    def test_vision_provider_actions_and_failures_keep_contracts(self) -> None:
        save_provider = Mock(return_value={"providers": [{"id": "p"}]})
        delete_provider = Mock(return_value={"providers": []})
        save_policy = Mock(return_value={"auto_fallback_from_mineru": True})
        controller = self._controller(
            save_vision=save_provider,
            delete_vision=delete_provider,
            save_vision_fallback=save_policy,
        )
        config_path = self.paths.runtime_root / "config/vision.json"

        self.assertEqual(
            controller.update_vision_providers(
                {"action": "save_provider", "provider": {"id": "p"}}
            ),
            (200, {"ok": True, "providers": [{"id": "p"}]}),
        )
        save_provider.assert_called_once_with({"id": "p"}, config_path)
        self.assertEqual(
            controller.update_vision_providers(
                {"action": "delete_provider", "provider_id": "p"}
            ),
            (200, {"ok": True, "providers": []}),
        )
        delete_provider.assert_called_once_with("p", config_path)
        self.assertEqual(
            controller.update_vision_providers(
                {"action": "save_policy", "auto_fallback": True}
            ),
            (200, {"ok": True, "auto_fallback_from_mineru": True}),
        )
        self.assertEqual(
            controller.update_vision_providers({"action": "unknown"}),
            (400, {"error": "不支持的配置操作。"}),
        )

        self.assertEqual(
            controller.vision_models({"provider": {"id": "p"}}),
            (200, {"ok": True, "models": [{"id": "vision-model"}]}),
        )
        self.discover_models.assert_called_once_with({"id": "p"}, config_path)
        self.assertEqual(
            controller.vision_models({}),
            (
                400,
                {
                    "error": "解析接口配置格式无效。",
                    "manual_entry_allowed": True,
                },
            ),
        )
        self.assertEqual(
            controller.test_vision_provider({"provider_id": "p"}),
            (200, {"ok": True}),
        )
        self.test_vision.assert_called_once_with("p", config_path)

        failing = self._controller(
            discover_vision_models=Mock(
                side_effect=VisionAPIError("model lookup failed")
            ),
            test_vision_provider=Mock(
                side_effect=VisionAPIError("provider test failed")
            ),
        )
        self.assertEqual(
            failing.vision_models({"provider": {"id": "p"}}),
            (
                400,
                {
                    "error": "model lookup failed",
                    "manual_entry_allowed": True,
                },
            ),
        )
        self.assertEqual(
            failing.test_vision_provider({"provider_id": "p"}),
            (400, {"error": "provider test failed"}),
        )


if __name__ == "__main__":
    unittest.main()
