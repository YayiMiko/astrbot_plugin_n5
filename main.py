"""Generate guarded NovelAI images through the official API."""

import asyncio
import base64
import binascii
import ctypes
import json
import os
import re
import secrets
import shutil
import sys
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from time import monotonic
from typing import TypedDict
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.core.star.filter.command import GreedyStr

try:
    from .identity_planner import identity_alias_key, plan_identities
    from .image_context import RequestImageContext, resolve_request_images
    from .novelai_tags import NovelAITagResolver
except ImportError:
    from identity_planner import identity_alias_key, plan_identities
    from image_context import RequestImageContext, resolve_request_images
    from novelai_tags import NovelAITagResolver

PLUGIN_NAME = "astrbot_plugin_n5"
NOVELAI_API_BASE_URL = "https://image.novelai.net"
NOVELAI_IMAGE_ENDPOINT = "/ai/generate-image"
NOVELAI_SUBSCRIPTION_ENDPOINT = "/user/subscription"
NOVELAI_MODEL = "nai-diffusion-5-curated"
NOVELAI_MODELS = {
    "v5c": NOVELAI_MODEL,
    "v5f": "nai-diffusion-5-full",
}
NOVELAI_MODEL_LABELS = {
    NOVELAI_MODELS["v5c"]: "V5C（Curated）",
    NOVELAI_MODELS["v5f"]: "V5F（Full）",
}
NOVELAI_PARAMS_VERSION = 4
NOVELAI_PAT_ENV = "NOVELAI_API_TOKEN"
DEFAULT_STEPS = 23
DEFAULT_NEGATIVE_PROMPT = ""
DEFAULT_PROMPT_PLANNER_PROVIDER_ID = "deepseek/deepseek-v4-flash-vision-exp"
DEFAULT_ARTIST_STRING_NAME = "千代NAI1"
DEFAULT_ARTIST_STRING = (
    "artist:deyui, artist:yukisiannn, artist:kani biimu, artist:shiromochi_sakura"
)
CREATIVE_REFERENCE_BEGIN = "[CREATIVE_REFERENCE_BEGIN]"
CREATIVE_REFERENCE_END = "[CREATIVE_REFERENCE_END]"
ORIGINAL_ARTIST_STYLE = "__NAI_ORIGINAL_STYLE__"
CHIBI_SOURCE_PATTERN = re.compile(
    r"(?:Q版|Ｑ版|q版|chibi|super[\s_-]*deformed)",
    re.IGNORECASE,
)
CHARACTER_SLOT_PATTERN = re.compile(
    r"__NAI_CHARACTER_SLOT_\d+__",
    re.IGNORECASE,
)
NAI_HARD_ROUTE_PATTERN = re.compile(
    r"^\s*/n5(?:\s+(?P<prompt>.*))?\s*$",
    re.IGNORECASE,
)
NAI_STATUS_HARD_ROUTE_PATTERN = re.compile(r"^\s*/n5 状态\s*$", re.IGNORECASE)
CHARACTER_SUBJECT_PATTERN = re.compile(
    r"(?<![a-z0-9_])(?:1\s*)?(girl|boy|other)(?![a-z0-9_])",
    re.IGNORECASE,
)
EXPLICIT_NUDITY_SOURCE_PATTERN = re.compile(
    r"(?:裸体|全裸|赤裸|一丝不挂|不穿衣服|脱光|\b(?:fully\s+)?nude\b|"
    r"\bnaked\b|\bwithout\s+clothes\b)",
    re.IGNORECASE,
)
CLOTHING_TAG_PATTERN = re.compile(
    r"(?i)(?<![a-z])(?:dress|uniform|shirt|blouse|coat|jacket|skirt|pants|"
    r"trousers|shorts|underwear|lingerie|swimsuit|bikini|robe|cape|cloak|"
    r"sweater|cardigan|stockings?|socks?|gloves?|boots?|shoes?|bra|panties|"
    r"leotard|bodysuit|armor|clothing|clothes)(?![a-z])"
)
NOVELAI_PROMPT_SIGNAL_PATTERN = re.compile(
    r"(?:\b(?:artist|character|copyright|series|rating)\s*:|"
    r"\b\d+(?:girls?|boys?|women|men)\b|"
    r"\b(?:solo|best quality|very aesthetic|absurdres)\b|"
    r"[{}\[\]]|::|\b[a-z0-9]+_[a-z0-9_]+\b)",
    re.IGNORECASE,
)
NOVELAI_ASCII_TAG_PATTERN = re.compile(
    r"[a-z0-9][a-z0-9 _.:+\-'/()\\]*",
    re.IGNORECASE,
)
NATURAL_LANGUAGE_SCRIPT_PATTERN = re.compile(
    r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]"
)
NOVELAI_CHARACTER_TAG_PATTERN = re.compile(
    r"(?<![a-z0-9_])[a-z0-9][a-z0-9_']+_\([a-z0-9][a-z0-9 _.'-]*\)(?![a-z0-9_])",
    re.IGNORECASE,
)
COMIC_PANEL_PATTERN = re.compile(r"(?i)\bpanel\s*([1-4])\b")
COMIC_TEXT_FORBID_PATTERN = re.compile(
    r"(?:无对白|不要对白|没有对白|无文字|不要文字|没有文字|纯画面|"
    r"\b(?:no text|no dialogue|visual only)\b)",
    re.IGNORECASE,
)
COMIC_TEXT_BLOCK_PATTERN = re.compile(r"(?i)\btext\s*:")
EXPLICIT_SUBJECT_COUNT_PATTERN = re.compile(
    r"(?<![a-z0-9_])\d+\s*(?:girls?|boys?|women|men|others?|people|persons?|characters?)"
    r"(?![a-z0-9_])",
    re.IGNORECASE,
)
PROMPT_PLANNER_SYSTEM_PROMPT_PATHS = (
    (
        Path(__file__).resolve().parent
        / "skills"
        / "novelai-n5-prompt-planner"
        / "references"
        / "runtime-system-prompt.txt"
    ),
    (
        Path(__file__).resolve().parent
        / "skills"
        / "novelai-n5-prompt-planner"
        / "references"
        / "runtime-semantic-expansion.txt"
    ),
)
COMIC_PLANNER_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent
    / "skills"
    / "novelai-n5-prompt-planner"
    / "references"
    / "runtime-comic-mode.txt"
)
COMIC_DRAW_PLANNER_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent
    / "skills"
    / "novelai-n5-prompt-planner"
    / "references"
    / "runtime-comic-draw-mode.txt"
)
COMIC_STORYBOARD_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent
    / "skills"
    / "novelai-n5-prompt-planner"
    / "references"
    / "runtime-comic-storyboard.txt"
)
PROMPT_KNOWLEDGE_DIR = (
    Path(__file__).resolve().parent
    / "skills"
    / "novelai-n5-prompt-planner"
    / "knowledge"
)
OFFICIAL_RULES_PATH = PROMPT_KNOWLEDGE_DIR / "official-rules.json"
OFFICIAL_SOURCE_MANIFEST_PATH = PROMPT_KNOWLEDGE_DIR / "source-manifest.json"
LOCAL_PREFERENCES_PATH = PROMPT_KNOWLEDGE_DIR / "local-preferences.json"
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF")
DEFAULT_GENERATION_SIZE = (832, 1216)
GENERATION_SIZE_PRESETS = {
    "竖图": (832, 1216),
    "横图": (1216, 832),
    "方图": (1024, 1024),
}


class ArtistLibraryState(TypedDict):
    """Persist one group or private-chat artist-string library."""

    presets: dict[str, str]


class ArtistUserState(TypedDict):
    """Persist one QQ user's active strings and generation preferences."""

    active_by_library: dict[str, str]
    negative_prompt_by_library: dict[str, str]
    last_prompt_by_library: dict[str, str]
    last_negative_prompt_by_library: dict[str, str]
    last_character_prompts_by_library: dict[str, list[str]]
    last_character_negative_prompts_by_library: dict[str, list[str]]
    image_model: str
    width: int
    height: int


class ArtistState(TypedDict):
    """Persist shared libraries and per-QQ selections."""

    version: int
    libraries: dict[str, ArtistLibraryState]
    users: dict[str, ArtistUserState]


class CharacterLibraryState(TypedDict):
    """Persist one user's global character library."""

    prompts: dict[str, str]
    negative_prompts: dict[str, str]


class CharacterState(TypedDict):
    """Persist user-scoped character prompts."""

    version: int
    libraries: dict[str, CharacterLibraryState]


class PromptPlan(TypedDict):
    """Hold one validated base prompt and per-character dynamic prompts."""

    prompt: str
    character_prompts: dict[str, str]


class ComicStoryboard(TypedDict):
    """Hold one validated multi-panel visual storyboard."""

    page_layout: str
    reading_order: str
    visual_continuity: str
    panels: list[dict[str, object]]


class PendingCharacterChange(TypedDict):
    """Hold one short-lived character mutation awaiting confirmation."""

    operation: str
    name: str
    content: str
    negative_content: str
    previous_content: str
    previous_negative_content: str
    expires_at: float


class BugReport(TypedDict):
    """Persist one user-submitted NovelAI plugin bug report."""

    id: int
    created_at: str
    sender_id: str
    group_id: str
    content: str


class BugReportState(TypedDict):
    """Persist sequential bug report identifiers and report history."""

    version: int
    next_id: int
    reports: list[BugReport]


class DeliveryTask(TypedDict):
    """Persist one generated image and its chat delivery state."""

    task_id: str
    created_at: str
    sender_id: str
    conversation: str
    group_id: str
    output_path: str
    generated: bool
    delivery_status: str
    retry_count: int
    error: str


class DeliveryState(TypedDict):
    """Persist bounded request-scoped image delivery history."""

    version: int
    tasks: list[DeliveryTask]


class NovelAIWebError(Exception):
    """Represent a safe error message that can be returned to the bot owner."""


