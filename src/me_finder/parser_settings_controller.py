"""Transport-neutral JSON responses for parser provider settings."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable, Dict, Mapping, Tuple
from urllib.parse import urlparse

from .app_context import AppPaths
from .large_document.mineru_accounts import MinerUAccountService
from .mineru_api import (
    DEFAULT_MINERU_API_BASE,
    MinerUError,
    clear_legacy_mineru_token,
    load_mineru_config,
    mineru_config_summary,
    normalize_mineru_token,
    read_mineru_config_data,
    resolve_mineru_config_path,
    save_mineru_config,
)
from .mineru_local_settings import (
    mineru_local_config_summary,
    save_mineru_local_config,
    test_mineru_local_connection,
)
from .import_resume import ResumeManifestError
from .local_ocr_settings import (
    LocalOCRError,
    local_ocr_config_summary,
    resolve_local_ocr_config_path,
    save_local_ocr_config,
    test_local_ocr_engine,
)
from .parser_provider import ParserProviderError
from .parser_statistics import build_parser_statistics
from .vision_api import (
    VisionAPIError,
    delete_vision_provider,
    resolve_vision_config_path,
    save_vision_policy,
    save_vision_provider,
    vision_config_summary,
)


ParserSettingsResponse = Tuple[int, Dict[str, object]]
JSONOperation = Callable[..., Dict[str, object]]
PathResolver = Callable[[Path], Path]


class ParserSettingsController:
    """Coordinate MinerU and optional vision-provider configuration."""

    def __init__(
        self,
        paths: AppPaths,
        mineru_account_service: MinerUAccountService,
        *,
        test_mineru_credential: JSONOperation,
        test_mineru_connection: JSONOperation,
        discover_vision_models: JSONOperation,
        test_vision_provider: JSONOperation,
        resolve_mineru_config: PathResolver = resolve_mineru_config_path,
        read_mineru_config: JSONOperation = read_mineru_config_data,
        load_mineru: Callable[[Path], object] = load_mineru_config,
        normalize_mineru: Callable[[object], str] = normalize_mineru_token,
        summarize_mineru: JSONOperation = mineru_config_summary,
        save_mineru: JSONOperation = save_mineru_config,
        clear_legacy_mineru: Callable[[Path], None] = clear_legacy_mineru_token,
        summarize_mineru_local: JSONOperation = mineru_local_config_summary,
        save_mineru_local: JSONOperation = save_mineru_local_config,
        test_mineru_local: JSONOperation = test_mineru_local_connection,
        build_statistics: JSONOperation = build_parser_statistics,
        resolve_vision_config: PathResolver = resolve_vision_config_path,
        summarize_vision: JSONOperation = vision_config_summary,
        save_vision: JSONOperation = save_vision_provider,
        delete_vision: JSONOperation = delete_vision_provider,
        save_vision_fallback: JSONOperation = save_vision_policy,
        resolve_local_ocr_config: PathResolver = resolve_local_ocr_config_path,
        summarize_local_ocr: JSONOperation = local_ocr_config_summary,
        save_local_ocr: JSONOperation = save_local_ocr_config,
        test_local_ocr: JSONOperation = test_local_ocr_engine,
    ) -> None:
        self._paths = paths
        self._mineru_account_service = mineru_account_service
        self._test_mineru_credential = test_mineru_credential
        self._test_mineru_connection = test_mineru_connection
        self._discover_vision_models = discover_vision_models
        self._test_vision_provider = test_vision_provider
        self._resolve_mineru_config = resolve_mineru_config
        self._read_mineru_config = read_mineru_config
        self._load_mineru = load_mineru
        self._normalize_mineru = normalize_mineru
        self._summarize_mineru = summarize_mineru
        self._save_mineru = save_mineru
        self._clear_legacy_mineru = clear_legacy_mineru
        self._summarize_mineru_local = summarize_mineru_local
        self._save_mineru_local = save_mineru_local
        self._test_mineru_local = test_mineru_local
        self._build_statistics = build_statistics
        self._resolve_vision_config = resolve_vision_config
        self._summarize_vision = summarize_vision
        self._save_vision = save_vision
        self._delete_vision = delete_vision
        self._save_vision_fallback = save_vision_fallback
        self._resolve_local_ocr_config = resolve_local_ocr_config
        self._summarize_local_ocr = summarize_local_ocr
        self._save_local_ocr = save_local_ocr
        self._test_local_ocr = test_local_ocr
        self._legacy_migration_error: Exception | None = None

    def mineru_accounts(self) -> ParserSettingsResponse:
        try:
            return 200, self._mineru_accounts_payload()
        except (
            MinerUError,
            OSError,
            ValueError,
            json.JSONDecodeError,
            sqlite3.Error,
        ) as exc:
            return 400, {"error": str(exc)}

    def migrate_legacy_mineru_account(self) -> None:
        """Upgrade the former single-token config before serving requests."""

        try:
            accounts = self._mineru_account_service.list_accounts()
        except (
            MinerUError,
            OSError,
            ValueError,
            json.JSONDecodeError,
            sqlite3.Error,
        ) as exc:
            self._legacy_migration_error = exc
            return
        self._legacy_migration_error = None
        if accounts:
            try:
                self._clear_legacy_mineru(
                    self._resolve_mineru_config(self._paths.runtime_root)
                )
            except (MinerUError, OSError, json.JSONDecodeError) as exc:
                self._legacy_migration_error = exc
            return
        if self._mineru_account_service.private_config_exists():
            return
        legacy_path = self._resolve_mineru_config(self._paths.runtime_root)
        try:
            legacy = self._read_mineru_config(legacy_path)
            config = self._load_mineru(legacy_path)
            token = self._normalize_mineru(config.token)
        except (MinerUError, OSError, json.JSONDecodeError):
            return
        except (ValueError, sqlite3.Error) as exc:
            self._legacy_migration_error = exc
            return
        try:
            self._mineru_account_service.save_account(
                account_id="mineru-default",
                display_name="MinerU 账号 1",
                token=token,
                enabled=True,
                expires_at=str(legacy.get("expires_at") or "") or None,
            )
            self._clear_legacy_mineru(legacy_path)
        except (
            MinerUError,
            OSError,
            ValueError,
            json.JSONDecodeError,
            sqlite3.Error,
        ) as exc:
            self._legacy_migration_error = exc

    def mineru_statistics(self) -> ParserSettingsResponse:
        try:
            payload = self._mineru_account_service.usage_statistics().to_dict()
        except (OSError, sqlite3.Error) as exc:
            return 400, {"error": str(exc)}
        return 200, payload

    def parser_statistics(self) -> ParserSettingsResponse:
        try:
            payload = self._build_statistics(
                self._paths.index_path,
                mineru_statistics=(
                    self._mineru_account_service.usage_statistics()
                ),
            )
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error):
            logging.exception("Local parser statistics read failed")
            return 500, {
                "error": "本地解析统计无法读取，请稍后重试。"
            }
        return 200, payload

    def mineru_config(self) -> ParserSettingsResponse:
        config_path = self._resolve_mineru_config(self._paths.runtime_root)
        try:
            payload = self._summarize_mineru(config_path)
        except (MinerUError, OSError, json.JSONDecodeError):
            return 500, {"error": "本机 MinerU 配置文件无法读取。"}
        return 200, payload

    def vision_providers(self) -> ParserSettingsResponse:
        config_path = self._resolve_vision_config(self._paths.runtime_root)
        try:
            payload = self._summarize_vision(config_path)
        except VisionAPIError as exc:
            return 500, {"error": str(exc)}
        return 200, payload

    def save_mineru_account(
        self,
        payload: object,
    ) -> ParserSettingsResponse:
        if not isinstance(payload, dict):
            return 400, {"error": "MinerU 账号请求必须是 JSON 对象。"}
        if payload.get("action") == "delete_account":
            return self.delete_mineru_account(payload)
        try:
            if "enabled" in payload and not isinstance(
                payload["enabled"], bool
            ):
                raise MinerUError("enabled 必须是布尔值。")
            raw_api_base = str(payload.get("api_base") or "").strip()
            if raw_api_base:
                parsed_base = urlparse(raw_api_base)
                if (
                    parsed_base.scheme not in {"http", "https"}
                    or not parsed_base.netloc
                ):
                    raise MinerUError(
                        "API 地址必须是以 http:// 或 https:// 开头的网址。"
                    )
            summary = self._mineru_account_service.save_account(
                account_id=(
                    str(payload.get("account_id") or "").strip() or None
                ),
                display_name=str(payload.get("display_name") or ""),
                token=(
                    str(payload.get("token"))
                    if payload.get("token") is not None
                    else None
                ),
                enabled=bool(payload.get("enabled", True)),
                max_concurrency_override=(
                    int(payload["max_concurrency_override"])
                    if payload.get("max_concurrency_override") is not None
                    else None
                ),
                expires_at=(
                    str(payload.get("expires_at") or "")
                    if "expires_at" in payload
                    else None
                ),
            )
            self._legacy_migration_error = None
            if raw_api_base:
                self._save_mineru(
                    {"api_base": raw_api_base},
                    self._resolve_mineru_config(self._paths.runtime_root),
                )
            response = self._mineru_accounts_payload()
            response["saved_account_id"] = summary.account_id
        except (MinerUError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except (OSError, json.JSONDecodeError, sqlite3.Error):
            logging.exception("MinerU account configuration save failed")
            return 500, {
                "error": "MinerU 账号配置无法保存，请检查应用数据目录。"
            }
        return 200, response

    def delete_mineru_account(
        self,
        payload: Mapping[str, object],
    ) -> ParserSettingsResponse:
        account_id = str(payload.get("account_id") or "").strip()
        if not account_id:
            return 400, {"error": "请选择要删除的 MinerU 账号。"}
        try:
            self._mineru_account_service.delete_account(account_id)
            response = self._mineru_accounts_payload()
            response["deleted_account_id"] = account_id
        except KeyError:
            return 404, {"error": "该 MinerU 账号不存在或已删除。"}
        except (MinerUError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except (OSError, json.JSONDecodeError, sqlite3.Error):
            logging.exception("MinerU account deletion failed")
            return 500, {
                "error": "MinerU 账号无法删除，请检查应用数据目录。"
            }
        return 200, response

    def test_mineru_account(
        self,
        payload: object,
    ) -> ParserSettingsResponse:
        if not isinstance(payload, dict):
            return 400, {"error": "MinerU 连接测试请求必须是 JSON 对象。"}
        try:
            account_id = str(payload.get("account_id") or "").strip()
            if not account_id:
                raise MinerUError("请选择要测试的 MinerU 账号。")
            record = self._mineru_account_service.get_account(account_id)
            if not record.configured:
                raise MinerUError("该 MinerU 账号尚未保存有效 Token。")
            global_config = self._read_mineru_config(
                self._resolve_mineru_config(self._paths.runtime_root)
            )
            result = self._test_mineru_credential(
                self._mineru_account_service.resolve_secret(
                    f"mineru-account:{account_id}"
                ),
                api_base=str(
                    global_config.get("api_base")
                    or DEFAULT_MINERU_API_BASE
                ),
            )
            result["account_id"] = account_id
        except (MinerUError, KeyError) as exc:
            return 400, {"error": str(exc)}
        return 200, result

    def save_mineru_service(
        self,
        payload: object,
    ) -> ParserSettingsResponse:
        if not isinstance(payload, dict):
            return 400, {"error": "MinerU 服务设置请求必须是 JSON 对象。"}
        try:
            raw_api_base = str(payload.get("api_base") or "").strip()
            parsed_base = urlparse(raw_api_base)
            if (
                parsed_base.scheme not in {"http", "https"}
                or not parsed_base.netloc
            ):
                raise MinerUError(
                    "API 地址必须是以 http:// 或 https:// 开头的网址。"
                )
            self._save_mineru(
                {"api_base": raw_api_base},
                self._resolve_mineru_config(self._paths.runtime_root),
            )
            response = self._mineru_accounts_payload()
        except MinerUError as exc:
            return 400, {"error": str(exc)}
        except (OSError, json.JSONDecodeError):
            logging.exception("MinerU service address save failed")
            return 500, {"error": "MinerU 服务地址无法保存。"}
        return 200, response

    def save_mineru_config(
        self,
        payload: object,
    ) -> ParserSettingsResponse:
        config_path = self._resolve_mineru_config(self._paths.runtime_root)
        try:
            summary = self._save_mineru(payload, config_path)
        except MinerUError as exc:
            return 400, {"error": str(exc)}
        except (OSError, json.JSONDecodeError):
            return 500, {
                "error": "本机配置文件无法保存，请检查应用目录是否可写。"
            }
        return 200, {"ok": True, **summary}

    def test_mineru_config(self) -> ParserSettingsResponse:
        config_path = self._resolve_mineru_config(self._paths.runtime_root)
        try:
            result = self._test_mineru_connection(config_path)
        except MinerUError as exc:
            return 400, {"error": str(exc)}
        except (OSError, ValueError):
            return 500, {
                "error": "无法读取本机 MinerU 配置，请检查配置目录。"
            }
        return 200, result

    def save_mineru_local_config(
        self,
        payload: object,
    ) -> ParserSettingsResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "本地部署设置必须是 JSON 对象。"}
        config_path = self._resolve_mineru_config(self._paths.runtime_root)
        try:
            summary = self._save_mineru_local(payload, config_path)
        except (MinerUError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except (OSError, json.JSONDecodeError):
            logging.exception("MinerU Local configuration save failed")
            return 500, {"error": "本地部署设置无法保存。"}
        return 200, {"ok": True, **summary}

    def test_mineru_local_config(
        self,
        payload: object,
    ) -> ParserSettingsResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "本地部署连接测试必须是 JSON 对象。"}
        config_path = self._resolve_mineru_config(self._paths.runtime_root)
        try:
            result = self._test_mineru_local(payload, config_path)
        except (MinerUError, ParserProviderError, ValueError) as exc:
            return 400, {"error": str(exc)}
        return 200, result

    def local_ocr_config(self) -> ParserSettingsResponse:
        path = self._resolve_local_ocr_config(self._paths.runtime_root)
        try:
            return 200, self._summarize_local_ocr(path)
        except (OSError, ResumeManifestError, LocalOCRError):
            logging.exception("Local OCR configuration read failed")
            return 500, {"error": "本地 OCR 组件设置无法读取。"}

    def save_local_ocr_config(
        self,
        payload: object,
    ) -> ParserSettingsResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "本地 OCR 设置必须是 JSON 对象。"}
        path = self._resolve_local_ocr_config(self._paths.runtime_root)
        try:
            summary = self._save_local_ocr(payload, path)
        except (LocalOCRError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except (OSError, ResumeManifestError):
            logging.exception("Local OCR configuration save failed")
            return 500, {"error": "本地 OCR 组件设置无法保存。"}
        return 200, {"ok": True, **summary}

    def test_local_ocr_config(
        self,
        payload: object,
    ) -> ParserSettingsResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "本地 OCR 测试必须是 JSON 对象。"}
        path = self._resolve_local_ocr_config(self._paths.runtime_root)
        try:
            result = self._test_local_ocr(payload, path)
        except (
            LocalOCRError,
            OSError,
            ResumeManifestError,
            subprocess.SubprocessError,
        ) as exc:
            return 400, {"error": str(exc)}
        return 200, result

    def update_vision_providers(
        self,
        payload: Mapping[str, object],
    ) -> ParserSettingsResponse:
        config_path = self._resolve_vision_config(self._paths.runtime_root)
        action = str(payload.get("action") or "").strip().lower()
        try:
            if action == "save_provider":
                provider = payload.get("provider")
                if not isinstance(provider, dict):
                    raise VisionAPIError("解析接口配置格式无效。")
                summary = self._save_vision(provider, config_path)
            elif action == "delete_provider":
                summary = self._delete_vision(
                    str(payload.get("provider_id") or ""), config_path
                )
            elif action == "save_policy":
                summary = self._save_vision_fallback(payload, config_path)
            else:
                raise VisionAPIError("不支持的配置操作。")
        except VisionAPIError as exc:
            return 400, {"error": str(exc)}
        except (OSError, json.JSONDecodeError):
            return 500, {
                "error": "其他解析 API 配置无法保存，请检查配置目录。"
            }
        return 200, {"ok": True, **summary}

    def vision_models(
        self,
        payload: Mapping[str, object],
    ) -> ParserSettingsResponse:
        provider = payload.get("provider")
        if not isinstance(provider, dict):
            return 400, {
                "error": "解析接口配置格式无效。",
                "manual_entry_allowed": True,
            }
        try:
            result = self._discover_vision_models(
                provider,
                self._resolve_vision_config(self._paths.runtime_root),
            )
        except VisionAPIError as exc:
            return 400, {
                "error": str(exc),
                "manual_entry_allowed": True,
            }
        return 200, {"ok": True, **result}

    def test_vision_provider(
        self,
        payload: Mapping[str, object],
    ) -> ParserSettingsResponse:
        provider_id = str(payload.get("provider_id") or "").strip()
        try:
            result = self._test_vision_provider(
                provider_id,
                self._resolve_vision_config(self._paths.runtime_root),
            )
        except VisionAPIError as exc:
            return 400, {"error": str(exc)}
        return 200, result

    def _mineru_accounts_payload(self) -> Dict[str, object]:
        if self._legacy_migration_error is not None:
            raise self._legacy_migration_error
        accounts = self._mineru_account_service.list_accounts()
        statistics = self._mineru_account_service.usage_statistics()
        global_config = self._read_mineru_config(
            self._resolve_mineru_config(self._paths.runtime_root)
        )
        return {
            "configured": any(
                bool(getattr(item, "configured", False))
                and bool(getattr(item, "enabled", False))
                for item in accounts
            ),
            "api_base": str(
                global_config.get("api_base") or DEFAULT_MINERU_API_BASE
            ).rstrip("/"),
            "accounts": [item.to_dict() for item in accounts],
            "statistics": statistics.to_dict(),
            "local_deployment": self._summarize_mineru_local(
                self._resolve_mineru_config(self._paths.runtime_root)
            ),
        }