@star.register(
    PLUGIN_NAME,
    "YayiMiko",
    "Generate NovelAI V5 images with multimodal prompt planning and identity locks.",
    "0.1.0",
)
class NovelAIWebPlugin(star.Star):
    """Call NovelAI with a persistent API token and strict free-tier guards."""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        """Initialize API state and generation guards.

        Args:
            context: Active AstrBot plugin context.
            config: Persistent plugin configuration.
        """
        super().__init__(context)
        self.config = config
        self._generation_semaphore = asyncio.Semaphore(1)
        self._generation_queue_lock = asyncio.Lock()
        self._generation_queue_size = 0
        self._artist_state_lock = asyncio.Lock()
        self._character_state_lock = asyncio.Lock()
        self._identity_alias_lock = asyncio.Lock()
        self._bug_report_lock = asyncio.Lock()
        self._delivery_state_lock = asyncio.Lock()
        self._pending_character_changes: dict[
            tuple[str, str], PendingCharacterChange
        ] = {}
        self._api_client: httpx.AsyncClient | None = None
        self._migrate_legacy_state()

    @staticmethod
    def _migrate_legacy_state() -> None:
        """Copy compatible user state from the old plugin on first startup."""
        new_dir = star.StarTools.get_data_dir(PLUGIN_NAME)
        old_dir = star.StarTools.get_data_dir("astrbot_plugin_novelai")
        new_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("artist_strings.json", "characters.json", "bug_reports.json"):
            source = old_dir / filename
            target = new_dir / filename
            if source.is_file() and not target.exists():
                try:
                    shutil.copy2(source, target)
                except OSError as exc:
                    logger.warning("[n5] failed to migrate %s: %s", filename, exc)

    @staticmethod
    def _load_api_token() -> str:
        """Load a NovelAI persistent API token without exposing plaintext.

        Returns:
            A token including the required ``pst-`` prefix.

        Raises:
            NovelAIWebError: If no token is configured or DPAPI decryption fails.
        """
        token = os.environ.get(NOVELAI_PAT_ENV, "").strip()
        if not token:
            token_path = star.StarTools.get_data_dir(PLUGIN_NAME) / "novelai_pat.dpapi"
            if os.name != "nt":
                raise NovelAIWebError(
                    f"未配置 {NOVELAI_PAT_ENV}；非 Windows 部署必须通过环境变量提供 PAT。"
                )
            try:
                encrypted = token_path.read_bytes()
            except FileNotFoundError as exc:
                raise NovelAIWebError(
                    f"未找到 NovelAI PAT；请配置 {NOVELAI_PAT_ENV}。"
                ) from exc
            except OSError as exc:
                raise NovelAIWebError("NovelAI PAT 加密文件无法读取。") from exc

            class DataBlob(ctypes.Structure):
                """Represent a Windows DPAPI byte buffer."""

                _fields_ = [
                    ("cbData", ctypes.c_ulong),
                    ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
                ]

            input_buffer = ctypes.create_string_buffer(encrypted)
            input_blob = DataBlob(
                len(encrypted),
                ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
            )
            output_blob = DataBlob()
            try:
                decrypted = ctypes.windll.crypt32.CryptUnprotectData(
                    ctypes.byref(input_blob),
                    None,
                    None,
                    None,
                    None,
                    0x1,
                    ctypes.byref(output_blob),
                )
                if not decrypted:
                    raise ctypes.WinError()
                token = ctypes.string_at(
                    output_blob.pbData,
                    output_blob.cbData,
                ).decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise NovelAIWebError(
                    "NovelAI PAT 无法由当前 Windows 用户解密。"
                ) from exc
            finally:
                if output_blob.pbData:
                    ctypes.windll.kernel32.LocalFree(output_blob.pbData)

        token = token.strip()
        if token and not token.startswith("pst-"):
            token = f"pst-{token}"
        if len(token) < 16 or any(char.isspace() for char in token):
            raise NovelAIWebError("NovelAI PAT 格式无效。")
        return token

    def _get_api_client(self) -> httpx.AsyncClient:
        """Create or reuse the token-authenticated NovelAI HTTP client.

        Returns:
            A reusable asynchronous HTTP client with no browser cookies.
        """
        if self._api_client is None:
            self._api_client = httpx.AsyncClient(
                base_url=NOVELAI_API_BASE_URL,
                headers={
                    "Authorization": f"Bearer {self._load_api_token()}",
                    "User-Agent": "AstrBot-N5/0.1.0",
                },
                follow_redirects=False,
            )
        return self._api_client

    async def _read_subscription(self) -> dict[str, object]:
        """Read the current NovelAI subscription through PAT authentication.

        Returns:
            Subscription metadata including tier, activity, and Anlas balance.

        Raises:
            NovelAIWebError: If authentication, networking, or decoding fails.
        """
        try:
            response = await self._get_api_client().get(
                NOVELAI_SUBSCRIPTION_ENDPOINT,
                timeout=30,
            )
        except httpx.TimeoutException as exc:
            raise NovelAIWebError("读取 NovelAI 订阅状态超时。") from exc
        except httpx.HTTPError as exc:
            raise NovelAIWebError("无法连接 NovelAI API。") from exc
        if response.status_code == 401:
            raise NovelAIWebError("NovelAI PAT 已失效或无权访问账号。")
        if response.status_code != 200:
            raise NovelAIWebError(f"NovelAI 订阅接口返回 HTTP {response.status_code}。")
        try:
            data = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NovelAIWebError("NovelAI 订阅接口返回了无效 JSON。") from exc
        if not isinstance(data, dict):
            raise NovelAIWebError("NovelAI 订阅接口返回格式异常。")
        return data

    @staticmethod
    def _normalize_id_list(value: object) -> set[str]:
        """Normalize a list or comma-separated string of QQ identifiers."""
        if isinstance(value, str):
            return {item.strip() for item in re.split(r"[,\s]+", value) if item.strip()}
        if isinstance(value, list):
            return {str(item).strip() for item in value if str(item).strip()}
        return set()

    def _check_access(
        self,
        event: AstrMessageEvent,
        *,
        allow_group_access: bool = True,
    ) -> None:
        """Apply fail-closed sender authorization in private chats and groups."""
        sender_id = str(event.get_sender_id()).strip()
        allowed_ids = self._normalize_id_list(self.config.get("allowed_sender_ids", []))
        if not allowed_ids or sender_id not in allowed_ids:
            raise NovelAIWebError("当前 QQ 不在 NovelAI 插件的使用者白名单中。")

        if event.is_private_chat():
            return

        if not allow_group_access or not bool(self.config.get("allow_group", False)):
            raise NovelAIWebError("当前 NovelAI 指令不允许在群聊中使用。")

        group_id = str(event.get_group_id()).strip()
        allowed_group_ids = self._normalize_id_list(
            self.config.get("allowed_group_ids", [])
        )
        if allowed_group_ids and group_id not in allowed_group_ids:
            raise NovelAIWebError("当前群不在 NovelAI 群白名单中。")

    @staticmethod
    def _help_text() -> str:
        """Build the command reference shown in the current conversation."""
        return "\n".join(
            [
                "NovelAI N5 指令",
                "/n5 生成 <内容> - 自然语言扩写；附图时使用 DS4F Vision 参考",
                "/n5 漫画 <剧情> - 规划并生成完整的多格漫画页",
                "/n5 漫画抽卡 <角色>[，剧情] - 随机创作或扩写指定剧情",
                "/n5 参考 <修改要求> - 使用本条或引用消息中的图片",
                "/n5 原始 <Prompt> - 跳过自然语言规划，原样生成",
                "/n5 再来 - 复用自己上一次成功生成的最终 Prompt",
                "/n5 重发 - 重发当前会话最近生成的图片，不重新生图",
                "/n5 最近 - 查看当前会话最近一次图片交付状态",
                "/n5 角色 [名称] - 列出或查看自己的角色",
                "/n5 画风 [名称|默认|原生] - 查看或切换画风",
                "/n5 负面 - 查看自己的当前负面提示词",
                "/n5 负面 <内容>|清空 - 设置或清空自己的负面提示词",
                "/n5 模型 [V5C|V5F] - 查看或切换绘图模型",
                "/n5 尺寸 竖图|横图|方图|<宽>x<高> - 设置免费尺寸",
                "/n5 状态 - 检查 API 与当前设置",
                "/n5 诊断 - 显示模型、路由和隔离策略",
                "角色与画风管理仍支持：添加画师串、创建人物、删除人物、确认。",
            ]
        )

    @staticmethod
    def _admin_help_text() -> str:
        """Build the administrator-only command reference."""
        return "\n".join(
            [
                "NovelAI 管理员指令",
                "/n5 状态 - 检查 PAT、Opus、Anlas 与免费生成参数",
            ]
        )

    @staticmethod
    def _artist_state_path() -> Path:
        """Return the persistent artist-string state path."""
        return star.StarTools.get_data_dir(PLUGIN_NAME) / "artist_strings.json"

    @staticmethod
    def _character_state_path() -> Path:
        """Return the persistent user-scoped character state path."""
        return star.StarTools.get_data_dir(PLUGIN_NAME) / "characters.json"

    @staticmethod
    def _bug_report_state_path() -> Path:
        """Return the persistent user bug report state path."""
        return star.StarTools.get_data_dir(PLUGIN_NAME) / "bug_reports.json"

    @staticmethod
    def _delivery_state_path() -> Path:
        """Return the request-scoped image delivery state path."""
        return star.StarTools.get_data_dir(PLUGIN_NAME) / "deliveries.json"

    @staticmethod
    def _identity_alias_state_path() -> Path:
        """Return the verified localized-name cache path."""
        return star.StarTools.get_data_dir(PLUGIN_NAME) / "identity_aliases.json"

    @classmethod
    def _load_verified_identity_aliases(cls) -> dict[str, str]:
        """Load only aliases previously confirmed by NovelAI.

        Returns:
            Mapping from normalized localized identity keys to canonical tags.
        """
        state_path = cls._identity_alias_state_path()
        if not state_path.is_file():
            return {}
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("[n5] verified identity cache could not be read: %s", exc)
            return {}
        raw_aliases = payload.get("aliases", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw_aliases, dict):
            return {}
        return {
            str(key): str(value).strip(" ,")
            for key, value in raw_aliases.items()
            if isinstance(key, str) and isinstance(value, str) and value.strip(" ,")
        }

    @classmethod
    def _save_verified_identity_aliases(cls, aliases: dict[str, str]) -> None:
        """Atomically persist aliases confirmed by NovelAI.

        Args:
            aliases: Verified normalized identity mappings.
        """
        state_path = cls._identity_alias_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = state_path.with_suffix(".json.tmp")
        try:
            temporary_path.write_text(
                json.dumps(
                    {"version": 1, "aliases": aliases},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary_path.replace(state_path)
        except OSError as exc:
            logger.warning("[n5] verified identity cache could not be saved: %s", exc)

    @staticmethod
    def _load_prompt_planner_system_prompt() -> str:
        """Load official rules, local preferences, and runtime instructions.

        Returns:
            Complete versioned system prompt for NovelAI V5 planning.

        Raises:
            NovelAIWebError: If a knowledge file is missing or inconsistent.
        """
        sections: list[str] = []
        try:
            source_manifest = json.loads(
                OFFICIAL_SOURCE_MANIFEST_PATH.read_text(encoding="utf-8")
            )
            official_rules = json.loads(OFFICIAL_RULES_PATH.read_text(encoding="utf-8"))
            local_preferences = json.loads(
                LOCAL_PREFERENCES_PATH.read_text(encoding="utf-8")
            )
            supported_models = official_rules.get("models", [])
            if not isinstance(supported_models, list) or any(
                model not in supported_models for model in NOVELAI_MODELS.values()
            ):
                raise NovelAIWebError("NovelAI 官方规则库与当前生图模型不匹配。")
            raw_sources = source_manifest.get("sources", [])
            source_ids = {
                str(item.get("id") or "")
                for item in raw_sources
                if isinstance(item, dict) and item.get("authority") == "official"
            }
            raw_rules = official_rules.get("rules", [])
            if not isinstance(raw_rules, list) or not raw_rules:
                raise NovelAIWebError("NovelAI 官方规则库为空。")
            official_lines = [
                "NovelAI V5 官方规则库（模型专用事实层；规则 ID 用于追溯，不得输出）："
            ]
            for item in raw_rules:
                if not isinstance(item, dict):
                    raise NovelAIWebError("NovelAI 官方规则库包含无效记录。")
                rule_id = str(item.get("id") or "").strip()
                enforcement = str(item.get("enforcement") or "").strip()
                instruction = str(item.get("instruction") or "").strip()
                rule_sources = item.get("sources", [])
                if (
                    not rule_id
                    or enforcement not in {"deterministic", "llm", "soft"}
                    or not instruction
                    or not isinstance(rule_sources, list)
                    or not rule_sources
                    or any(str(source) not in source_ids for source in rule_sources)
                ):
                    raise NovelAIWebError("NovelAI 官方规则库引用无效。")
                official_lines.append(f"- [{enforcement}][{rule_id}] {instruction}")
            sections.append("\n".join(official_lines))

            raw_preferences = local_preferences.get("preferences", [])
            if not isinstance(raw_preferences, list):
                raise NovelAIWebError("NovelAI 本地偏好层格式无效。")
            preference_lines = [
                "本地偏好层（低于用户明确要求与官方模型规则，不得反向覆盖）："
            ]
            for item in raw_preferences:
                if not isinstance(item, dict):
                    raise NovelAIWebError("NovelAI 本地偏好层包含无效记录。")
                preference_id = str(item.get("id") or "").strip()
                enforcement = str(item.get("enforcement") or "").strip()
                instruction = str(item.get("instruction") or "").strip()
                if (
                    not preference_id
                    or enforcement not in {"deterministic", "llm", "soft"}
                    or not instruction
                ):
                    raise NovelAIWebError("NovelAI 本地偏好层记录无效。")
                preference_lines.append(
                    f"- [{enforcement}][{preference_id}] {instruction}"
                )
            sections.append("\n".join(preference_lines))

            for path in PROMPT_PLANNER_SYSTEM_PROMPT_PATHS:
                section = path.read_text(encoding="utf-8").strip()
                if not section:
                    raise NovelAIWebError(
                        f"NovelAI Prompt 规划 skill 内容为空：{path.name}"
                    )
                sections.append(section)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise NovelAIWebError(
                "NovelAI Prompt 规划 skill 或官方规则库无法读取。"
            ) from exc
        return "\n\n".join(sections)

    @staticmethod
    def _parse_planner_response(
        raw_response: str,
        max_length: int,
        required_character_slots: tuple[str, ...] = (),
    ) -> PromptPlan:
        """Validate one JSON response returned by the prompt planner.

        Args:
            raw_response: Raw model completion expected to contain one JSON object.
            max_length: Maximum combined prompt character count.
            required_character_slots: Protected character keys required in the result.

        Returns:
            Validated base prompt and per-character dynamic prompts.

        Raises:
            NovelAIWebError: If the response violates the planning protocol.
        """
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            raw_response.strip(),
            re.DOTALL | re.IGNORECASE,
        )
        if fenced:
            raw_response = fenced.group(1)
        try:
            payload = json.loads(raw_response)
        except (json.JSONDecodeError, TypeError) as exc:
            raise NovelAIWebError("Prompt 规划模型没有返回有效 JSON。") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise NovelAIWebError("Prompt 规划模型返回了无效协议。")
        if payload["ok"] is False:
            error_code = str(payload.get("error") or "request_rejected").strip()
            if error_code == "conflicting_constraints":
                raise NovelAIWebError("画面描述存在无法消解的互斥约束，请修改后重试。")
            raise NovelAIWebError("Prompt 规划模型拒绝了该描述。")

        planned_prompt = payload.get("prompt")
        if not isinstance(planned_prompt, str):
            raise NovelAIWebError("Prompt 规划模型没有返回 Prompt。")
        planned_prompt = re.sub(r"\s+", " ", planned_prompt).strip(" ,")
        if not planned_prompt:
            raise NovelAIWebError("Prompt 规划模型返回了空 Prompt。")
        forbidden = re.search(
            r"(?i)(?:\bartist\s*:|\bartist collaboration\b|\bchar\s*\d+\s*:|"
            r"CREATIVE_REFERENCE_(?:BEGIN|END))",
            planned_prompt,
        )
        if forbidden:
            raise NovelAIWebError("Prompt 规划结果包含应由插件管理的画师或角色字段。")
        returned_slots = CHARACTER_SLOT_PATTERN.findall(planned_prompt)
        if returned_slots:
            raise NovelAIWebError("人物占位符不能出现在主 Prompt 中。")

        raw_character_prompts = payload.get("character_prompts")
        if not isinstance(raw_character_prompts, dict):
            raise NovelAIWebError("Prompt 规划模型没有返回人物 Prompt 对象。")
        expected_slots = set(required_character_slots)
        if not expected_slots and raw_character_prompts:
            raise NovelAIWebError("Prompt 规划模型改动或遗漏了人物 Prompt 键。")
        character_prompts: dict[str, str] = {}
        for slot in required_character_slots:
            value = raw_character_prompts.get(slot, "")
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise NovelAIWebError("Prompt 规划模型返回了无效人物 Prompt。")
            value = re.sub(r"\s+", " ", value).strip(" ,")
            if CHARACTER_SLOT_PATTERN.search(value):
                raise NovelAIWebError("人物 Prompt 值中不能再次包含人物占位符。")
            if re.search(r"(?i)\bartist\s*:", value):
                raise NovelAIWebError("人物 Prompt 中不能包含画师标签。")
            character_prompts[slot] = value

        combined_length = len(planned_prompt) + sum(
            len(value) for value in character_prompts.values()
        )
        if combined_length > max_length:
            raise NovelAIWebError(
                f"规划后的 Prompt 过长，当前上限为 {max_length} 个字符。"
            )
        return {"prompt": planned_prompt, "character_prompts": character_prompts}

    @staticmethod
    def _parse_comic_storyboard_response(
        raw_response: str,
        required_character_slots: tuple[str, ...] = (),
        *,
        exact_four_panels: bool = False,
        allow_rendered_text: bool = True,
        require_full_cast_each_panel: bool = False,
    ) -> ComicStoryboard:
        """Validate a structured visual storyboard returned by the planner.

        Args:
            raw_response: Raw model completion expected to contain one JSON object.
            required_character_slots: Protected cast slots available to the storyboard.
            exact_four_panels: Whether the storyboard must contain exactly four panels.
            allow_rendered_text: Whether the user permits rendered story text.
            require_full_cast_each_panel: Whether every panel must show the full cast.

        Returns:
            Validated page layout, continuity, and sequential panel descriptions.

        Raises:
            NovelAIWebError: If the response violates the storyboard protocol.
        """
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            raw_response.strip(),
            re.DOTALL | re.IGNORECASE,
        )
        if fenced:
            raw_response = fenced.group(1)
        try:
            payload = json.loads(raw_response)
        except (json.JSONDecodeError, TypeError) as exc:
            raise NovelAIWebError("漫画分镜模型没有返回有效 JSON。") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise NovelAIWebError("漫画分镜模型返回了无效协议。")

        storyboard: ComicStoryboard = {
            "page_layout": "",
            "reading_order": "",
            "visual_continuity": "",
            "panels": [],
        }
        for field in ("page_layout", "reading_order", "visual_continuity"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise NovelAIWebError(f"漫画分镜缺少 {field}。")
            storyboard[field] = re.sub(r"\s+", " ", value).strip()

        raw_panels = payload.get("panels")
        if not isinstance(raw_panels, list) or not 1 <= len(raw_panels) <= 4:
            raise NovelAIWebError("漫画分镜必须包含 1 至 4 格。")
        if exact_four_panels and len(raw_panels) != 4:
            raise NovelAIWebError("漫画抽卡分镜必须完整包含 4 格。")
        expected_panel_sequence = list(range(1, len(raw_panels) + 1))
        layout_panel_sequence = [
            int(value)
            for value in COMIC_PANEL_PATTERN.findall(storyboard["page_layout"])
        ]
        if layout_panel_sequence != expected_panel_sequence:
            raise NovelAIWebError("漫画分镜布局必须逐格说明每格的位置和尺寸。")
        reading_panel_sequence = [
            int(value)
            for value in COMIC_PANEL_PATTERN.findall(storyboard["reading_order"])
        ]
        if reading_panel_sequence != expected_panel_sequence:
            raise NovelAIWebError("漫画分镜阅读顺序必须与连续格号完全一致。")

        required_fields = (
            "beat",
            "shot",
            "camera",
            "composition",
            "action",
            "state_change",
        )
        allowed_slots = set(required_character_slots)
        used_slots: set[str] = set()
        for expected_number, raw_panel in enumerate(raw_panels, start=1):
            if not isinstance(raw_panel, dict):
                raise NovelAIWebError("漫画分镜包含无效分格。")
            if raw_panel.get("panel") != expected_number:
                raise NovelAIWebError("漫画分镜格号必须从 1 开始连续排列。")
            panel: dict[str, object] = {"panel": expected_number}
            for field in required_fields:
                value = raw_panel.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise NovelAIWebError(
                        f"漫画分镜 Panel {expected_number} 缺少 {field}。"
                    )
                panel[field] = re.sub(r"\s+", " ", value).strip()

            characters = raw_panel.get("characters")
            if not isinstance(characters, list) or not all(
                isinstance(value, str) and value.strip() for value in characters
            ):
                raise NovelAIWebError(
                    f"漫画分镜 Panel {expected_number} 的出场人物无效。"
                )
            normalized_characters = [value.strip() for value in characters]
            if allowed_slots and any(
                value not in allowed_slots for value in normalized_characters
            ):
                raise NovelAIWebError("漫画分镜使用了本次请求之外的人物槽位。")
            used_slots.update(
                value for value in normalized_characters if value in allowed_slots
            )
            if (
                require_full_cast_each_panel
                and set(normalized_characters) != allowed_slots
            ):
                raise NovelAIWebError(
                    f"漫画分镜 Panel {expected_number} 没有让全部参赛角色共同入镜。"
                )
            if len(normalized_characters) > 1 and not re.search(
                r"(?i)\b(?:two-shot|group shot|wide shot|ensemble shot)\b",
                str(panel["shot"]),
            ):
                raise NovelAIWebError(
                    f"漫画分镜 Panel {expected_number} 的景别不足以容纳多人。"
                )
            panel["characters"] = normalized_characters

            raw_text_elements = raw_panel.get("text_elements", [])
            if not isinstance(raw_text_elements, list):
                raise NovelAIWebError(
                    f"漫画分镜 Panel {expected_number} 的文字元素无效。"
                )
            text_elements: list[dict[str, str]] = []
            for raw_element in raw_text_elements:
                if not isinstance(raw_element, dict):
                    raise NovelAIWebError(
                        f"漫画分镜 Panel {expected_number} 的文字元素无效。"
                    )
                element: dict[str, str] = {}
                for field in ("kind", "content", "speaker", "placement", "style"):
                    value = raw_element.get(field)
                    if not isinstance(value, str):
                        raise NovelAIWebError(
                            f"漫画分镜 Panel {expected_number} 的文字元素缺少 {field}。"
                        )
                    element[field] = re.sub(r"\s+", " ", value).strip()
                if element["kind"] not in {"dialogue", "title", "narration", "sfx"}:
                    raise NovelAIWebError("漫画分镜使用了未知的文字元素类型。")
                if not all(
                    element[field] for field in ("content", "placement", "style")
                ):
                    raise NovelAIWebError("漫画分镜的文字内容、位置或样式为空。")
                if '"' in element["content"]:
                    raise NovelAIWebError("漫画文字原文不能包含 ASCII 双引号。")
                if len(element["content"]) > 60:
                    raise NovelAIWebError("单个漫画文字元素不得超过 60 个字符。")
                if element["kind"] == "dialogue":
                    if not element["speaker"]:
                        raise NovelAIWebError("漫画对白必须绑定说话者。")
                    if allowed_slots and element["speaker"] not in allowed_slots:
                        raise NovelAIWebError("漫画对白绑定了请求之外的说话者。")
                elif element["speaker"]:
                    raise NovelAIWebError("标题、旁白和拟声词不得绑定说话者。")
                text_elements.append(element)
            if text_elements and not allow_rendered_text:
                raise NovelAIWebError("用户明确要求纯画面，漫画分镜不得添加文字。")
            panel["text_elements"] = text_elements
            storyboard["panels"].append(panel)

        if allowed_slots - used_slots:
            raise NovelAIWebError("漫画分镜遗漏了本次请求中的出场角色。")
        rendered_text_length = sum(
            len(element["content"])
            for panel in storyboard["panels"]
            for element in panel["text_elements"]
        )
        if rendered_text_length > 120:
            raise NovelAIWebError("整页漫画文字总长度不得超过 120 个字符。")
        return storyboard

    async def _plan_comic_storyboard(
        self,
        description: str,
        required_character_slots: tuple[str, ...] = (),
        image_urls: tuple[str, ...] = (),
        metadata_prompt: str = "",
        *,
        comic_draw_mode: bool = False,
        comic_draw_plot_seed: str = "",
        comic_text_allowed: bool = True,
    ) -> str:
        """Design a validated storyboard before writing the NovelAI prompt.

        Args:
            description: User-provided comic description with protected cast slots.
            required_character_slots: Protected character keys available to the story.
            image_urls: Request-local images for native multimodal planning.
            metadata_prompt: Trusted NovelAI metadata recovered from request images.
            comic_draw_mode: Whether to create an exact four-panel draw story.
            comic_draw_plot_seed: Optional user-specified event to expand.
            comic_text_allowed: Whether the user permits rendered story text.

        Returns:
            Compact JSON containing the validated storyboard.

        Raises:
            NovelAIWebError: If storyboard planning fails after retry.
        """
        provider_id = str(
            self.config.get(
                "prompt_planner_provider_id",
                DEFAULT_PROMPT_PLANNER_PROVIDER_ID,
            )
        ).strip()
        if not provider_id:
            raise NovelAIWebError("prompt_planner_provider_id 不能为空。")
        try:
            system_prompt = COMIC_STORYBOARD_SYSTEM_PROMPT_PATH.read_text(
                encoding="utf-8"
            ).strip()
        except OSError as exc:
            raise NovelAIWebError("NovelAI 漫画分镜规则无法读取。") from exc
        if not system_prompt:
            raise NovelAIWebError("NovelAI 漫画分镜规则为空。")

        allowed_slots = ", ".join(required_character_slots) or "NONE"
        plot_seed = comic_draw_plot_seed or "AI_INVENT_STORY"
        retry_prompt = (
            f"[CAST_SLOTS]\n{allowed_slots}\n[/CAST_SLOTS]\n"
            f"[MODE]\n{'COMIC_DRAW_EXACT_4' if comic_draw_mode else 'COMIC_1_TO_4'}"
            "\n[/MODE]\n"
            f"[PLOT_SEED]\n{plot_seed}\n[/PLOT_SEED]\n"
            f"[TEXT_POLICY]\n{'ALLOW_STORY_TEXT' if comic_text_allowed else 'VISUAL_ONLY_NO_TEXT'}"
            "\n[/TEXT_POLICY]\n"
            f"[USER_REQUEST]\n{description}\n[/USER_REQUEST]"
        )
        if metadata_prompt:
            retry_prompt += (
                "\n[IMAGE_METADATA]\n" + metadata_prompt + "\n[/IMAGE_METADATA]"
            )
        last_error: NovelAIWebError | None = None
        for attempt in range(3):
            try:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=retry_prompt,
                    image_urls=list(image_urls),
                    system_prompt=system_prompt,
                    request_max_retries=2,
                    temperature=0.7 if comic_draw_mode else 0,
                )
            except Exception as exc:
                raise NovelAIWebError(
                    "DeepSeek Flash 漫画分镜失败，请稍后再试。"
                ) from exc
            try:
                storyboard = self._parse_comic_storyboard_response(
                    str(response.completion_text or "").strip(),
                    required_character_slots,
                    exact_four_panels=comic_draw_mode,
                    allow_rendered_text=comic_text_allowed,
                    require_full_cast_each_panel=(
                        comic_draw_mode and 1 < len(required_character_slots) <= 4
                    ),
                )
                return json.dumps(
                    storyboard,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            except NovelAIWebError as exc:
                last_error = exc
                if attempt < 2:
                    retry_prompt = (
                        f"上一次分镜无效：{exc} 请严格修正协议并重新设计。\n"
                        + retry_prompt
                    )
        raise last_error or NovelAIWebError("漫画分镜规划失败。")

    @staticmethod
    def _semantic_plan_errors(
        description: str,
        plan: PromptPlan,
        *,
        comic_mode: bool = False,
        comic_draw_mode: bool = False,
    ) -> list[str]:
        """Find omissions that require deterministic post-processing.

        Args:
            description: Original user description before planning.
            plan: Parsed base prompt and dynamic character prompts.
            comic_mode: Whether the request is a multi-panel comic page.
            comic_draw_mode: Whether the planner must invent a four-panel story.

        Returns:
            Human-readable semantic errors; an empty list means validation passed.
        """
        combined_prompt = ", ".join(
            (plan["prompt"], *plan["character_prompts"].values())
        )
        errors: list[str] = []
        if EXPLICIT_NUDITY_SOURCE_PATTERN.search(description) and not re.search(
            r"(?i)(?<![a-z])(?:nude|naked)(?![a-z])",
            combined_prompt,
        ):
            errors.append("缺少 nude")
        if comic_mode:
            if not re.search(r"(?i)\b(?:comic|manga|[1-4]koma)\b", plan["prompt"]):
                errors.append("缺少漫画媒介锚点")
            panel_numbers = {
                int(value) for value in COMIC_PANEL_PATTERN.findall(plan["prompt"])
            }
            if not panel_numbers:
                errors.append("缺少逐格页面描述")
            if comic_draw_mode and panel_numbers != {1, 2, 3, 4}:
                errors.append("漫画抽卡必须完整描述 Panel 1 至 Panel 4")
            if comic_draw_mode and any(
                value and not COMIC_PANEL_PATTERN.search(value)
                for value in plan["character_prompts"].values()
            ):
                errors.append("漫画抽卡的人物 Prompt 缺少分格归属")
        return errors

    async def _plan_prompt(
        self,
        description: str,
        max_length: int,
        required_character_slots: tuple[str, ...] = (),
        image_urls: tuple[str, ...] = (),
        metadata_prompt: str = "",
        *,
        comic_mode: bool = False,
        comic_draw_mode: bool = False,
        comic_draw_plot_seed: str = "",
        comic_storyboard: str = "",
        comic_text_allowed: bool = True,
    ) -> PromptPlan:
        """Convert a user description into a validated NovelAI V5 prompt.

        Args:
            description: User-provided natural-language scene description.
            max_length: Maximum combined prompt character count.
            required_character_slots: Protected character keys found in the input.
            image_urls: Request-local images for native multimodal planning.
            metadata_prompt: Trusted NovelAI PNG metadata recovered from those images.
            comic_mode: Whether to plan a complete multi-panel comic page.
            comic_draw_mode: Whether to invent a four-panel story from cast names.
            comic_draw_plot_seed: Optional user-specified event to expand.
            comic_storyboard: Trusted structured storyboard for prompt translation.
            comic_text_allowed: Whether the user permits rendered story text.

        Returns:
            Validated base prompt and per-character dynamic prompts.

        Raises:
            NovelAIWebError: If planning fails or remains invalid after retry.
        """
        if not bool(self.config.get("prompt_planner_enabled", True)):
            base_prompt = CHARACTER_SLOT_PATTERN.sub("", description)
            base_prompt = re.sub(r"\s*,\s*,+", ", ", base_prompt).strip(" ,")
            return {
                "prompt": base_prompt,
                "character_prompts": dict.fromkeys(required_character_slots, ""),
            }

        provider_id = str(
            self.config.get(
                "prompt_planner_provider_id",
                DEFAULT_PROMPT_PLANNER_PROVIDER_ID,
            )
        ).strip()
        if not provider_id:
            raise NovelAIWebError("prompt_planner_provider_id 不能为空。")
        system_prompt = self._load_prompt_planner_system_prompt()
        if comic_mode:
            try:
                comic_prompt = COMIC_PLANNER_SYSTEM_PROMPT_PATH.read_text(
                    encoding="utf-8"
                ).strip()
            except OSError as exc:
                raise NovelAIWebError("NovelAI 漫画规划规则无法读取。") from exc
            if not comic_prompt:
                raise NovelAIWebError("NovelAI 漫画规划规则为空。")
            system_prompt += "\n\n" + comic_prompt
        if comic_draw_mode:
            try:
                comic_draw_prompt = COMIC_DRAW_PLANNER_SYSTEM_PROMPT_PATH.read_text(
                    encoding="utf-8"
                ).strip()
            except OSError as exc:
                raise NovelAIWebError("NovelAI 漫画抽卡规划规则无法读取。") from exc
            if not comic_draw_prompt:
                raise NovelAIWebError("NovelAI 漫画抽卡规划规则为空。")
            system_prompt += "\n\n" + comic_draw_prompt
        if required_character_slots:
            allowed_slots = ", ".join(f"`{slot}`" for slot in required_character_slots)
            slot_contract = (
                "本次用户消息中由插件实际提供的合法人物槽位恰好为："
                f"{allowed_slots}。`character_prompts` 的键集合必须与此列表完全一致，"
                "不多不少，并原样复制每个键；不得创建列表外的槽位。"
            )
        else:
            slot_contract = (
                "本次用户消息不含任何由插件提供的人物槽位。`character_prompts` 必须严格为 "
                "{}。不得因为出现作品角色名、普通姓名、人物代号或系统示例而自行创建任何 "
                "`__NAI_CHARACTER_SLOT_*__`。角色身份、作品消歧、可辨识外观、本图服装、"
                "道具和动作必须全部写入主 `prompt`。"
            )
        system_prompt += "\n\n本次请求的人物槽位契约：\n" + slot_contract
        if comic_mode and not comic_text_allowed:
            system_prompt += (
                "\n\n本次用户明确要求纯画面。最终主 Prompt 和人物 Prompt 禁止"
                " `Text:`、Caption、Subtitle、引号对白、对白气泡或标题；只用动作、"
                "表情、视线、物件状态和构图讲故事。每格说明应是紧凑的视觉指令，"
                "不得把分镜字段或规划说明当作页面文字。"
            )
        if CHIBI_SOURCE_PATTERN.search(description):
            system_prompt += (
                "\n\n本次输入包含强风格约束 Q版/chibi。必须在主 Prompt 开头保留 "
                "`chibi, super deformed`；身份、动作和必要场景仍需表达，但使用 "
                "最小必要的一组紧凑标签，避免自动补充 realistic proportions、photorealistic、"
                "tall、long legs 或写实电影镜头等会稀释 Q 版比例的内容。"
            )
        retry_prompt = description
        storyboard_contract = ""
        if comic_mode and comic_storyboard:
            storyboard_contract = (
                "\n\n[COMIC_STORYBOARD_BEGIN]\n"
                + comic_storyboard
                + "\n[COMIC_STORYBOARD_END]\n"
                "这是已经通过插件验证的分镜。必须逐格忠实转译为最终 NovelAI Prompt，"
                "保留每格景别、机位、构图、出场人物、动作、状态变化与对白；不得合并、"
                "改序、删格或另写剧情。"
            )
            retry_prompt += storyboard_contract
        comic_draw_plot_contract = ""
        if comic_draw_mode:
            if comic_draw_plot_seed:
                comic_draw_plot_contract = (
                    "\n\n[COMIC_DRAW_PLOT_SEED]\n"
                    f"{comic_draw_plot_seed}\n"
                    "[/COMIC_DRAW_PLOT_SEED]\n"
                    "这是用户指定的核心剧情事件，四格必须围绕它形成因果动作链，"
                    "只能扩写，不能替换、弱化成背景元素或改成静态差分。"
                )
            else:
                comic_draw_plot_contract = (
                    "\n\n[COMIC_DRAW_PLOT_SEED]\nAI_INVENT_STORY\n"
                    "[/COMIC_DRAW_PLOT_SEED]\n"
                    "用户没有指定剧情，由你创作完整的四拍事件。"
                )
            retry_prompt += comic_draw_plot_contract
        if metadata_prompt:
            retry_prompt += (
                "\n\n以下为引用图中的 NovelAI PNG Prompt 元数据，身份事实优先于视觉猜测，"
                "但只保留符合本次修改要求的内容：\n" + metadata_prompt
            )
        last_error: NovelAIWebError | None = None

        for attempt in range(3):
            try:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=retry_prompt,
                    image_urls=list(image_urls),
                    system_prompt=system_prompt,
                    request_max_retries=2,
                    temperature=0.7 if comic_draw_mode else 0,
                )
            except Exception as exc:
                raise NovelAIWebError(
                    "DeepSeek Flash Prompt 规划失败，请稍后再试。"
                ) from exc

            raw_response = str(response.completion_text or "").strip()
            try:
                plan = self._parse_planner_response(
                    raw_response,
                    max_length,
                    required_character_slots,
                )
                if CHIBI_SOURCE_PATTERN.search(description):
                    prompt_items = [
                        item.strip()
                        for item in plan["prompt"].split(",")
                        if item.strip()
                    ]
                    prompt_items = [
                        item
                        for item in prompt_items
                        if item.casefold()
                        not in {
                            "chibi",
                            "super deformed",
                            "realistic proportions",
                            "photorealistic",
                        }
                    ]
                    plan["prompt"] = ", ".join(
                        ("chibi", "super deformed", *prompt_items)
                    )
                    if (
                        len(plan["prompt"])
                        + sum(
                            len(value) for value in plan["character_prompts"].values()
                        )
                        > max_length
                    ):
                        raise NovelAIWebError("Q版风格锁定后的 Prompt 超过长度上限。")
                semantic_errors = self._semantic_plan_errors(
                    description,
                    plan,
                    comic_mode=comic_mode,
                    comic_draw_mode=comic_draw_mode,
                )
                if comic_mode and comic_storyboard:
                    storyboard_payload = json.loads(comic_storyboard)
                    expected_panels = [
                        int(panel["panel"])
                        for panel in storyboard_payload.get("panels", [])
                    ]
                    planned_panels = [
                        int(value)
                        for value in COMIC_PANEL_PATTERN.findall(plan["prompt"])
                    ]
                    if planned_panels != expected_panels:
                        semantic_errors.append("最终 Prompt 未完整保留分镜格数与顺序")
                    expected_rendered_texts = [
                        str(element["content"])
                        for panel in storyboard_payload.get("panels", [])
                        for element in panel.get("text_elements", [])
                    ]
                    quoted_rendered_texts = re.findall(r'"([^"\r\n]+)"', plan["prompt"])
                    if quoted_rendered_texts != expected_rendered_texts:
                        semantic_errors.append("最终 Prompt 未正确引用分镜中的可见文字")
                combined_comic_prompt = ", ".join(
                    (plan["prompt"], *plan["character_prompts"].values())
                )
                if comic_mode and COMIC_TEXT_BLOCK_PATTERN.search(
                    combined_comic_prompt
                ):
                    semantic_errors.append("Text 块必须由插件统一追加到 Prompt 末尾")
                if semantic_errors:
                    raise NovelAIWebError(
                        "Prompt 规划遗漏或曲解核心语义："
                        + "、".join(semantic_errors)
                        + "。"
                    )
                return plan
            except NovelAIWebError as exc:
                last_error = exc
                if attempt < 2:
                    retry_focus = (
                        "逐格保留页面布局、阅读顺序、角色出场、动作、表情和对白；"
                        if comic_mode
                        else "逐项保留人数、主体、身份、主题服装、配饰、手持物、"
                        "动作、关系和环境；"
                    )
                    retry_prompt = (
                        f"上一次输出无效：{exc} 请重新规划以下原始描述，"
                        f"{retry_focus}人物槽位的本图服装与道具必须写入"
                        "对应 character_prompts，"
                        f"只返回协议规定的一行 JSON。{slot_contract}\n"
                        + description
                        + comic_draw_plot_contract
                        + storyboard_contract
                    )

        raise last_error or NovelAIWebError("Prompt 规划失败。")

    def _load_artist_state(self) -> ArtistState:
        """Load shared libraries and per-QQ selections with legacy migration."""
        state_path = self._artist_state_path()
        if not state_path.is_file():
            return {"version": 7, "libraries": {}, "users": {}}
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NovelAIWebError("画师串配置文件无法读取。") from exc
        if not isinstance(raw_state, dict):
            raise NovelAIWebError("画师串配置文件格式无效。")

        libraries: dict[str, ArtistLibraryState] = {}
        raw_libraries = raw_state.get("libraries", {})
        if isinstance(raw_libraries, dict):
            for library_key, raw_library in raw_libraries.items():
                if not isinstance(library_key, str) or not isinstance(
                    raw_library, dict
                ):
                    continue
                raw_presets = raw_library.get("presets", {})
                presets = (
                    {
                        name.strip(): content.strip()
                        for name, content in raw_presets.items()
                        if isinstance(name, str)
                        and isinstance(content, str)
                        and name.strip()
                        and content.strip()
                    }
                    if isinstance(raw_presets, dict)
                    else {}
                )
                libraries[library_key] = {"presets": presets}

        legacy_groups = sorted(
            self._normalize_id_list(self.config.get("allowed_group_ids", []))
        )
        users: dict[str, ArtistUserState] = {}
        raw_users = raw_state.get("users", {})
        if isinstance(raw_users, dict):
            for sender_id, raw_user in raw_users.items():
                if not isinstance(sender_id, str) or not isinstance(raw_user, dict):
                    continue
                raw_presets = raw_user.get("presets", {})
                presets = (
                    {
                        name.strip(): content.strip()
                        for name, content in raw_presets.items()
                        if isinstance(name, str)
                        and isinstance(content, str)
                        and name.strip()
                        and content.strip()
                    }
                    if isinstance(raw_presets, dict)
                    else {}
                )
                legacy_library_key = (
                    f"group:{legacy_groups[0]}"
                    if legacy_groups
                    else f"private:{sender_id}"
                )
                if presets:
                    library = libraries.setdefault(
                        legacy_library_key,
                        {"presets": {}},
                    )
                    library["presets"].update(presets)

                active_by_library: dict[str, str] = {}
                raw_active_by_library = raw_user.get("active_by_library", {})
                if isinstance(raw_active_by_library, dict):
                    for library_key, name in raw_active_by_library.items():
                        library = libraries.get(str(library_key))
                        if isinstance(name, str) and (
                            name == ORIGINAL_ARTIST_STYLE
                            or (library is not None and name in library["presets"])
                        ):
                            active_by_library[str(library_key)] = name
                raw_active = raw_user.get("active", "")
                if (
                    isinstance(raw_active, str)
                    and raw_active in presets
                    and legacy_library_key not in active_by_library
                ):
                    active_by_library[legacy_library_key] = raw_active
                raw_width = raw_user.get("width", DEFAULT_GENERATION_SIZE[0])
                raw_height = raw_user.get("height", DEFAULT_GENERATION_SIZE[1])
                negative_prompt_by_library: dict[str, str] = {}
                raw_negative_prompts = raw_user.get(
                    "negative_prompt_by_library",
                    {},
                )
                if isinstance(raw_negative_prompts, dict):
                    for library_key, negative_prompt in raw_negative_prompts.items():
                        if (
                            isinstance(library_key, str)
                            and isinstance(negative_prompt, str)
                            and len(negative_prompt.strip()) <= 20_000
                        ):
                            negative_prompt_by_library[library_key] = (
                                negative_prompt.strip(" ,")
                            )
                last_prompt_by_library: dict[str, str] = {}
                raw_last_prompts = raw_user.get("last_prompt_by_library", {})
                if isinstance(raw_last_prompts, dict):
                    for library_key, prompt in raw_last_prompts.items():
                        if (
                            isinstance(library_key, str)
                            and isinstance(prompt, str)
                            and prompt.strip()
                            and len(prompt.strip()) <= 20_000
                        ):
                            last_prompt_by_library[library_key] = prompt.strip()
                last_character_prompts_by_library: dict[str, list[str]] = {}
                raw_last_character_prompts = raw_user.get(
                    "last_character_prompts_by_library",
                    {},
                )
                if isinstance(raw_last_character_prompts, dict):
                    for (
                        library_key,
                        character_prompts,
                    ) in raw_last_character_prompts.items():
                        if not isinstance(library_key, str) or not isinstance(
                            character_prompts,
                            list,
                        ):
                            continue
                        normalized_prompts = [
                            prompt.strip()
                            for prompt in character_prompts
                            if isinstance(prompt, str) and prompt.strip()
                        ]
                        if (
                            len(normalized_prompts) == len(character_prompts)
                            and len(normalized_prompts) <= 22
                            and sum(map(len, normalized_prompts)) <= 20_000
                        ):
                            last_character_prompts_by_library[library_key] = (
                                normalized_prompts
                            )
                last_negative_prompt_by_library: dict[str, str] = {}
                raw_last_negative_prompts = raw_user.get(
                    "last_negative_prompt_by_library",
                    {},
                )
                if isinstance(raw_last_negative_prompts, dict):
                    for (
                        library_key,
                        negative_prompt,
                    ) in raw_last_negative_prompts.items():
                        if (
                            isinstance(library_key, str)
                            and isinstance(negative_prompt, str)
                            and len(negative_prompt.strip()) <= 20_000
                        ):
                            last_negative_prompt_by_library[library_key] = (
                                negative_prompt.strip(" ,")
                            )
                last_character_negative_prompts_by_library: dict[str, list[str]] = {}
                raw_last_character_negative_prompts = raw_user.get(
                    "last_character_negative_prompts_by_library",
                    {},
                )
                if isinstance(raw_last_character_negative_prompts, dict):
                    for (
                        library_key,
                        negative_prompts,
                    ) in raw_last_character_negative_prompts.items():
                        if not isinstance(library_key, str) or not isinstance(
                            negative_prompts,
                            list,
                        ):
                            continue
                        normalized_negative_prompts = [
                            prompt.strip(" ,")
                            for prompt in negative_prompts
                            if isinstance(prompt, str)
                        ]
                        if (
                            len(normalized_negative_prompts) == len(negative_prompts)
                            and len(normalized_negative_prompts) <= 22
                            and sum(map(len, normalized_negative_prompts)) <= 20_000
                        ):
                            last_character_negative_prompts_by_library[library_key] = (
                                normalized_negative_prompts
                            )
                try:
                    width, height = self._validate_generation_size(
                        int(raw_width),
                        int(raw_height),
                    )
                except (TypeError, ValueError, NovelAIWebError):
                    width, height = DEFAULT_GENERATION_SIZE
                raw_image_model = raw_user.get("image_model", "")
                try:
                    image_model = (
                        self._normalize_image_model(raw_image_model)
                        if isinstance(raw_image_model, str) and raw_image_model.strip()
                        else ""
                    )
                except NovelAIWebError:
                    image_model = ""
                users[sender_id] = {
                    "active_by_library": active_by_library,
                    "negative_prompt_by_library": negative_prompt_by_library,
                    "last_prompt_by_library": last_prompt_by_library,
                    "last_negative_prompt_by_library": (
                        last_negative_prompt_by_library
                    ),
                    "last_character_prompts_by_library": (
                        last_character_prompts_by_library
                    ),
                    "last_character_negative_prompts_by_library": (
                        last_character_negative_prompts_by_library
                    ),
                    "image_model": image_model,
                    "width": width,
                    "height": height,
                }
        return {"version": 7, "libraries": libraries, "users": users}

    def _save_artist_state(self, state: ArtistState) -> None:
        """Atomically persist per-QQ artist strings and selections."""
        state_path = self._artist_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = state_path.with_suffix(".json.tmp")
        try:
            temporary_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(state_path)
        except OSError as exc:
            raise NovelAIWebError("画师串配置文件无法保存。") from exc

    def _load_character_state(self) -> CharacterState:
        """Load user-scoped character libraries."""
        state_path = self._character_state_path()
        if not state_path.is_file():
            return {"version": 2, "libraries": {}}
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NovelAIWebError("人物配置文件无法读取。") from exc
        if not isinstance(raw_state, dict):
            raise NovelAIWebError("人物配置文件格式无效。")

        try:
            max_length = int(self.config.get("max_character_prompt_length", 2000))
        except (TypeError, ValueError) as exc:
            raise NovelAIWebError("max_character_prompt_length 必须是整数。") from exc
        if not 1 <= max_length <= 10_000:
            raise NovelAIWebError(
                "max_character_prompt_length 配置必须在 1 到 10000 之间。"
            )

        libraries: dict[str, CharacterLibraryState] = {}
        raw_libraries = raw_state.get("libraries", {})
        if isinstance(raw_libraries, dict):
            for library_key, raw_library in raw_libraries.items():
                if not isinstance(library_key, str) or not isinstance(
                    raw_library, dict
                ):
                    continue
                raw_prompts = raw_library.get("prompts", {})
                prompts: dict[str, str] = {}
                if isinstance(raw_prompts, dict):
                    for name, content in raw_prompts.items():
                        if not isinstance(name, str) or not isinstance(content, str):
                            continue
                        try:
                            normalized_name = self._validate_character_name(name)
                            normalized_content = self._normalize_character_prompt(
                                content,
                                max_length,
                            )
                        except NovelAIWebError:
                            continue
                        prompts[normalized_name] = normalized_content
                raw_negative_prompts = raw_library.get("negative_prompts", {})
                negative_prompts: dict[str, str] = {}
                if isinstance(raw_negative_prompts, dict):
                    for name, content in raw_negative_prompts.items():
                        if (
                            not isinstance(name, str)
                            or not isinstance(content, str)
                            or name not in prompts
                        ):
                            continue
                        try:
                            normalized_content = self._normalize_negative_prompt(
                                content,
                                max_length,
                            )
                        except NovelAIWebError:
                            continue
                        if normalized_content:
                            negative_prompts[name] = normalized_content
                libraries[library_key] = {
                    "prompts": prompts,
                    "negative_prompts": negative_prompts,
                }
        return {"version": 2, "libraries": libraries}

    def _save_character_state(self, state: CharacterState) -> None:
        """Atomically persist user-scoped character libraries."""
        state_path = self._character_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = state_path.with_suffix(".json.tmp")
        try:
            temporary_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(state_path)
        except OSError as exc:
            raise NovelAIWebError("人物配置文件无法保存。") from exc

    def _load_bug_report_state(self) -> BugReportState:
        """Load and sanitize persisted bug reports."""
        state_path = self._bug_report_state_path()
        if not state_path.is_file():
            return {"version": 1, "next_id": 1, "reports": []}
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NovelAIWebError("Bug 反馈记录无法读取。") from exc
        if not isinstance(raw_state, dict):
            raise NovelAIWebError("Bug 反馈记录格式无效。")

        reports: list[BugReport] = []
        raw_reports = raw_state.get("reports", [])
        if isinstance(raw_reports, list):
            for raw_report in raw_reports:
                if not isinstance(raw_report, dict):
                    continue
                try:
                    report_id = int(raw_report.get("id", 0))
                except (TypeError, ValueError):
                    continue
                created_at = str(raw_report.get("created_at", "")).strip()
                sender_id = str(raw_report.get("sender_id", "")).strip()
                group_id = str(raw_report.get("group_id", "")).strip()
                content = str(raw_report.get("content", "")).strip()
                if (
                    report_id < 1
                    or not created_at
                    or not sender_id
                    or not content
                    or len(content) > 2000
                ):
                    continue
                reports.append(
                    {
                        "id": report_id,
                        "created_at": created_at,
                        "sender_id": sender_id,
                        "group_id": group_id,
                        "content": content,
                    }
                )
        highest_id = max((report["id"] for report in reports), default=0)
        try:
            configured_next_id = int(raw_state.get("next_id", highest_id + 1))
        except (TypeError, ValueError):
            configured_next_id = highest_id + 1
        return {
            "version": 1,
            "next_id": max(highest_id + 1, configured_next_id, 1),
            "reports": reports,
        }

    def _save_bug_report_state(self, state: BugReportState) -> None:
        """Atomically persist bug reports."""
        state_path = self._bug_report_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = state_path.with_suffix(".json.tmp")
        try:
            temporary_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(state_path)
        except OSError as exc:
            raise NovelAIWebError("Bug 反馈记录无法保存。") from exc

    def _load_delivery_state(self) -> DeliveryState:
        """Load and sanitize bounded image delivery history."""
        state_path = self._delivery_state_path()
        if not state_path.is_file():
            return {"version": 1, "tasks": []}
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("[n5] delivery history could not be read: %s", exc)
            return {"version": 1, "tasks": []}
        raw_tasks = raw_state.get("tasks", []) if isinstance(raw_state, dict) else []
        if not isinstance(raw_tasks, list):
            return {"version": 1, "tasks": []}

        tasks: list[DeliveryTask] = []
        for raw_task in raw_tasks[-200:]:
            if not isinstance(raw_task, dict):
                continue
            task_id = str(raw_task.get("task_id", "")).strip()
            sender_id = str(raw_task.get("sender_id", "")).strip()
            conversation = str(raw_task.get("conversation", "")).strip()
            output_path = str(raw_task.get("output_path", "")).strip()
            if not task_id or not sender_id or not conversation or not output_path:
                continue
            try:
                retry_count = max(0, int(raw_task.get("retry_count", 0)))
            except (TypeError, ValueError):
                retry_count = 0
            tasks.append(
                {
                    "task_id": task_id,
                    "created_at": str(raw_task.get("created_at", "")).strip(),
                    "sender_id": sender_id,
                    "conversation": conversation,
                    "group_id": str(raw_task.get("group_id", "")).strip(),
                    "output_path": output_path,
                    "generated": bool(raw_task.get("generated", True)),
                    "delivery_status": str(
                        raw_task.get("delivery_status", "unknown")
                    ).strip(),
                    "retry_count": retry_count,
                    "error": str(raw_task.get("error", ""))[:500],
                }
            )
        return {"version": 1, "tasks": tasks}

    def _save_delivery_state(self, state: DeliveryState) -> None:
        """Atomically persist bounded image delivery history.

        Args:
            state: Sanitized delivery state.
        """
        state_path = self._delivery_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = state_path.with_suffix(".json.tmp")
        try:
            temporary_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(state_path)
        except OSError as exc:
            logger.warning("[n5] delivery history could not be saved: %s", exc)

    async def _record_delivery_task(
        self,
        event: AstrMessageEvent,
        output_path: Path,
    ) -> str:
        """Create one generated-image delivery task.

        Args:
            event: Request event identifying the sender and conversation.
            output_path: Verified generated image path.

        Returns:
            Unique delivery task identifier.
        """
        task_id = uuid4().hex
        task: DeliveryTask = {
            "task_id": task_id,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "sender_id": self._artist_owner_id(event),
            "conversation": self._artist_library_key(event),
            "group_id": "" if event.is_private_chat() else str(event.get_group_id()),
            "output_path": str(output_path.resolve()),
            "generated": True,
            "delivery_status": "pending",
            "retry_count": 0,
            "error": "",
        }
        async with self._delivery_state_lock:
            state = self._load_delivery_state()
            state["tasks"] = [*state["tasks"], task][-200:]
            self._save_delivery_state(state)
        return task_id

    async def _update_delivery_task(
        self,
        task_id: str,
        status: str,
        *,
        retry_count: int = 0,
        error: str = "",
    ) -> None:
        """Update the originating delivery task without touching another request.

        Args:
            task_id: Unique task identifier.
            status: Current delivery state.
            retry_count: Automatic or manual resend count.
            error: Sanitized platform error text.
        """
        async with self._delivery_state_lock:
            state = self._load_delivery_state()
            for task in reversed(state["tasks"]):
                if task["task_id"] != task_id:
                    continue
                task["delivery_status"] = status
                task["retry_count"] = max(0, retry_count)
                task["error"] = error[:500]
                self._save_delivery_state(state)
                return

    async def _last_delivery_task(
        self,
        event: AstrMessageEvent,
    ) -> DeliveryTask | None:
        """Return this user's latest generated image in this conversation.

        Args:
            event: Request event identifying the sender and conversation.

        Returns:
            A copied delivery task, or ``None`` when no matching task exists.
        """
        sender_id = self._artist_owner_id(event)
        conversation = self._artist_library_key(event)
        async with self._delivery_state_lock:
            state = self._load_delivery_state()
            for task in reversed(state["tasks"]):
                if (
                    task["sender_id"] == sender_id
                    and task["conversation"] == conversation
                ):
                    return task.copy()
        return None

    @staticmethod
    def _validate_character_name(name: str) -> str:
        """Normalize a literal character name used for automatic matching."""
        normalized_name = name.strip()
        if not 2 <= len(normalized_name) <= 40:
            raise NovelAIWebError("角色名长度必须为 2 到 40 个字符。")
        if re.search(r"\s", normalized_name):
            raise NovelAIWebError("角色名不能包含空格。")
        if not re.fullmatch(r"[\w·.-]+", normalized_name, re.UNICODE):
            raise NovelAIWebError(
                "角色名只能包含文字、数字、下划线、点、连字符或间隔点。"
            )
        if "__NAI_CHARACTER_SLOT_" in normalized_name.upper():
            raise NovelAIWebError("角色名包含保留字段。")
        return normalized_name

    @staticmethod
    def _normalize_character_prompt(content: str, max_length: int) -> str:
        """Normalize and validate one immutable character prompt."""
        normalized_content = re.sub(r"\s+", " ", content).strip(" ,")
        if not normalized_content:
            raise NovelAIWebError("人物 Prompt 不能为空。")
        if len(normalized_content) > max_length:
            raise NovelAIWebError(f"人物 Prompt 过长，当前上限为 {max_length} 个字符。")
        if CHARACTER_SLOT_PATTERN.search(normalized_content) or re.search(
            r"(?i)(?:\bartist\s*:|\bartist collaboration\b|\bchar\s*\d+\s*:)",
            normalized_content,
        ):
            raise NovelAIWebError(
                "人物 Prompt 不能包含画师字段、多角色编辑器字段或保留占位符。"
            )
        return normalized_content

    @staticmethod
    def _normalize_negative_prompt(content: str, max_length: int = 20_000) -> str:
        """Normalize one optional base or character negative prompt.

        Args:
            content: User-provided negative prompt.
            max_length: Maximum normalized character count.

        Returns:
            Normalized prompt, or an empty string when explicitly cleared.

        Raises:
            NovelAIWebError: If the prompt exceeds the limit or uses reserved fields.
        """
        normalized_content = re.sub(r"\s+", " ", content).strip(" ,")
        if len(normalized_content) > max_length:
            raise NovelAIWebError(f"负面提示词过长，当前上限为 {max_length} 个字符。")
        if CHARACTER_SLOT_PATTERN.search(normalized_content) or re.search(
            r"(?i)\bchar\s*\d+\s*:",
            normalized_content,
        ):
            raise NovelAIWebError("负面提示词不能包含人物系统保留字段。")
        return normalized_content

    @staticmethod
    def _apply_global_nsfw_prompt(content: str) -> str:
        """Apply the global NSFW direction without content-rating tags.

        Args:
            content: Positive prompt before the NovelAI request.

        Returns:
            Prompt with one global ``nsfw`` token and no ``rating:`` items.
        """
        text_block = ""
        text_block_match = re.search(r"(?i)\ntext\s*:", content)
        if text_block_match:
            text_block = content[text_block_match.start() :]
            content = content[: text_block_match.start()]
        prompt_items = [item.strip() for item in content.split(",") if item.strip()]
        prompt_items = [
            item
            for item in prompt_items
            if item.casefold() != "nsfw"
            and not re.search(r"(?i)(?<![a-z0-9_])rating\s*:", item)
        ]
        insert_at = 0
        while insert_at < len(prompt_items) and re.match(
            r"(?i)^artist\s*:", prompt_items[insert_at]
        ):
            insert_at += 1
        prompt_items.insert(insert_at, "nsfw")
        return ", ".join(prompt_items) + text_block

    @staticmethod
    def _character_name_pattern(name: str) -> re.Pattern[str]:
        """Build a literal matcher without matching inside ASCII identifiers."""
        escaped_name = re.escape(name)
        if re.fullmatch(r"[A-Za-z0-9_-]+", name):
            escaped_name = rf"(?<![A-Za-z0-9_-]){escaped_name}(?![A-Za-z0-9_-])"
        return re.compile(escaped_name, re.IGNORECASE)

    async def _add_character(
        self,
        event: AstrMessageEvent,
        name: str,
        content: str,
        negative_content: str = "",
    ) -> bool:
        """Add a character or stage an existing character for confirmation."""
        normalized_name = self._validate_character_name(name)
        try:
            max_length = int(self.config.get("max_character_prompt_length", 2000))
        except (TypeError, ValueError) as exc:
            raise NovelAIWebError("max_character_prompt_length 必须是整数。") from exc
        if not 1 <= max_length <= 10_000:
            raise NovelAIWebError(
                "max_character_prompt_length 配置必须在 1 到 10000 之间。"
            )
        normalized_content = self._normalize_character_prompt(
            content,
            max_length,
        )
        normalized_negative_content = self._normalize_negative_prompt(
            negative_content,
            max_length,
        )

        library_key = self._character_library_key(event)
        sender_id = self._artist_owner_id(event)
        pending_key = (library_key, sender_id)
        async with self._character_state_lock:
            state = self._load_character_state()
            library = state["libraries"].setdefault(
                library_key,
                {"prompts": {}, "negative_prompts": {}},
            )
            existing_name = next(
                (
                    item
                    for item in library["prompts"]
                    if item.casefold() == normalized_name.casefold()
                ),
                None,
            )
            if existing_name is not None:
                self._pending_character_changes[pending_key] = {
                    "operation": "overwrite",
                    "name": normalized_name,
                    "content": normalized_content,
                    "negative_content": normalized_negative_content,
                    "previous_content": library["prompts"][existing_name],
                    "previous_negative_content": library["negative_prompts"].get(
                        existing_name,
                        "",
                    ),
                    "expires_at": monotonic() + 60.0,
                }
                return True

            self._pending_character_changes.pop(pending_key, None)
            library["prompts"][normalized_name] = normalized_content
            if normalized_negative_content:
                library["negative_prompts"][normalized_name] = (
                    normalized_negative_content
                )
            self._save_character_state(state)
            return False

    async def _stage_character_deletion(
        self,
        event: AstrMessageEvent,
        name: str,
    ) -> str:
        """Stage one existing user-scoped character for deletion.

        Args:
            event: Message event identifying the requesting QQ user.
            name: Existing character name to delete.

        Returns:
            Canonical stored character name awaiting confirmation.

        Raises:
            NovelAIWebError: If the character does not exist.
        """
        normalized_name = self._validate_character_name(name)
        library_key = self._character_library_key(event)
        sender_id = self._artist_owner_id(event)
        pending_key = (library_key, sender_id)
        async with self._character_state_lock:
            state = self._load_character_state()
            library = state["libraries"].get(library_key)
            prompts = library["prompts"] if library is not None else {}
            existing_name = next(
                (
                    item
                    for item in prompts
                    if item.casefold() == normalized_name.casefold()
                ),
                None,
            )
            if existing_name is None:
                self._pending_character_changes.pop(pending_key, None)
                raise NovelAIWebError(f"你的全局人物中不存在「{normalized_name}」。")
            self._pending_character_changes[pending_key] = {
                "operation": "delete",
                "name": existing_name,
                "content": "",
                "negative_content": "",
                "previous_content": prompts[existing_name],
                "previous_negative_content": library["negative_prompts"].get(
                    existing_name,
                    "",
                ),
                "expires_at": monotonic() + 60.0,
            }
            return existing_name

    async def _confirm_character_change(
        self,
        event: AstrMessageEvent,
    ) -> tuple[str, str]:
        """Commit this QQ's unexpired pending character mutation.

        Args:
            event: Message event identifying the confirming QQ user.

        Returns:
            Operation name and canonical character name.

        Raises:
            NovelAIWebError: If no matching request exists, expired, or became stale.
        """
        library_key = self._character_library_key(event)
        sender_id = self._artist_owner_id(event)
        pending_key = (library_key, sender_id)
        async with self._character_state_lock:
            pending = self._pending_character_changes.pop(pending_key, None)
            if pending is None:
                raise NovelAIWebError("当前没有待确认的人物覆盖或删除请求。")
            if pending["expires_at"] <= monotonic():
                raise NovelAIWebError("人物操作确认已超时，请重新提交请求。")

            state = self._load_character_state()
            library = state["libraries"].get(library_key)
            prompts = library["prompts"] if library is not None else {}
            existing_name = next(
                (
                    item
                    for item in prompts
                    if item.casefold() == pending["name"].casefold()
                ),
                None,
            )
            if (
                existing_name is None
                or prompts[existing_name] != pending["previous_content"]
                or library["negative_prompts"].get(existing_name, "")
                != pending["previous_negative_content"]
            ):
                raise NovelAIWebError("该人物已被其他成员修改，请重新提交请求。")
            if pending["operation"] == "delete":
                del prompts[existing_name]
                library["negative_prompts"].pop(existing_name, None)
                self._save_character_state(state)
                return "delete", existing_name
            if existing_name != pending["name"]:
                del prompts[existing_name]
                library["negative_prompts"].pop(existing_name, None)
            prompts[pending["name"]] = pending["content"]
            if pending["negative_content"]:
                library["negative_prompts"][pending["name"]] = pending[
                    "negative_content"
                ]
            else:
                library["negative_prompts"].pop(pending["name"], None)
            self._save_character_state(state)
            return "overwrite", pending["name"]

    async def _character_text(
        self,
        event: AstrMessageEvent,
        name: str,
    ) -> str:
        """List character names or show one exact character prompt."""
        library_key = self._character_library_key(event)
        normalized_name = name.strip()
        async with self._character_state_lock:
            state = self._load_character_state()
            library = state["libraries"].get(library_key)
            prompts = library["prompts"] if library is not None else {}
            if not normalized_name:
                names = sorted(prompts)
                if not names:
                    return "你还没有保存全局人物。"
                lines = [f"你的全局人物（共 {len(names)} 个）"]
                lines.extend(f"- {item}" for item in names[:50])
                if len(names) > 50:
                    lines.append(f"另有 {len(names) - 50} 个未显示。")
                return "\n".join(lines)

            normalized_name = self._validate_character_name(normalized_name)
            canonical_name = next(
                (
                    item
                    for item in prompts
                    if item.casefold() == normalized_name.casefold()
                ),
                normalized_name,
            )
            content = prompts.get(canonical_name)
            if content is None:
                raise NovelAIWebError(f"你的全局人物中不存在「{normalized_name}」。")
            negative_content = library["negative_prompts"].get(canonical_name, "")
            return (
                f"人物「{canonical_name}」\n"
                f"Prompt：{content}\n"
                f"负面：{negative_content or '未设置'}"
            )

    async def _resolve_character_slots(
        self,
        event: AstrMessageEvent,
        description: str,
    ) -> tuple[str, list[tuple[str, str, str, str]]]:
        """Replace matched names with protected slots before LLM planning."""
        if CHARACTER_SLOT_PATTERN.search(description):
            raise NovelAIWebError("画面描述包含人物系统保留占位符。")
        try:
            max_slots = int(self.config.get("max_characters_per_prompt", 4))
        except (TypeError, ValueError) as exc:
            raise NovelAIWebError("max_characters_per_prompt 必须是整数。") from exc
        if not 1 <= max_slots <= 22:
            raise NovelAIWebError("max_characters_per_prompt 配置必须在 1 到 22 之间。")

        library_key = self._character_library_key(event)
        async with self._character_state_lock:
            state = self._load_character_state()
            library = state["libraries"].get(library_key)
            prompts = dict(library["prompts"]) if library is not None else {}

        replacements: list[tuple[str, str, str, str]] = []
        occupied_spans: list[tuple[int, int]] = []
        matched_characters: list[tuple[str, str, list[tuple[int, int]]]] = []
        for name, content in sorted(
            prompts.items(),
            key=lambda item: (-len(item[0]), item[0].casefold()),
        ):
            pattern = self._character_name_pattern(name)
            available_spans = [
                match.span()
                for match in pattern.finditer(description)
                if not any(
                    match.start() < occupied_end and match.end() > occupied_start
                    for occupied_start, occupied_end in occupied_spans
                )
            ]
            if not available_spans:
                continue
            if len(matched_characters) >= max_slots:
                raise NovelAIWebError(
                    f"一次最多自动引用 {max_slots} 个人物，请减少角色数量。"
                )
            occupied_spans.extend(available_spans)
            matched_characters.append((name, content, available_spans))

        matched_characters.sort(key=lambda item: min(item[2]))
        edits: list[tuple[int, int, str]] = []
        for name, content, spans in matched_characters:
            slot = f"__NAI_CHARACTER_SLOT_{len(replacements) + 1}__"
            for occurrence_index, (start, end) in enumerate(sorted(spans)):
                replacement = slot if occurrence_index == 0 else "the same character"
                edits.append((start, end, replacement))
            replacements.append(
                (
                    slot,
                    name,
                    content,
                    library["negative_prompts"].get(name, ""),
                )
            )

        slotted_description = description
        for start, end, replacement in sorted(edits, reverse=True):
            slotted_description = (
                slotted_description[:start] + replacement + slotted_description[end:]
            )
        return slotted_description, replacements

    @staticmethod
    async def _request_image_context(event: AstrMessageEvent) -> RequestImageContext:
        """Resolve images attached to only the current command.

        Args:
            event: Current AstrBot event.

        Returns:
            Request-local image context, or an empty context for unsupported events.
        """
        try:
            return await resolve_request_images(event)
        except (AttributeError, OSError, ValueError) as exc:
            logger.warning("[n5] image context resolution failed: %s", exc)
            return RequestImageContext((), "", "")

    async def _resolve_planned_character_slots(
        self,
        event: AstrMessageEvent,
        description: str,
        replacements: list[tuple[str, str, str, str]],
        image_context: RequestImageContext,
        image_model: str = NOVELAI_MODEL,
    ) -> tuple[str, list[tuple[str, str, str, str]], list[str], str]:
        """Add protected characters and researched creative references.

        Args:
            event: Current event used for native web-search permissions.
            description: Description after saved-character replacement.
            replacements: Existing protected character entries.
            image_context: Request-scoped multimodal context.
            image_model: NovelAI V5 model used for vocabulary verification.

        Returns:
            Slotted description, character entries, warnings, and reference context.

        Raises:
            NovelAIWebError: If the multimodal identity planner fails.
        """
        provider_id = str(
            self.config.get(
                "prompt_planner_provider_id",
                DEFAULT_PROMPT_PLANNER_PROVIDER_ID,
            )
        ).strip()
        if not provider_id:
            raise NovelAIWebError("prompt_planner_provider_id 不能为空。")
        async with self._identity_alias_lock:
            verified_aliases = self._load_verified_identity_aliases()
        try:
            identities, creative_references = await plan_identities(
                self.context,
                provider_id,
                description,
                image_context.image_urls,
                image_context.metadata_prompt,
                NovelAITagResolver(
                    self._get_api_client(),
                    NOVELAI_API_BASE_URL,
                    image_model,
                ),
                event=event,
                verified_aliases=verified_aliases,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise NovelAIWebError("角色识别模型没有返回有效协议。") from exc
        except Exception as exc:
            raise NovelAIWebError("DS4F Vision 角色识别失败，请稍后再试。") from exc

        learned_aliases = {
            identity_alias_key(
                identity.source_name, identity.work
            ): identity.canonical_tag
            for identity in identities
            if identity.verified and identity.canonical_tag
        }
        if any(
            verified_aliases.get(key) != value for key, value in learned_aliases.items()
        ):
            async with self._identity_alias_lock:
                latest_aliases = self._load_verified_identity_aliases()
                latest_aliases.update(learned_aliases)
                self._save_verified_identity_aliases(latest_aliases)

        try:
            max_slots = int(self.config.get("max_characters_per_prompt", 4))
        except (TypeError, ValueError) as exc:
            raise NovelAIWebError("max_characters_per_prompt 必须是整数。") from exc
        warnings: list[str] = []
        seen_names: set[str] = set()
        for identity in identities:
            normalized_name = identity.source_name.casefold()
            if normalized_name in seen_names:
                continue
            seen_names.add(normalized_name)
            if identity.role not in {"visible_subject", "reference_subject"}:
                source_label = identity.canonical_tag or identity.source_name
                role_label = identity.role.replace("_", " ")
                description = re.sub(
                    re.escape(identity.source_name),
                    f"{source_label} [{role_label} only; not an additional visible character]",
                    description,
                    flags=re.IGNORECASE,
                )
                if not identity.verified:
                    warnings.append(identity.source_name)
                continue
            if len(replacements) >= max(0, min(max_slots, 22)):
                continue
            slot = f"__NAI_CHARACTER_SLOT_{len(replacements) + 1}__"
            name_pattern = re.compile(re.escape(identity.source_name), re.IGNORECASE)
            matches = list(name_pattern.finditer(description))
            if matches:
                edits: list[tuple[int, int, str]] = []
                for index, match in enumerate(matches):
                    edits.append(
                        (
                            match.start(),
                            match.end(),
                            slot if index == 0 else "the same character",
                        )
                    )
                for start, end, replacement in reversed(edits):
                    description = description[:start] + replacement + description[end:]
            elif image_context.image_urls:
                description = f"{description}, depicted reference subject {slot}"
            else:
                continue
            replacements.append(
                (slot, identity.source_name, identity.immutable_prompt, "")
            )
            if not identity.verified:
                warnings.append(identity.source_name)
        reference_sections: list[str] = []
        for reference in creative_references:
            lines = [
                f"Type: {reference.reference_type}",
                f"Source term: {reference.source_name}",
                f"Canonical name: {reference.canonical_name}",
            ]
            source_work = reference.work_en or reference.work
            if source_work:
                lines.append(f"Source work: {source_work}")
            if reference.anchor_tags:
                lines.append(
                    "Optional visual anchors: " + ", ".join(reference.anchor_tags)
                )
            if reference.visual_blueprint:
                lines.append("Visual blueprint: " + reference.visual_blueprint)
            if reference.exclude_subjects:
                lines.append(
                    "Do not add source performers unless explicitly requested: "
                    + ", ".join(reference.exclude_subjects)
                )
            lines.append(
                "Adapt this visual language to the actual requested subject; this reference is not another character."
            )
            reference_sections.append("\n".join(lines))
        reference_context = ""
        if reference_sections:
            reference_context = (
                CREATIVE_REFERENCE_BEGIN
                + "\n"
                + "\n\n".join(reference_sections)
                + "\n"
                + CREATIVE_REFERENCE_END
            )
        return description, replacements, warnings, reference_context

    @staticmethod
    def _restore_character_slots(
        planned_prompt: str,
        replacements: list[tuple[str, str, str, str]],
    ) -> str:
        """Restore every validated character prompt exactly once."""
        restored_prompt = planned_prompt
        for slot, _, character_prompt, _ in replacements:
            if restored_prompt.count(slot) != 1:
                raise NovelAIWebError("人物占位符数量异常，已停止生成。")
            restored_prompt = restored_prompt.replace(slot, character_prompt, 1)
        if CHARACTER_SLOT_PATTERN.search(restored_prompt):
            raise NovelAIWebError("Prompt 中仍存在未知人物占位符，已停止生成。")
        return restored_prompt

    @staticmethod
    def _build_character_prompts(
        replacements: list[tuple[str, str, str, str]],
        dynamic_prompts: dict[str, str],
        max_length: int,
        explicit_nudity: bool = False,
    ) -> tuple[str, ...]:
        """Build native V5 captions from immutable identity and per-image design.

        Args:
            replacements: Slot, character name, and saved immutable prompt tuples.
            dynamic_prompts: Per-slot clothing, props, actions, and expressions.
            max_length: Maximum combined character prompt length.
            explicit_nudity: Whether explicit user intent must override garments.

        Returns:
            Native character captions in original mention order.

        Raises:
            NovelAIWebError: If slots differ or combined prompts exceed the limit.
        """
        expected_slots = {slot for slot, _, _, _ in replacements}
        if set(dynamic_prompts) != expected_slots:
            raise NovelAIWebError("人物动态 Prompt 与命中的人物不一致。")

        character_prompts: list[str] = []
        for slot, _, saved_prompt, _ in replacements:
            if re.search(
                r"(?i)(?<![a-z0-9_])(?:girl|1girl|woman|female|loli)(?![a-z0-9_])",
                saved_prompt,
            ):
                subject = "girl"
            elif re.search(
                r"(?i)(?<![a-z0-9_])(?:boy|1boy|man|male|shota)(?![a-z0-9_])",
                saved_prompt,
            ):
                subject = "boy"
            else:
                subject = "other"

            saved_items: list[str] = []
            for item in saved_prompt.split(","):
                item = item.strip()
                if not item or item.casefold() == "solo":
                    continue
                item = re.sub(
                    r"(?i)(?<![a-z0-9_])1\s*(girl|boy|other)(?![a-z0-9_])",
                    r"\1",
                    item,
                )
                saved_items.append(item)
            if explicit_nudity:
                saved_items = [
                    item
                    for item in saved_items
                    if not CLOTHING_TAG_PATTERN.search(item)
                ]
            if not any(
                CHARACTER_SUBJECT_PATTERN.fullmatch(item) for item in saved_items
            ):
                saved_items.insert(0, subject)

            dynamic_items = [
                item.strip()
                for item in dynamic_prompts[slot].split(",")
                if item.strip()
            ]
            if explicit_nudity:
                dynamic_items = [
                    item
                    for item in dynamic_items
                    if not CLOTHING_TAG_PATTERN.search(item)
                ]
                if not any(
                    re.search(r"(?i)(?<![a-z])(?:nude|naked)(?![a-z])", item)
                    for item in dynamic_items
                ):
                    dynamic_items.insert(0, "nude")
            dynamic_items = [
                item
                for item in dynamic_items
                if not CHARACTER_SUBJECT_PATTERN.fullmatch(item)
            ]
            character_prompt = ", ".join((*saved_items, *dynamic_items))
            character_prompts.append(character_prompt)

        if sum(map(len, character_prompts)) > max_length:
            raise NovelAIWebError("人物 Prompt 拼接后超过长度上限。")
        return tuple(character_prompts)

    @staticmethod
    def _apply_character_subject_counts(
        base_prompt: str,
        character_prompts: tuple[str, ...],
    ) -> str:
        """Derive base subject counts from protected saved character prompts.

        Args:
            base_prompt: Planned shared scene prompt.
            character_prompts: Final native captions beginning with a subject type.

        Returns:
            Base prompt with deterministic V5 subject count tags.
        """
        if not character_prompts:
            return base_prompt

        counts = {"girl": 0, "boy": 0, "other": 0}
        for character_prompt in character_prompts:
            subject_match = CHARACTER_SUBJECT_PATTERN.search(character_prompt)
            subject = subject_match.group(1).casefold() if subject_match else "other"
            counts[subject] += 1

        count_pattern = re.compile(
            r"(?i)^(?:\d+\s*(?:girls?|boys?|others?|people|persons?|characters)|"
            r"(?:one|two|three|four|five|six)\s+"
            r"(?:girls?|boys?|others?|people|persons|characters)|"
            r"multiple\s+(?:girls?|boys?|others?|people|persons|characters))$"
        )
        base_items = [
            item.strip()
            for item in base_prompt.split(",")
            if item.strip()
            and not count_pattern.fullmatch(item.strip())
            and item.strip().casefold() != "solo"
        ]
        count_items = [
            f"{count}{subject if count == 1 else subject + 's'}"
            for subject in ("girl", "boy", "other")
            if (count := counts[subject])
        ]
        if sum(counts.values()) == 1:
            count_items.append("solo")
        return ", ".join((*count_items, *base_items))

    @staticmethod
    def _artist_owner_id(event: AstrMessageEvent) -> str:
        """Return the stable sender ID used to isolate artist-string state."""
        sender_id = str(event.get_sender_id()).strip()
        if not sender_id:
            raise NovelAIWebError("无法识别当前用户 ID。")
        return sender_id

    def _artist_library_key(self, event: AstrMessageEvent) -> str:
        """Return the group-shared or owner-private library key."""
        if event.is_private_chat():
            return f"private:{self._artist_owner_id(event)}"
        group_id = str(event.get_group_id()).strip()
        if not group_id:
            raise NovelAIWebError("无法识别当前群号。")
        return f"group:{group_id}"

    def _character_library_key(self, event: AstrMessageEvent) -> str:
        """Return one user-scoped character library shared across all chats."""
        return f"private:{self._artist_owner_id(event)}"

    @staticmethod
    def _new_user_state() -> ArtistUserState:
        """Return default per-QQ artist and generation preferences."""
        return {
            "active_by_library": {},
            "negative_prompt_by_library": {},
            "last_prompt_by_library": {},
            "last_negative_prompt_by_library": {},
            "last_character_prompts_by_library": {},
            "last_character_negative_prompts_by_library": {},
            "image_model": "",
            "width": DEFAULT_GENERATION_SIZE[0],
            "height": DEFAULT_GENERATION_SIZE[1],
        }

    @staticmethod
    def _normalize_image_model(value: str) -> str:
        """Normalize one supported V5 model name.

        Args:
            value: User-facing alias or NovelAI API model identifier.

        Returns:
            Canonical NovelAI API model identifier.

        Raises:
            NovelAIWebError: If the value does not select V5 Curated or V5 Full.
        """
        normalized = re.sub(r"[\s_-]+", "", str(value).casefold())
        aliases = {
            "v5c": NOVELAI_MODELS["v5c"],
            "5c": NOVELAI_MODELS["v5c"],
            "curated": NOVELAI_MODELS["v5c"],
            "v5curated": NOVELAI_MODELS["v5c"],
            "naidiffusion5curated": NOVELAI_MODELS["v5c"],
            "v5f": NOVELAI_MODELS["v5f"],
            "5f": NOVELAI_MODELS["v5f"],
            "full": NOVELAI_MODELS["v5f"],
            "v5full": NOVELAI_MODELS["v5f"],
            "naidiffusion5full": NOVELAI_MODELS["v5f"],
        }
        image_model = aliases.get(normalized)
        if image_model is None:
            raise NovelAIWebError("用法：/n5 模型 V5C|V5F")
        return image_model

    async def _user_image_model(
        self,
        event: AstrMessageEvent,
        selection: str | None = None,
    ) -> str:
        """Read or persist this QQ user's NovelAI V5 model selection.

        Args:
            event: Message event identifying the QQ user.
            selection: Optional V5C or V5F selection to persist.

        Returns:
            Canonical NovelAI API model identifier.
        """
        default_model = self._normalize_image_model(
            str(self.config.get("image_model", NOVELAI_MODEL))
        )
        sender_id = self._artist_owner_id(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            user_state = state["users"].get(sender_id)
            if selection is None:
                if user_state is None or not user_state["image_model"]:
                    return default_model
                return user_state["image_model"]
            image_model = self._normalize_image_model(selection)
            if user_state is None:
                user_state = self._new_user_state()
                state["users"][sender_id] = user_state
            user_state["image_model"] = image_model
            self._save_artist_state(state)
            return image_model

    async def _user_negative_prompt(
        self,
        event: AstrMessageEvent,
        content: str | None = None,
    ) -> str:
        """Read or update this QQ user's base negative prompt for a conversation.

        Args:
            event: Message event identifying the QQ user and conversation.
            content: New prompt, an empty string to clear, or ``None`` to read.

        Returns:
            The effective normalized negative prompt.
        """
        sender_id = self._artist_owner_id(event)
        library_key = self._artist_library_key(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            user_state = state["users"].get(sender_id)
            if content is None:
                if user_state is None:
                    return DEFAULT_NEGATIVE_PROMPT
                return user_state["negative_prompt_by_library"].get(
                    library_key,
                    DEFAULT_NEGATIVE_PROMPT,
                )
            normalized_content = self._normalize_negative_prompt(content)
            if user_state is None:
                user_state = self._new_user_state()
                state["users"][sender_id] = user_state
            user_state["negative_prompt_by_library"][library_key] = normalized_content
            self._save_artist_state(state)
            return normalized_content

    async def _remember_last_prompt(
        self,
        event: AstrMessageEvent,
        prompt: str,
        character_prompts: tuple[str, ...] = (),
        negative_prompt: str = "",
        character_negative_prompts: tuple[str, ...] = (),
    ) -> None:
        """Persist one successful generation for this QQ and conversation.

        Args:
            event: Message event identifying the QQ user and conversation.
            prompt: Final base prompt sent to NovelAI.
            character_prompts: Final native V5 character captions.
            negative_prompt: Final base negative prompt.
            character_negative_prompts: Final native V5 character negatives.
        """
        sender_id = self._artist_owner_id(event)
        library_key = self._artist_library_key(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            user_state = state["users"].setdefault(
                sender_id,
                self._new_user_state(),
            )
            user_state["last_prompt_by_library"][library_key] = prompt
            user_state["last_negative_prompt_by_library"][library_key] = negative_prompt
            user_state["last_character_prompts_by_library"][library_key] = list(
                character_prompts
            )
            user_state["last_character_negative_prompts_by_library"][library_key] = (
                list(character_negative_prompts)
            )
            self._save_artist_state(state)

    async def _last_successful_prompt(
        self,
        event: AstrMessageEvent,
    ) -> tuple[str, tuple[str, ...], str, tuple[str, ...]] | None:
        """Return this QQ's last successful generation in this conversation.

        Args:
            event: Message event identifying the QQ user and conversation.

        Returns:
            Base prompt, native character captions, base negative prompt, and
            character negatives, or ``None`` when absent.
        """
        sender_id = self._artist_owner_id(event)
        library_key = self._artist_library_key(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            user_state = state["users"].get(sender_id)
            if user_state is None:
                return None
            prompt = user_state["last_prompt_by_library"].get(library_key, "")
            if not prompt:
                return None
            character_prompts = user_state["last_character_prompts_by_library"].get(
                library_key, []
            )
            negative_prompt = user_state["last_negative_prompt_by_library"].get(
                library_key,
                "",
            )
            character_negative_prompts = user_state[
                "last_character_negative_prompts_by_library"
            ].get(library_key, [])
            return (
                prompt,
                tuple(character_prompts),
                negative_prompt,
                tuple(character_negative_prompts),
            )

    @staticmethod
    def _is_delivery_ack_timeout(exc: Exception) -> bool:
        """Return whether a platform error is an ambiguous send ACK timeout.

        Args:
            exc: Exception raised by the platform send API.

        Returns:
            True for NapCat/OneBot send acknowledgement timeouts.
        """
        retcode = getattr(exc, "retcode", None)
        wording = str(getattr(exc, "wording", "") or getattr(exc, "message", "") or exc)
        return retcode == 1200 or (
            "Timeout" in wording and "NodeIKernelMsgService/sendMsg" in wording
        )

    async def _delivery_history_contains_image(
        self,
        event: AstrMessageEvent,
        output_path: Path,
        started_at: int,
    ) -> bool:
        """Check recent NapCat history for the image after an ACK timeout.

        Args:
            event: Original message event carrying the OneBot client.
            output_path: Image whose byte size identifies the attempted upload.
            started_at: Unix timestamp immediately before the send attempt.

        Returns:
            True when a recent self-sent image with the same byte size exists.
        """
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return False
        raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
        self_id = ""
        if isinstance(raw_event, dict):
            self_id = str(raw_event.get("self_id", "")).strip()
        if not self_id:
            return False
        try:
            if event.is_private_chat():
                payload = await bot.call_action(
                    "get_friend_msg_history",
                    user_id=str(event.get_sender_id()),
                    count=20,
                    reverse_order=False,
                    disable_get_url=True,
                    parse_mult_msg=False,
                )
            else:
                payload = await bot.call_action(
                    "get_group_msg_history",
                    group_id=str(event.get_group_id()),
                    count=20,
                    reverse_order=False,
                    disable_get_url=True,
                    parse_mult_msg=False,
                )
        except Exception as exc:
            logger.warning(
                "[n5] could not verify an ACK timeout through message history: %s",
                str(exc)[:500],
            )
            return False
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        if not isinstance(messages, list):
            return False
        try:
            expected_size = output_path.stat().st_size
        except OSError:
            return False
        for message in messages:
            if not isinstance(message, dict):
                continue
            sender = message.get("sender", {})
            sender_id = sender.get("user_id") if isinstance(sender, dict) else None
            try:
                message_time = int(message.get("time", 0))
            except (TypeError, ValueError):
                continue
            if str(sender_id) != self_id or message_time < started_at - 5:
                continue
            segments = message.get("message", [])
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if not isinstance(segment, dict) or segment.get("type") != "image":
                    continue
                data = segment.get("data", {})
                if not isinstance(data, dict):
                    continue
                try:
                    file_size = int(data.get("file_size", -1))
                except (TypeError, ValueError):
                    continue
                if file_size == expected_size:
                    return True
        return False

    async def _deliver_generated_image(
        self,
        event: AstrMessageEvent,
        output_path: Path,
        *,
        task_id: str | None = None,
        retry_count: int = 0,
    ) -> None:
        """Send a standalone image with ACK reconciliation and one retry.

        Args:
            event: Original request event used for direct platform delivery.
            output_path: Verified image file to send.
            task_id: Existing task identifier for manual resend.
            retry_count: Number of sends already attempted for the task.
        """
        active_task_id = task_id or await self._record_delivery_task(event, output_path)
        current_retry_count = max(0, retry_count)
        last_error = ""
        for attempt in range(2):
            if attempt == 1:
                current_retry_count += 1
            started_at = int(datetime.now().timestamp())
            try:
                await event.send(event.image_result(str(output_path)))
            except Exception as exc:
                last_error = str(
                    getattr(exc, "wording", "") or getattr(exc, "message", "") or exc
                )[:500]
                if not self._is_delivery_ack_timeout(exc):
                    await self._update_delivery_task(
                        active_task_id,
                        "send_failed",
                        retry_count=current_retry_count,
                        error=last_error,
                    )
                    break
                await self._update_delivery_task(
                    active_task_id,
                    "ack_timeout",
                    retry_count=current_retry_count,
                    error=last_error,
                )
                await asyncio.sleep(
                    max(
                        0.0,
                        float(self.config.get("delivery_verify_delay_seconds", 3)),
                    )
                )
                if await self._delivery_history_contains_image(
                    event,
                    output_path,
                    started_at,
                ):
                    await self._update_delivery_task(
                        active_task_id,
                        "sent_after_ack_timeout",
                        retry_count=current_retry_count,
                        error=last_error,
                    )
                    return
                if attempt == 0:
                    logger.warning(
                        "[n5] image ACK timed out and history did not confirm delivery; "
                        "retrying once. task=%s path=%s",
                        active_task_id,
                        output_path,
                    )
                    continue
                await self._update_delivery_task(
                    active_task_id,
                    "send_failed_after_retry",
                    retry_count=current_retry_count,
                    error=last_error,
                )
                break
            else:
                await self._update_delivery_task(
                    active_task_id,
                    "sent",
                    retry_count=current_retry_count,
                )
                return

        logger.warning(
            "[n5] generated image delivery failed. task=%s path=%s error=%s",
            active_task_id,
            output_path,
            last_error,
        )
        notice = (
            "图片已经生成，但 QQ 图片发送失败。发送 /n5 重发 可再次发送，"
            "不会重新消耗 NAI 点数。"
        )
        try:
            await event.send(event.plain_result(notice))
        except Exception as notice_exc:
            logger.warning(
                "[n5] image delivery failure notice could not be sent: %s",
                str(notice_exc)[:500],
            )

    def _validate_generation_size(self, width: int, height: int) -> tuple[int, int]:
        """Validate a NovelAI size against UI and zero-Anlas constraints."""
        if not 64 <= width <= 2048 or not 64 <= height <= 2048:
            raise NovelAIWebError("宽高必须分别位于 64 到 2048 之间。")
        if width % 64 or height % 64:
            raise NovelAIWebError("宽高必须是 64 的倍数。")
        max_total_pixels = min(
            int(self.config.get("max_total_pixels", 1_048_576)),
            1_048_576,
        )
        if width * height > max_total_pixels:
            raise NovelAIWebError(
                f"总像素不能超过 1024x1024（{max_total_pixels} 像素）。"
            )
        return width, height

    async def _add_artist_string(
        self,
        event: AstrMessageEvent,
        name: str,
        content: str,
    ) -> None:
        """Add or replace one artist string in the current shared library."""
        normalized_name = name.strip()
        normalized_content = content.strip(" ,")
        if not normalized_name or len(normalized_name) > 64:
            raise NovelAIWebError("串名称长度必须为 1 到 64 个字符。")
        if re.search(r"\s", normalized_name):
            raise NovelAIWebError("串名称不能包含空格。")
        if normalized_name in {"默认", "原生", "无"}:
            raise NovelAIWebError("「默认」「原生」「无」是保留名称。")
        if not normalized_content:
            raise NovelAIWebError("画师串内容不能为空。")
        max_prompt_length = int(self.config.get("max_prompt_length", 4000))
        if len(normalized_content) > max_prompt_length:
            raise NovelAIWebError("画师串内容超过 Prompt 长度上限。")

        library_key = self._artist_library_key(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            library = state["libraries"].setdefault(
                library_key,
                {"presets": {}},
            )
            library["presets"][normalized_name] = normalized_content
            self._save_artist_state(state)

    async def _switch_artist_string(
        self,
        event: AstrMessageEvent,
        name: str,
    ) -> None:
        """Select one shared artist string for the current QQ user and group."""
        normalized_name = name.strip()
        if not normalized_name or re.search(r"\s", normalized_name):
            raise NovelAIWebError("用法：/n5 切换画师串 <串名称>|默认|原生")
        sender_id = self._artist_owner_id(event)
        library_key = self._artist_library_key(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            if normalized_name == "默认":
                user_state = state["users"].get(sender_id)
                if user_state is not None:
                    user_state["active_by_library"].pop(library_key, None)
                    self._save_artist_state(state)
                return
            if normalized_name in {"原生", "无"}:
                user_state = state["users"].setdefault(
                    sender_id,
                    self._new_user_state(),
                )
                user_state["active_by_library"][library_key] = ORIGINAL_ARTIST_STYLE
                self._save_artist_state(state)
                return
            library = state["libraries"].get(library_key)
            if library is None or normalized_name not in library["presets"]:
                raise NovelAIWebError(f"本群画师串中不存在「{normalized_name}」。")
            user_state = state["users"].setdefault(
                sender_id,
                self._new_user_state(),
            )
            user_state["active_by_library"][library_key] = normalized_name
            self._save_artist_state(state)

    async def _active_artist_string(
        self,
        event: AstrMessageEvent,
    ) -> tuple[str, str] | None:
        """Return the shared artist string selected by this user in this group."""
        sender_id = self._artist_owner_id(event)
        library_key = self._artist_library_key(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            user_state = state["users"].get(sender_id)
            name = (
                user_state["active_by_library"].get(library_key, "")
                if user_state is not None
                else ""
            )
            if name == ORIGINAL_ARTIST_STYLE:
                return None
            if not name:
                default_name = str(
                    self.config.get(
                        "default_artist_string_name",
                        DEFAULT_ARTIST_STRING_NAME,
                    )
                ).strip()
                default_content = str(
                    self.config.get(
                        "default_artist_string",
                        DEFAULT_ARTIST_STRING,
                    )
                ).strip(" ,")
                return (
                    (default_name or DEFAULT_ARTIST_STRING_NAME, default_content)
                    if default_content
                    else None
                )
            library = state["libraries"].get(library_key)
            content = library["presets"].get(name) if library is not None else None
            if content:
                return name, content.strip(" ,")
            default_name = str(
                self.config.get(
                    "default_artist_string_name",
                    DEFAULT_ARTIST_STRING_NAME,
                )
            ).strip()
            default_content = str(
                self.config.get(
                    "default_artist_string",
                    DEFAULT_ARTIST_STRING,
                )
            ).strip(" ,")
            return (
                (default_name or DEFAULT_ARTIST_STRING_NAME, default_content)
                if default_content
                else None
            )

    async def _artist_string_names_text(self, event: AstrMessageEvent) -> str:
        """List only shared artist-string names and the user's active name."""
        sender_id = self._artist_owner_id(event)
        library_key = self._artist_library_key(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            library = state["libraries"].get(library_key)
            user_state = state["users"].get(sender_id)
            active = (
                user_state["active_by_library"].get(library_key, "")
                if user_state is not None
                else ""
            )
            names = sorted(library["presets"]) if library is not None else []

        if active == ORIGINAL_ARTIST_STYLE:
            active_text = "原生（不添加画师串）"
        elif active:
            active_text = active
        else:
            active_text = (
                str(
                    self.config.get(
                        "default_artist_string_name",
                        DEFAULT_ARTIST_STRING_NAME,
                    )
                ).strip()
                or DEFAULT_ARTIST_STRING_NAME
            )
        lines = [
            f"当前画风：{active_text}",
            f"本群画师串（共 {len(names)} 个）",
        ]
        for name in names[:50]:
            marker = " [当前]" if name == active else ""
            lines.append(f"- {name}{marker}")
        if len(names) > 50:
            lines.append(f"另有 {len(names) - 50} 个未显示。")
        return "\n".join(lines)

    async def _artist_string_detail_text(
        self,
        event: AstrMessageEvent,
        name: str,
    ) -> str:
        """Return the full content of one shared artist string."""
        normalized_name = name.strip()
        if not normalized_name or re.search(r"\s", normalized_name):
            raise NovelAIWebError("用法：/n5 查看画师串 <串名称>")
        library_key = self._artist_library_key(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            library = state["libraries"].get(library_key)
            content = (
                library["presets"].get(normalized_name) if library is not None else None
            )
        if content is None:
            raise NovelAIWebError(f"本群画师串中不存在「{normalized_name}」。")
        return f"画师串「{normalized_name}」\n{content}"

    async def _set_user_generation_size(
        self,
        event: AstrMessageEvent,
        width: int,
        height: int,
    ) -> tuple[int, int]:
        """Persist one validated generation size for the current QQ user."""
        width, height = self._validate_generation_size(width, height)
        sender_id = self._artist_owner_id(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            user_state = state["users"].setdefault(
                sender_id,
                self._new_user_state(),
            )
            user_state["width"] = width
            user_state["height"] = height
            self._save_artist_state(state)
        return width, height

    async def _user_generation_size(
        self,
        event: AstrMessageEvent,
    ) -> tuple[int, int]:
        """Return the generation size selected by the current QQ user."""
        sender_id = self._artist_owner_id(event)
        async with self._artist_state_lock:
            state = self._load_artist_state()
            user_state = state["users"].get(sender_id)
            if user_state is None:
                return DEFAULT_GENERATION_SIZE
            return user_state["width"], user_state["height"]

    def _parse_custom_size(self, value: str) -> tuple[int, int]:
        """Parse custom sizes written as WIDTHxHEIGHT or WIDTH HEIGHT."""
        match = re.fullmatch(r"\s*(\d+)\s*[xX×*\s]\s*(\d+)\s*", value)
        if match is None:
            raise NovelAIWebError("用法：/n5 自定义大小 <宽>x<高>")
        return self._validate_generation_size(
            int(match.group(1)),
            int(match.group(2)),
        )

    async def _join_generation_queue(self) -> int:
        """Register a generation request and return the number ahead of it."""
        async with self._generation_queue_lock:
            requests_ahead = self._generation_queue_size
            self._generation_queue_size += 1
            return requests_ahead

    async def _leave_generation_queue(self) -> None:
        """Remove one completed or cancelled request from the local queue."""
        async with self._generation_queue_lock:
            self._generation_queue_size = max(0, self._generation_queue_size - 1)

    def _rate_limit_settings(self) -> tuple[int, int]:
        """Return validated retry count and fixed delay."""
        try:
            max_retries = int(self.config.get("rate_limit_max_retries", 8))
            wait_seconds = int(self.config.get("rate_limit_wait_seconds", 5))
        except (TypeError, ValueError) as exc:
            raise NovelAIWebError("429 等待配置必须是整数。") from exc
        if not 0 <= max_retries <= 20:
            raise NovelAIWebError("rate_limit_max_retries 必须在 0 到 20 之间。")
        if not 1 <= wait_seconds <= 60:
            raise NovelAIWebError("rate_limit_wait_seconds 必须在 1 到 60 之间。")
        return max_retries, wait_seconds

    @staticmethod
    def _response_is_rate_limited(
        status_code: int,
        content_type: str,
        body: bytes,
    ) -> bool:
        """Detect HTTP 429 and rate-limit errors carried inside API responses.

        Args:
            status_code: HTTP response status.
            content_type: Response content type.
            body: Bounded response payload.

        Returns:
            Whether NovelAI rejected the request due to concurrent generation.
        """
        if status_code == 429:
            return True
        content_type = content_type.lower()
        if not any(
            marker in content_type for marker in ("json", "event-stream", "text")
        ):
            return False
        text = body[:1_048_576].decode("utf-8", errors="replace")
        return bool(
            re.search(
                r'"(?:status|statusCode|code)"\s*:\s*429\b'
                r"|too many requests|rate[ _-]?limit|concurrent generation",
                text,
                flags=re.IGNORECASE,
            )
        )

    def _rate_limit_wait_seconds(self) -> int:
        """Return the configured fixed 429 retry interval."""
        _, wait_seconds = self._rate_limit_settings()
        return wait_seconds

    @staticmethod
    async def _send_private_text_to(
        event: AstrMessageEvent,
        recipient_id: str,
        text: str,
    ) -> None:
        """Send text directly to one QQ through the event's OneBot client."""
        normalized_recipient = str(recipient_id).strip()
        bot = getattr(event, "bot", None)
        send_private_msg = getattr(bot, "send_private_msg", None)
        if not normalized_recipient.isdigit() or not callable(send_private_msg):
            raise NovelAIWebError("当前平台不支持 QQ 私聊通知。")
        try:
            await send_private_msg(
                user_id=int(normalized_recipient),
                message=[{"type": "text", "data": {"text": text}}],
            )
        except Exception as exc:
            raise NovelAIWebError("QQ 私聊通知发送失败。") from exc

    @classmethod
    async def _send_private_text(cls, event: AstrMessageEvent, text: str) -> None:
        """Send text directly to the command sender through OneBot."""
        sender_id = str(event.get_sender_id()).strip()
        try:
            await cls._send_private_text_to(event, sender_id, text)
        except NovelAIWebError as exc:
            raise NovelAIWebError("管理员帮助私聊发送失败。") from exc

    async def _submit_bug_report(
        self,
        event: AstrMessageEvent,
        content: str,
    ) -> tuple[str, int, int]:
        """Persist one report, then best-effort notify configured admins."""
        normalized_content = re.sub(r"\s+", " ", content).strip()
        if not normalized_content:
            raise NovelAIWebError("用法：/n5 bug反馈 <问题描述>")
        if len(normalized_content) > 2000:
            raise NovelAIWebError("Bug 反馈不能超过 2000 个字符。")

        sender_id = self._artist_owner_id(event)
        group_id = "" if event.is_private_chat() else str(event.get_group_id()).strip()
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        async with self._bug_report_lock:
            state = self._load_bug_report_state()
            report_number = state["next_id"]
            state["next_id"] += 1
            state["reports"].append(
                {
                    "id": report_number,
                    "created_at": created_at,
                    "sender_id": sender_id,
                    "group_id": group_id,
                    "content": normalized_content,
                }
            )
            self._save_bug_report_state(state)

        report_id = f"NAI-{report_number:06d}"
        report_location = f"群 {group_id}" if group_id else "私聊"
        notification = "\n".join(
            [
                f"[NovelAI Bug 反馈 {report_id}]",
                f"来源：{report_location}",
                f"提交者 QQ：{sender_id}",
                f"时间：{created_at}",
                f"内容：{normalized_content}",
            ]
        )
        admin_ids = self._normalize_id_list(self.config.get("bug_report_admin_ids", []))
        if not admin_ids:
            admin_ids = self._normalize_id_list(
                self.config.get("allowed_sender_ids", [])
            )
        delivered = 0
        failed = 0
        for admin_id in sorted(admin_ids):
            try:
                await self._send_private_text_to(event, admin_id, notification)
                delivered += 1
            except NovelAIWebError:
                failed += 1
        return report_id, delivered, failed

    @filter.event_message_type(EventMessageType.ALL, priority=sys.maxsize - 2)
    async def hard_route_nai(self, event: AstrMessageEvent):
        """Route explicit NovelAI commands before the default chat pipeline.

        Args:
            event: Message event that may contain an explicit NovelAI command.
        """
        message = event.get_message_str()
        if NAI_STATUS_HARD_ROUTE_PATTERN.fullmatch(message):
            event.should_call_llm(False)
            event.stop_event()
            async for result in self.generation_status(event):
                yield result
            return

        match = NAI_HARD_ROUTE_PATTERN.fullmatch(message)
        if not match:
            return

        event.should_call_llm(False)
        event.stop_event()
        prompt = match.group("prompt") or ""
        logger.info(
            "[novelai] hard route sender=%s",
            event.get_sender_id(),
        )
        async for result in self.generate_image(event, GreedyStr(prompt)):
            yield result

    @filter.regex(re.compile(r"^/?n5(?=\S)", re.IGNORECASE))
    async def reject_malformed_nai_command(self, event: AstrMessageEvent):
        """Stop malformed NovelAI commands before the default chat pipeline.

        Args:
            event: Message event containing a wake-prefixed but malformed command.
        """
        if not event.is_at_or_wake_command:
            return
        event.should_call_llm(False)
        event.stop_event()
        try:
            self._check_access(event)
        except NovelAIWebError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(
            "NovelAI 指令格式错误。请使用「/n5 <子指令>」，"
            "例如：/n5 生成 1girl；发送 /n5 help 查看帮助。"
        )

    @filter.command("n5_status")
    async def generation_status(self, event: AstrMessageEvent):
        """Report PAT, subscription, balance, and free-generation guards.

        Args:
            event: Message event that initiated the command.
        """
        event.should_call_llm(False)
        event.stop_event()
        try:
            self._check_access(event)
            width, height = await self._user_generation_size(event)
            image_model = await self._user_image_model(event)
            selected_artist = await self._active_artist_string(event)
            negative_prompt = await self._user_negative_prompt(event)
            async with self._generation_queue_lock:
                queue_total = self._generation_queue_size
                queue_active = (
                    1 if queue_total > 0 and self._generation_semaphore.locked() else 0
                )
                queue_waiting = max(0, queue_total - queue_active)
            subscription_error = ""
            try:
                subscription = await self._read_subscription()
            except NovelAIWebError as exc:
                subscription = {}
                subscription_error = str(exc)
            steps = int(self.config.get("steps", DEFAULT_STEPS))
            planner_provider = str(
                self.config.get(
                    "prompt_planner_provider_id",
                    DEFAULT_PROMPT_PLANNER_PROVIDER_ID,
                )
            ).strip()
            active = bool(subscription.get("active", False))
            tier = int(subscription.get("tier", 0))
            training_steps = subscription.get("trainingStepsLeft", {})
            if not isinstance(training_steps, dict):
                training_steps = {}
            balance = training_steps.get("fixedTrainingStepsLeft", "未知")
            purchased = training_steps.get("purchasedTrainingSteps", "未知")
        except NovelAIWebError as exc:
            yield event.plain_result(str(exc))
            return
        except (TypeError, ValueError):
            yield event.plain_result("steps 配置必须是整数。")
            return

        free_eligible = (
            active
            and tier == 3
            and width * height
            <= min(
                int(self.config.get("max_total_pixels", 1_048_576)),
                1_048_576,
            )
            and steps <= min(int(self.config.get("max_steps", 28)), 28)
        )
        yield event.plain_result(
            (
                "NovelAI API 状态正常\n"
                if not subscription_error
                else f"NovelAI API 状态异常: {subscription_error}\n"
            )
            + "认证: Persistent API Token\n"
            f"订阅: {'Opus' if tier == 3 else f'Tier {tier}'}"
            f" ({'有效' if active else '无效'})\n"
            f"Anlas: {balance}（已购 {purchased}）\n"
            f"队列: 生成中 {queue_active}，等待 {queue_waiting}，总计 {queue_total}\n"
            f"Prompt 模型: {planner_provider}\n"
            f"绘图模型: {NOVELAI_MODEL_LABELS[image_model]}\n"
            f"当前画风: {selected_artist[0] if selected_artist else '原生'}\n"
            f"负面提示词: {negative_prompt or '未设置'}\n"
            f"尺寸: {width}x{height}\n"
            f"Steps: {steps}\n"
            f"免费参数保护: {'通过' if free_eligible else '不通过'}"
        )

    @filter.command("n5")
    async def generate_image(self, event: AstrMessageEvent, prompt: GreedyStr):
        """Generate one image through the guarded NovelAI API path.

        Args:
            event: Message event that initiated the command.
            prompt: Complete text following the ``/n5`` command.
        """
        event.should_call_llm(False)
        event.stop_event()
        prompt_text = str(prompt).strip()
        if prompt_text.casefold() == "help":
            try:
                self._check_access(event)
                if event.is_admin():
                    await self._send_private_text(event, self._admin_help_text())
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(self._help_text())
            return

        subcommand, separator, arguments = prompt_text.partition(" ")
        arguments = arguments.strip() if separator else ""
        if subcommand == "再来":
            subcommand = "重抽"
        elif subcommand == "角色":
            subcommand = "人物"
        elif subcommand == "画风":
            if not arguments:
                subcommand = "画师串"
            elif arguments.startswith("查看 "):
                subcommand = "查看画师串"
                arguments = arguments.removeprefix("查看 ").strip()
            else:
                subcommand = "切换画师串"
        elif subcommand == "尺寸":
            selected_size = GENERATION_SIZE_PRESETS.get(arguments)
            try:
                self._check_access(event)
                if selected_size is not None:
                    width, height = await self._set_user_generation_size(
                        event,
                        *selected_size,
                    )
                else:
                    width, height = self._parse_custom_size(arguments)
                    await self._set_user_generation_size(event, width, height)
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(f"你的生成尺寸已设置为 {width}x{height}。")
            return
        elif subcommand == "模型":
            try:
                self._check_access(event)
                if arguments:
                    image_model = await self._user_image_model(event, arguments)
                    yield event.plain_result(
                        f"你的绘图模型已切换为 {NOVELAI_MODEL_LABELS[image_model]}。"
                    )
                else:
                    image_model = await self._user_image_model(event)
                    yield event.plain_result(
                        f"当前绘图模型：{NOVELAI_MODEL_LABELS[image_model]}\n"
                        "切换：/n5 模型 V5C 或 /n5 模型 V5F"
                    )
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
            return
        elif subcommand == "诊断":
            try:
                self._check_access(event)
                image_model = await self._user_image_model(event)
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(
                "N5 诊断\n"
                f"指令路由：/n5（未注册 /nai 别名）\n"
                f"规划模型：{self.config.get('prompt_planner_provider_id', DEFAULT_PROMPT_PLANNER_PROVIDER_ID)}\n"
                f"绘图模型：{NOVELAI_MODEL_LABELS[image_model]}\n"
                "图片选择：本条图片优先，其次引用图片；不使用全局 latest 回退\n"
                "角色校正：NovelAI 官方 suggest-tags 为主"
            )
            return
        elif subcommand in {"重发", "最近"}:
            try:
                self._check_access(event)
                if arguments:
                    raise NovelAIWebError(f"用法：/n5 {subcommand}")
                task = await self._last_delivery_task(event)
                if task is None:
                    raise NovelAIWebError("当前会话还没有可用的 N5 生成记录。")
                output_path = Path(task["output_path"]).resolve()
                output_root = (
                    star.StarTools.get_data_dir(PLUGIN_NAME) / "outputs"
                ).resolve()
                if output_path.parent != output_root or not output_path.is_file():
                    raise NovelAIWebError("最近生成的图片文件已不存在，无法重发。")
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            if subcommand == "最近":
                status_labels = {
                    "pending": "等待发送",
                    "sent": "已确认发送",
                    "ack_timeout": "发送回执超时",
                    "sent_after_ack_timeout": "历史记录已确认送达",
                    "send_failed": "发送失败",
                    "send_failed_after_retry": "自动重试后仍失败",
                }
                yield event.plain_result(
                    "N5 最近生成\n"
                    f"任务：{task['task_id'][:8]}\n"
                    f"时间：{task['created_at'] or '未知'}\n"
                    f"交付：{status_labels.get(task['delivery_status'], task['delivery_status'])}\n"
                    f"重试：{task['retry_count']} 次\n"
                    "文件：仍可重发"
                )
                return
            await self._deliver_generated_image(
                event,
                output_path,
                task_id=task["task_id"],
                retry_count=task["retry_count"] + 1,
            )
            return
        if subcommand == "bug反馈":
            try:
                self._check_access(event)
                report_id, delivered, failed = await self._submit_bug_report(
                    event,
                    arguments,
                )
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            if delivered > 0:
                yield event.plain_result(
                    f"Bug 反馈已记录（{report_id}），并已通知管理员。"
                )
            elif failed > 0:
                yield event.plain_result(
                    f"Bug 反馈已记录（{report_id}），但管理员私聊通知失败。"
                )
            else:
                yield event.plain_result(
                    f"Bug 反馈已记录（{report_id}），但尚未配置通知管理员。"
                )
            return

        if subcommand == "添加画师串":
            try:
                self._check_access(event)
                name, name_separator, content = arguments.partition(" ")
                if not name_separator:
                    raise NovelAIWebError("用法：/n5 添加画师串 <串名称> <内容>")
                await self._add_artist_string(event, name, content)
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(f"已保存本群画师串「{name.strip()}」。")
            return

        if subcommand == "切换画师串":
            try:
                self._check_access(event)
                await self._switch_artist_string(event, arguments)
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            if arguments.strip() == "默认":
                yield event.plain_result(
                    f"已切换为全局默认画风「{DEFAULT_ARTIST_STRING_NAME}」。"
                )
            elif arguments.strip() in {"原生", "无"}:
                yield event.plain_result("已切换为 NovelAI 原生画风（不添加画师串）。")
            else:
                yield event.plain_result(
                    f"你的当前画师串已切换为「{arguments.strip()}」。"
                )
            return

        if subcommand == "画师串":
            try:
                self._check_access(event)
                if arguments:
                    raise NovelAIWebError("用法：/n5 画师串")
                artist_text = await self._artist_string_names_text(event)
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(artist_text)
            return

        if subcommand == "查看画师串":
            try:
                self._check_access(event)
                artist_text = await self._artist_string_detail_text(
                    event,
                    arguments,
                )
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(artist_text)
            return

        if subcommand == "负面":
            try:
                self._check_access(event)
                if not arguments:
                    negative_prompt = await self._user_negative_prompt(event)
                    yield event.plain_result(
                        f"你的当前负面提示词：{negative_prompt or '未设置'}"
                    )
                    return
                if arguments in {"清空", "默认", "无"}:
                    await self._user_negative_prompt(event, "")
                    yield event.plain_result("已清空你的负面提示词。")
                    return
                negative_prompt = await self._user_negative_prompt(
                    event,
                    arguments,
                )
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(f"已设置你的负面提示词：{negative_prompt}")
            return

        if subcommand == "创建人物":
            try:
                self._check_access(event)
                name, name_separator, content = arguments.partition(" ")
                if not name_separator:
                    raise NovelAIWebError(
                        "用法：/n5 创建人物 <角色名> <Prompt> [--负面 <内容>]"
                    )
                character_prompt, negative_separator, negative_prompt = (
                    content.partition(" --负面 ")
                )
                if negative_separator and not negative_prompt.strip():
                    raise NovelAIWebError("人物负面提示词不能为空。")
                requires_confirmation = await self._add_character(
                    event,
                    name,
                    character_prompt,
                    negative_prompt if negative_separator else "",
                )
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            if requires_confirmation:
                yield event.plain_result(
                    f"人物「{name.strip()}」已存在，这会替换已有人物，确定吗？"
                    "请在 60 秒内发送 /n5 确认。"
                )
                return
            yield event.plain_result(
                f"已保存全局人物「{name.strip()}」。在任意群或私聊命中该名字时会自动引用。"
            )
            return

        if subcommand == "删除人物":
            try:
                self._check_access(event)
                if not arguments or " " in arguments:
                    raise NovelAIWebError("用法：/n5 删除人物 <角色名>")
                canonical_name = await self._stage_character_deletion(
                    event,
                    arguments,
                )
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(
                f"这会删除你的全局人物「{canonical_name}」，确定吗？"
                "请在 60 秒内发送 /n5 确认。"
            )
            return

        if subcommand == "确认":
            try:
                self._check_access(event)
                if arguments:
                    raise NovelAIWebError("用法：/n5 确认")
                operation, confirmed_name = await self._confirm_character_change(event)
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            if operation == "delete":
                yield event.plain_result(f"已删除全局人物「{confirmed_name}」。")
            else:
                yield event.plain_result(f"已确认覆盖全局人物「{confirmed_name}」。")
            return

        if subcommand == "人物":
            try:
                self._check_access(event)
                character_text = await self._character_text(event, arguments)
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(character_text)
            return

        if subcommand == "切换大小":
            try:
                self._check_access(event)
                selected_size = GENERATION_SIZE_PRESETS.get(arguments)
                if selected_size is None:
                    raise NovelAIWebError("用法：/n5 切换大小 竖图|横图|方图")
                width, height = await self._set_user_generation_size(
                    event,
                    *selected_size,
                )
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(
                f"你的生成大小已切换为「{arguments}」{width}x{height}。"
            )
            return

        if subcommand == "自定义大小":
            try:
                self._check_access(event)
                width, height = self._parse_custom_size(arguments)
                await self._set_user_generation_size(event, width, height)
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.plain_result(f"你的自定义生成大小已设置为 {width}x{height}。")
            return

        if subcommand == "重抽":
            try:
                self._check_access(event)
                if arguments:
                    raise NovelAIWebError("用法：/n5 重抽")
                last_generation = await self._last_successful_prompt(event)
                if last_generation is None:
                    raise NovelAIWebError("还没有可重抽的成功记录，请先使用 /n5 生成。")
                (
                    prompt_text,
                    character_prompts,
                    negative_prompt,
                    character_negative_prompts,
                ) = last_generation
                prompt_text = self._apply_global_nsfw_prompt(prompt_text)
                generation_size = await self._user_generation_size(event)
                image_model = await self._user_image_model(event)
            except NovelAIWebError as exc:
                yield event.plain_result(str(exc))
                return

            await self._join_generation_queue()
            try:
                try:
                    async with self._generation_semaphore:
                        output_path = await self._generate_from_api(
                            prompt_text,
                            generation_size,
                            character_prompts,
                            negative_prompt,
                            character_negative_prompts,
                            image_model=image_model,
                        )
                        await self._remember_last_prompt(
                            event,
                            prompt_text,
                            character_prompts,
                            negative_prompt,
                            character_negative_prompts,
                        )
                except NovelAIWebError as exc:
                    yield event.plain_result(f"生成失败：{exc}")
                    return
                except Exception:
                    logger.exception("Unexpected NovelAI redraw failure")
                    yield event.plain_result(
                        "生成失败：NovelAI API 请求发生未知错误，请稍后再试。"
                    )
                    return
            finally:
                await self._leave_generation_queue()
            await self._deliver_generated_image(event, output_path)
            return

        if subcommand not in {"生成", "漫画", "漫画抽卡", "参考", "原始"}:
            yield event.plain_result(
                "请输入生图描述。\n"
                "示例：/n5 生成 雪夜车站里的银发少女\n"
                "其他模式：\n"
                "/n5 漫画 <剧情>：规划并生成完整的多格漫画页\n"
                "/n5 漫画抽卡 <角色>[，剧情]：随机创作或扩写指定剧情\n"
                "/n5 参考 <修改要求>：结合本条或引用消息中的图片生成\n"
                "/n5 原始 <Prompt>：跳过提示词优化\n"
                "发送 /n5 help 查看完整帮助。"
            )
            return
        prompt_text = arguments
        comic_draw_mode = subcommand == "漫画抽卡"
        comic_mode = subcommand in {"漫画", "漫画抽卡"}
        comic_text_allowed = not bool(COMIC_TEXT_FORBID_PATTERN.search(prompt_text))
        comic_draw_plot_seed = ""
        prompt_parts = [part.strip() for part in prompt_text.split(",") if part.strip()]
        is_direct_prompt = subcommand == "原始"
        if (
            not is_direct_prompt
            and not comic_mode
            and not NATURAL_LANGUAGE_SCRIPT_PATTERN.search(prompt_text)
        ):
            is_direct_prompt = bool(NOVELAI_PROMPT_SIGNAL_PATTERN.search(prompt_text))
            if not is_direct_prompt and len(prompt_parts) >= 2:
                is_direct_prompt = all(
                    len(part) <= 120 and NOVELAI_ASCII_TAG_PATTERN.fullmatch(part)
                    for part in prompt_parts
                )
            if (
                is_direct_prompt
                and NOVELAI_CHARACTER_TAG_PATTERN.search(prompt_text)
                and not EXPLICIT_SUBJECT_COUNT_PATTERN.search(prompt_text)
            ):
                is_direct_prompt = False

        try:
            self._check_access(event)
            max_prompt_length = int(self.config.get("max_prompt_length", 4000))
            if not prompt_text:
                raise NovelAIWebError(f"用法：/n5 {subcommand} <内容>")
            if not 1 <= max_prompt_length <= 20_000:
                raise NovelAIWebError("max_prompt_length 配置必须在 1 到 20000 之间。")
            if len(prompt_text) > max_prompt_length:
                raise NovelAIWebError(
                    f"画面描述过长，当前上限为 {max_prompt_length} 个字符。"
                )
            image_context = await self._request_image_context(event)
            image_model = await self._user_image_model(event)
            if subcommand == "参考" and not image_context.image_urls:
                raise NovelAIWebError(
                    "用法：发送图片并输入 /n5 参考 <修改要求>，或引用一条图片消息。"
                )
            selected_artist = await self._active_artist_string(event)
            artist_prefix_length = 0
            if selected_artist is not None:
                _, artist_content = selected_artist
                artist_prefix_length = len(artist_content) + 2
            planner_max_length = max_prompt_length - artist_prefix_length
            prompt_text, character_replacements = await self._resolve_character_slots(
                event, prompt_text
            )
            explicit_nudity = bool(EXPLICIT_NUDITY_SOURCE_PATTERN.search(prompt_text))
            unresolved_identities: list[str] = []
            creative_reference_context = ""
            if not is_direct_prompt:
                (
                    prompt_text,
                    character_replacements,
                    unresolved_identities,
                    creative_reference_context,
                ) = await self._resolve_planned_character_slots(
                    event,
                    prompt_text,
                    character_replacements,
                    image_context,
                    image_model,
                )
                if comic_draw_mode:
                    explicit_plot = re.search(
                        r"(?:剧情|情节)\s*[:：]\s*(.+)$",
                        prompt_text,
                        re.DOTALL,
                    )
                    plot_candidates = (
                        [explicit_plot.group(1)]
                        if explicit_plot
                        else [
                            prompt_text[separator.end() :]
                            for separator in re.finditer(r"[,，;；]", prompt_text)
                        ]
                    )
                    if not plot_candidates and character_replacements:
                        plot_candidates = [prompt_text]
                    for candidate in plot_candidates:
                        candidate = CHARACTER_SLOT_PATTERN.sub(" ", candidate)
                        candidate = re.sub(
                            r"(?i)\bthe same character\b",
                            " ",
                            candidate,
                        )
                        candidate = re.sub(r"\s+", " ", candidate).strip(
                            " 和与及同、,，;；:：/&+"
                        )
                        if candidate:
                            comic_draw_plot_seed = candidate
                            break
                if creative_reference_context:
                    prompt_text += "\n\n" + creative_reference_context
            character_expansion = sum(
                max(0, len(content) - len(slot))
                for slot, _, content, _ in character_replacements
            )
            planner_max_length -= character_expansion
            if planner_max_length < 1:
                raise NovelAIWebError(
                    "当前画师串与人物 Prompt 已占满 Prompt 长度上限。"
                )
            generation_size = await self._user_generation_size(event)
            negative_prompt = await self._user_negative_prompt(event)
        except NovelAIWebError as exc:
            yield event.plain_result(str(exc))
            return

        await self._join_generation_queue()
        try:
            try:
                async with self._generation_semaphore:
                    comic_text_elements: list[dict[str, str]] = []
                    if not is_direct_prompt:
                        comic_storyboard = ""
                        if comic_mode:
                            comic_storyboard = await self._plan_comic_storyboard(
                                prompt_text,
                                tuple(slot for slot, _, _, _ in character_replacements),
                                image_context.image_urls,
                                image_context.metadata_prompt,
                                comic_draw_mode=comic_draw_mode,
                                comic_draw_plot_seed=comic_draw_plot_seed,
                                comic_text_allowed=comic_text_allowed,
                            )
                            storyboard_payload = json.loads(comic_storyboard)
                            comic_text_elements = [
                                element
                                for panel in storyboard_payload.get("panels", [])
                                for element in panel.get("text_elements", [])
                            ]
                        plan = await self._plan_prompt(
                            prompt_text,
                            planner_max_length,
                            tuple(slot for slot, _, _, _ in character_replacements),
                            image_context.image_urls,
                            image_context.metadata_prompt,
                            comic_mode=comic_mode,
                            comic_draw_mode=comic_draw_mode,
                            comic_draw_plot_seed=comic_draw_plot_seed,
                            comic_storyboard=comic_storyboard,
                            comic_text_allowed=comic_text_allowed,
                        )
                    else:
                        base_prompt = CHARACTER_SLOT_PATTERN.sub("", prompt_text)
                        base_prompt = re.sub(
                            r"\s*,\s*,+",
                            ", ",
                            base_prompt,
                        ).strip(" ,")
                        plan = {
                            "prompt": base_prompt,
                            "character_prompts": {
                                slot: "" for slot, _, _, _ in character_replacements
                            },
                        }
                    prompt_text = plan["prompt"]
                    character_prompts = self._build_character_prompts(
                        character_replacements,
                        plan["character_prompts"],
                        max_prompt_length,
                        explicit_nudity,
                    )
                    character_negative_prompts = tuple(
                        character_negative_prompt
                        for _, _, _, character_negative_prompt in (
                            character_replacements
                        )
                    )
                    if comic_mode:
                        comic_negative_conflicts = {
                            "comic",
                            "comic strip",
                            "manga",
                            "panel",
                            "panels",
                            "multiple views",
                            "duplicate",
                            "frame",
                            "border",
                        }
                        negative_prompt = ", ".join(
                            item.strip()
                            for item in negative_prompt.split(",")
                            if item.strip().casefold() not in comic_negative_conflicts
                        )
                        if not comic_text_elements:
                            if not re.search(
                                r"(?i)(?<![a-z])no text(?![a-z])", prompt_text
                            ):
                                prompt_text = "no text, " + prompt_text
                            negative_items = [
                                item.strip()
                                for item in negative_prompt.split(",")
                                if item.strip()
                            ]
                            present_negative = {
                                item.casefold() for item in negative_items
                            }
                            negative_items.extend(
                                item
                                for item in (
                                    "text",
                                    "captions",
                                    "speech bubbles",
                                    "subtitles",
                                    "watermark",
                                    "signature",
                                )
                                if item.casefold() not in present_negative
                            )
                            negative_prompt = ", ".join(negative_items)
                        else:
                            rendered_text_conflicts = {
                                "text",
                                "captions",
                                "speech bubbles",
                                "subtitles",
                                "no text",
                            }
                            negative_prompt = ", ".join(
                                item.strip()
                                for item in negative_prompt.split(",")
                                if item.strip().casefold()
                                not in rendered_text_conflicts
                            )
                    else:
                        prompt_text = self._apply_character_subject_counts(
                            prompt_text,
                            character_prompts,
                        )
                    if not comic_mode and len(character_prompts) == 1:
                        duplicate_guards = (
                            "multiple girls",
                            "multiple boys",
                            "multiple views",
                            "character sheet",
                            "lineup",
                            "duplicate",
                        )
                        negative_items = [
                            item.strip()
                            for item in negative_prompt.split(",")
                            if item.strip()
                        ]
                        present_negative = {item.casefold() for item in negative_items}
                        negative_items.extend(
                            item
                            for item in duplicate_guards
                            if item.casefold() not in present_negative
                        )
                        negative_prompt = ", ".join(negative_items)
                    if selected_artist is not None:
                        prompt_text = f"{artist_content}, {prompt_text}"
                    prompt_text = self._apply_global_nsfw_prompt(prompt_text)
                    if comic_text_elements:
                        rendered_texts = [
                            element["content"] for element in comic_text_elements
                        ]
                        language_tags: list[str] = []
                        if any(
                            re.search(r"[\u3400-\u9fff]", content)
                            for content in rendered_texts
                        ):
                            language_tags.append("chinese text")
                        if any(
                            re.search(r"[\u3040-\u30ff]", content)
                            for content in rendered_texts
                        ):
                            language_tags.append("japanese text")
                        if any(
                            re.search(r"[A-Za-z]", content)
                            for content in rendered_texts
                        ):
                            language_tags.append("english text")
                        prompt_text = ", ".join(
                            ("text", *language_tags, prompt_text)
                        ).rstrip(" ,")
                        prompt_text += "\nText: " + "\n\n".join(rendered_texts)
                    if len(prompt_text) + sum(map(len, character_prompts)) > (
                        max_prompt_length
                    ):
                        raise NovelAIWebError(
                            "Prompt 规划与画师串、人物 Prompt 拼接后超过长度上限。"
                        )
                    output_path = await self._generate_from_api(
                        prompt_text,
                        generation_size,
                        character_prompts,
                        negative_prompt,
                        character_negative_prompts,
                        image_model=image_model,
                    )
                    await self._remember_last_prompt(
                        event,
                        prompt_text,
                        character_prompts,
                        negative_prompt,
                        character_negative_prompts,
                    )
            except NovelAIWebError as exc:
                yield event.plain_result(f"生成失败：{exc}")
                return
            except Exception:
                logger.exception("Unexpected NovelAI generation failure")
                yield event.plain_result(
                    "生成失败：NovelAI API 请求发生未知错误，请稍后再试。"
                )
                return
        finally:
            await self._leave_generation_queue()
        if unresolved_identities:
            yield event.plain_result(
                "未能用 NovelAI 官方词表精确确认："
                + "、".join(unresolved_identities)
                + "。本次已保留 DS4F 给出的候选与外观描述。"
            )
        await self._deliver_generated_image(event, output_path)

    async def _generate_from_api(
        self,
        prompt: str,
        generation_size: tuple[int, int],
        character_prompts: tuple[str, ...] = (),
        negative_prompt: str = "",
        character_negative_prompts: tuple[str, ...] = (),
        *,
        image_model: str = NOVELAI_MODEL,
    ) -> Path:
        """Submit one guarded free-generation request to the NovelAI API.

        Args:
            prompt: Owner-provided NovelAI prompt.
            generation_size: Width and height selected by the current QQ user.
            character_prompts: Native V5 captions for separately controlled characters.
            negative_prompt: Base NovelAI Undesired Content prompt.
            character_negative_prompts: Per-character V5 negative captions.
            image_model: Canonical NovelAI V5 Curated or Full identifier.

        Returns:
            Path to the verified generated image.

        Raises:
            NovelAIWebError: If a guard, authentication, network, or response fails.
        """
        prompt = self._apply_global_nsfw_prompt(prompt)
        image_model = self._normalize_image_model(image_model)
        try:
            width, height = self._validate_generation_size(*generation_size)
            steps = int(self.config.get("steps", DEFAULT_STEPS))
            max_total_pixels = min(
                int(self.config.get("max_total_pixels", 1_048_576)),
                1_048_576,
            )
            max_steps = min(int(self.config.get("max_steps", 28)), 28)
            timeout_seconds = int(self.config.get("timeout_seconds", 180))
            max_response_bytes = int(
                self.config.get("max_response_bytes", 16 * 1024 * 1024)
            )
            uc_preset = int(self.config.get("uc_preset", 3))
        except (TypeError, ValueError) as exc:
            raise NovelAIWebError("NovelAI API 数值配置必须是整数。") from exc
        if width * height > max_total_pixels:
            raise NovelAIWebError(
                f"已拒绝请求：{width}x{height} 超过免费像素上限 {max_total_pixels}。"
            )
        if not 1 <= steps <= max_steps:
            raise NovelAIWebError(
                f"已拒绝请求：Steps={steps} 不在免费范围 1 到 {max_steps}。"
            )
        if not 30 <= timeout_seconds <= 600:
            raise NovelAIWebError("timeout_seconds 配置必须在 30 到 600 之间。")
        if not 1024 <= max_response_bytes <= 128 * 1024 * 1024:
            raise NovelAIWebError("max_response_bytes 配置超出安全范围。")
        if not 0 <= uc_preset <= 7:
            raise NovelAIWebError("uc_preset 配置必须在 0 到 7 之间。")

        subscription = await self._read_subscription()
        if (
            not bool(subscription.get("active", False))
            or int(subscription.get("tier", 0)) != 3
        ):
            raise NovelAIWebError("已拒绝请求：当前账号不是有效的 NovelAI Opus。")

        negative_prompt = self._normalize_negative_prompt(negative_prompt)
        if len(character_prompts) > 22:
            raise NovelAIWebError("当前插件一次最多支持 22 个人物 Prompt。")
        if len(character_negative_prompts) != len(character_prompts):
            raise NovelAIWebError("人物正面与负面 Prompt 数量不一致。")
        normalized_character_negative_prompts = tuple(
            self._normalize_negative_prompt(item) for item in character_negative_prompts
        )
        if (
            len(prompt)
            + len(negative_prompt)
            + sum(map(len, character_prompts))
            + sum(map(len, normalized_character_negative_prompts))
            > 20_000
        ):
            raise NovelAIWebError("正面与负面 Prompt 总长度超过安全上限。")
        seed = secrets.randbelow(2**32)
        positive_character_captions = [
            {
                "char_caption": character_prompt,
                "centers": [{"x": 0.5, "y": 0.5}],
            }
            for character_prompt in character_prompts
        ]
        negative_character_captions = [
            {
                "char_caption": character_negative_prompt,
                "centers": [{"x": 0.5, "y": 0.5}],
            }
            for character_negative_prompt in normalized_character_negative_prompts
        ]
        payload = {
            "input": prompt,
            "model": image_model,
            "action": "generate",
            "parameters": {
                "params_version": NOVELAI_PARAMS_VERSION,
                "width": width,
                "height": height,
                "scale": 5,
                "sampler": "k_euler_ancestral",
                "steps": steps,
                "n_samples": 1,
                "ucPreset": uc_preset,
                "qualityToggle": bool(self.config.get("quality_toggle", False))
                and not COMIC_TEXT_BLOCK_PATTERN.search(prompt),
                "autoSmea": False,
                "dynamic_thresholding": False,
                "controlnet_strength": 1,
                "legacy": False,
                "add_original_image": True,
                "cfg_rescale": 0,
                "noise_schedule": "karras",
                "legacy_v3_extend": False,
                "skip_cfg_above_sigma": None,
                "use_coords": False,
                "legacy_uc": False,
                "normalize_reference_strength_multiple": True,
                "inpaintImg2ImgStrength": 1,
                "seed": seed,
                "extra_noise_seed": seed,
                "characterPrompts": [],
                "v4_prompt": {
                    "caption": {
                        "base_caption": prompt,
                        "char_captions": positive_character_captions,
                    },
                    "use_coords": False,
                    "use_order": True,
                },
                "v4_negative_prompt": {
                    "caption": {
                        "base_caption": negative_prompt,
                        "char_captions": negative_character_captions,
                    },
                    "legacy_uc": False,
                },
                "negative_prompt": negative_prompt,
                "deliberate_euler_ancestral_bug": False,
                "prefer_brownian": True,
                "image_format": "png",
                "prompt": prompt,
            },
            "use_new_shared_trial": True,
        }

        max_retries, _ = self._rate_limit_settings()
        body: bytes
        content_type = ""
        for retry_index in range(max_retries + 1):
            try:
                async with self._get_api_client().stream(
                    "POST",
                    NOVELAI_IMAGE_ENDPOINT,
                    json=payload,
                    headers={
                        "Accept": "application/zip",
                        "x-correlation-id": uuid4().hex[:6],
                    },
                    timeout=timeout_seconds,
                ) as response:
                    status_code = response.status_code
                    content_type = response.headers.get("content-type", "")
                    chunks: list[bytes] = []
                    total_bytes = 0
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > max_response_bytes:
                            raise NovelAIWebError(
                                "NovelAI API 响应超过配置的大小上限。"
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
            except NovelAIWebError:
                raise
            except httpx.TimeoutException as exc:
                raise NovelAIWebError(
                    "NovelAI API 生成超时；为避免重复生成，本次不会自动重试。"
                ) from exc
            except httpx.HTTPError as exc:
                raise NovelAIWebError("NovelAI API 生成请求失败。") from exc

            if self._response_is_rate_limited(
                status_code,
                content_type,
                body,
            ):
                if retry_index >= max_retries:
                    raise NovelAIWebError(
                        f"NovelAI 持续返回 429；排队重试 {max_retries} 次后仍不可用。"
                    )
                wait_seconds = self._rate_limit_wait_seconds()
                await asyncio.sleep(wait_seconds)
                continue
            if not 200 <= status_code < 300:
                detail = ""
                if "json" in content_type.lower():
                    try:
                        error_data = json.loads(body)
                        if isinstance(error_data, dict):
                            detail = str(error_data.get("message", "")).strip()
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
                suffix = f"：{detail[:200]}" if detail else ""
                raise NovelAIWebError(f"NovelAI API 返回 HTTP {status_code}{suffix}。")
            break

        expected_size = (width, height)
        image_bytes = self._extract_image_from_response(content_type, body)
        if image_bytes is None:
            raise NovelAIWebError("生成完成，但 NovelAI API 响应中没有可识别图片。")
        actual_size = self._image_dimensions(image_bytes)
        if actual_size != expected_size:
            size_text = (
                f"{actual_size[0]}x{actual_size[1]}"
                if actual_size is not None
                else "未知尺寸"
            )
            raise NovelAIWebError(
                f"NovelAI API 返回 {size_text}，期望主图为 {width}x{height}。"
            )
        return self._validate_and_save_image(image_bytes)

    def _extract_image_from_response(
        self,
        content_type: str,
        body: bytes,
    ) -> bytes | None:
        """Extract an image from a JSON or ZIP API response.

        Args:
            content_type: HTTP response content type.
            body: Fully buffered response bytes within the configured limit.

        Returns:
            Original image bytes when recognized, otherwise ``None``.
        """
        candidates: list[bytes] = []
        content_type = content_type.lower()
        if "zip" in content_type or body.startswith(b"PK\x03\x04"):
            try:
                with zipfile.ZipFile(BytesIO(body)) as archive:
                    for entry in archive.infolist():
                        max_image_bytes = int(
                            self.config.get("max_image_bytes", 32 * 1024 * 1024)
                        )
                        if entry.is_dir() or entry.file_size > max_image_bytes:
                            continue
                        data = archive.read(entry)
                        if self._looks_like_image(data):
                            candidates.append(data)
            except (OSError, ValueError, zipfile.BadZipFile):
                return None
            return self._select_largest_image(candidates)

        if "json" in content_type or body.lstrip().startswith((b"{", b"[")):
            try:
                self._collect_images_in_value(json.loads(body), candidates)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass

        if "event-stream" in content_type or b"\ndata:" in body:
            for line in body.decode("utf-8", errors="replace").splitlines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    self._collect_images_in_value(
                        json.loads(payload),
                        candidates,
                    )
                except json.JSONDecodeError:
                    candidate = self._decode_image_string(payload)
                    if candidate is not None:
                        candidates.append(candidate)
        return self._select_largest_image(candidates)

    def _collect_images_in_value(
        self,
        value: object,
        candidates: list[bytes],
        depth: int = 0,
    ) -> None:
        """Collect bounded base64 image candidates from a response object.

        Args:
            value: Decoded JSON or SSE event value.
            candidates: Mutable candidate list shared across the response.
            depth: Current recursive nesting depth.
        """
        if depth > 10 or len(candidates) >= 32:
            return
        if isinstance(value, str):
            candidate = self._decode_image_string(value)
            if candidate is not None:
                candidates.append(candidate)
            return
        if isinstance(value, list):
            for item in value[-32:]:
                self._collect_images_in_value(item, candidates, depth + 1)
                if len(candidates) >= 32:
                    break
            return
        if isinstance(value, dict):
            preferred = ("image", "images", "data", "result", "output")
            for key in preferred:
                if key in value:
                    self._collect_images_in_value(
                        value[key],
                        candidates,
                        depth + 1,
                    )
            for key, item in value.items():
                if key not in preferred:
                    self._collect_images_in_value(item, candidates, depth + 1)
                if len(candidates) >= 32:
                    break

    @staticmethod
    def _image_dimensions(image_bytes: bytes | None) -> tuple[int, int] | None:
        """Read image dimensions without decoding all image pixels."""
        if not image_bytes:
            return None
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                return image.size
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError):
            return None

    def _select_largest_image(self, candidates: list[bytes]) -> bytes | None:
        """Select the valid candidate with the greatest pixel area."""
        ranked: list[tuple[int, int, bytes]] = []
        for candidate in candidates[:32]:
            dimensions = self._image_dimensions(candidate)
            if dimensions is None:
                continue
            width, height = dimensions
            ranked.append((width * height, len(candidate), candidate))
        return max(ranked, default=(0, 0, b""), key=lambda item: item[:2])[2] or None

    def _decode_image_string(self, value: str) -> bytes | None:
        """Decode a possible data URL or base64 image string.

        Args:
            value: Candidate encoded string.

        Returns:
            Image bytes when the magic header is recognized, otherwise ``None``.
        """
        max_image_bytes = int(self.config.get("max_image_bytes", 32 * 1024 * 1024))
        max_encoded_length = 4 * ((max_image_bytes + 2) // 3) + 128
        if len(value) < 128 or len(value) > max_encoded_length:
            return None
        encoded = value.split(",", 1)[1] if value.startswith("data:image/") else value
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return None
        return data if self._looks_like_image(data) else None

    @staticmethod
    def _looks_like_image(data: bytes) -> bool:
        """Check common image magic headers.

        Args:
            data: Candidate image bytes.

        Returns:
            Whether the bytes begin with PNG, JPEG, or WEBP markers.
        """
        if data.startswith(IMAGE_MAGIC[:2]):
            return True
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"

    def _validate_and_save_image(self, image_bytes: bytes) -> Path:
        """Validate response bytes with Pillow and persist them for QQ sending.

        Args:
            image_bytes: Candidate generated image.

        Returns:
            Local path to the verified image.

        Raises:
            NovelAIWebError: If the image exceeds limits or is malformed.
        """
        max_image_bytes = int(self.config.get("max_image_bytes", 32 * 1024 * 1024))
        max_image_pixels = int(self.config.get("max_image_pixels", 16_777_216))
        if not image_bytes or len(image_bytes) > max_image_bytes:
            raise NovelAIWebError("生成图片为空或超过 max_image_bytes。")
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                image_format = image.format
                width, height = image.size
                if image_format not in {"PNG", "JPEG", "WEBP"}:
                    raise NovelAIWebError("NovelAI API 返回了不支持的图片格式。")
                if width <= 0 or height <= 0 or width * height > max_image_pixels:
                    raise NovelAIWebError("生成图片像素数量超过安全上限。")
                image.verify()
            with Image.open(BytesIO(image_bytes)) as image:
                image.load()
        except NovelAIWebError:
            raise
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
            raise NovelAIWebError("NovelAI API 返回的图片数据无效。") from exc

        extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image_format]
        output_dir = star.StarTools.get_data_dir(PLUGIN_NAME) / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{uuid4().hex}{extension}"
        try:
            output_path.write_bytes(image_bytes)
        except OSError as exc:
            raise NovelAIWebError("生成图片无法保存到本地。") from exc
        return output_path

    async def terminate(self) -> None:
        """Close the reusable NovelAI HTTP client."""
        if self._api_client is not None:
            await self._api_client.aclose()
            self._api_client = None
