"""Regression tests for NovelAI generation routing and replies."""

import asyncio
import hashlib
import importlib.util
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
sys.path.insert(0, str(PLUGIN_PATH.parent))

from identity_planner import (  # noqa: E402
    PlannedIdentity,
    PlannedReference,
    identity_alias_key,
)

SPEC = importlib.util.spec_from_file_location("novelai_plugin_under_test", PLUGIN_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeEvent:
    """Return inspectable results without constructing an AstrBot event."""

    def __init__(self, message: str = "") -> None:
        """Initialize default pipeline-control flags."""
        self.is_at_or_wake_command = True
        self.call_llm = True
        self.stopped = False
        self.message = message
        self.sent: list[tuple[str, str]] = []

    def get_message_str(self) -> str:
        """Return the configured raw message text."""
        return self.message

    @staticmethod
    def get_sender_id() -> str:
        """Return a stable sender identifier."""
        return "10001"

    def should_call_llm(self, call_llm: bool) -> None:
        """Record whether AstrBot may enter its default chat pipeline."""
        self.call_llm = call_llm

    def stop_event(self) -> None:
        """Record that no later handler should process this event."""
        self.stopped = True

    @staticmethod
    def plain_result(text: str) -> tuple[str, str]:
        """Build a fake plain-text result.

        Args:
            text: Reply text.

        Returns:
            Result kind and text.
        """
        return "plain", text

    @staticmethod
    def image_result(path: str) -> tuple[str, str]:
        """Build a fake image result.

        Args:
            path: Generated image path.

        Returns:
            Result kind and path.
        """
        return "image", path

    async def send(self, result: tuple[str, str]) -> None:
        """Record one direct plugin-owned delivery.

        Args:
            result: Fake message result sent by the plugin.
        """
        self.sent.append(result)


class AckTimeoutError(Exception):
    """Represent one NapCat send acknowledgement timeout."""

    retcode = 1200
    wording = (
        "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
        "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate"
    )


class CharacterEvent:
    """Identify one group and sender for persistent character tests."""

    def __init__(self, sender_id: str = "10001", group_id: str = "20001") -> None:
        """Initialize stable test identifiers.

        Args:
            sender_id: QQ user identifier.
            group_id: QQ group identifier.
        """
        self.sender_id = sender_id
        self.group_id = group_id

    def get_sender_id(self) -> str:
        """Return the configured sender identifier."""
        return self.sender_id

    def get_group_id(self) -> str:
        """Return the configured group identifier."""
        return self.group_id

    @staticmethod
    def is_private_chat() -> bool:
        """Treat the test event as a group message."""
        return False


class AccessEvent(CharacterEvent):
    """Expose configurable private-chat state for authorization tests."""

    def __init__(
        self,
        sender_id: str = "10001",
        group_id: str = "20001",
        *,
        private: bool = False,
    ) -> None:
        """Initialize sender, group, and chat type.

        Args:
            sender_id: QQ user identifier.
            group_id: QQ group identifier.
            private: Whether the event represents a private chat.
        """
        super().__init__(sender_id=sender_id, group_id=group_id)
        self.private = private

    def is_private_chat(self) -> bool:
        """Return whether the event represents a private chat."""
        return self.private


def build_plugin(
    planned_prompt: str = "planned prompt",
) -> MODULE.NovelAIWebPlugin:
    """Build a minimal plugin instance for command-level tests.

    Args:
        planned_prompt: Value returned by the mocked planner.

    Returns:
        Plugin with generation dependencies mocked.
    """
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "max_prompt_length": 4000,
        "delivery_verify_delay_seconds": 0,
    }
    plugin._generation_semaphore = asyncio.Semaphore(1)
    plugin._check_access = Mock()
    plugin._active_artist_string = AsyncMock(return_value=None)
    plugin._resolve_character_slots = AsyncMock(
        side_effect=lambda _event, prompt: (prompt, []),
    )
    plugin._request_image_context = AsyncMock(
        return_value=MODULE.RequestImageContext((), "", ""),
    )
    plugin._resolve_planned_character_slots = AsyncMock(
        side_effect=lambda _event, description, replacements, _image_context, _model: (
            description,
            replacements,
            [],
            "",
        ),
    )
    plugin._user_image_model = AsyncMock(return_value=MODULE.NOVELAI_MODEL)
    plugin._user_nsfw_enabled = AsyncMock(return_value=True)
    plugin._join_generation_queue = AsyncMock(return_value=2)
    plugin._leave_generation_queue = AsyncMock()
    plugin._plan_prompt = AsyncMock(
        return_value={"prompt": planned_prompt, "character_prompts": {}},
    )
    plugin._restore_character_slots = Mock(side_effect=lambda prompt, _items: prompt)
    plugin._generate_from_api = AsyncMock(return_value=Path("generated.png"))
    plugin._record_delivery_task = AsyncMock(return_value="task-1")
    plugin._update_delivery_task = AsyncMock()
    plugin._delivery_history_contains_image = AsyncMock(return_value=False)
    return plugin


def test_sender_whitelist_applies_to_private_and_group_commands() -> None:
    """Reject an unlisted sender regardless of the conversation type."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "allowed_sender_ids": ["10001"],
        "allow_group": True,
        "allowed_group_ids": [],
    }

    plugin._check_access(AccessEvent(private=True))
    plugin._check_access(AccessEvent())
    with pytest.raises(MODULE.NovelAIWebError, match="使用者白名单"):
        plugin._check_access(AccessEvent(sender_id="10002", private=True))
    with pytest.raises(MODULE.NovelAIWebError, match="使用者白名单"):
        plugin._check_access(AccessEvent(sender_id="10002"))


def test_empty_sender_whitelist_opens_access_to_everyone() -> None:
    """Treat an empty sender list as unrestricted in any conversation."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "allowed_sender_ids": [],
        "allow_group": True,
        "allowed_group_ids": [],
    }

    plugin._check_access(AccessEvent(sender_id="10002", private=True))
    plugin._check_access(AccessEvent(sender_id="10002"))


def test_group_access_defaults_to_open_without_config_key() -> None:
    """Allow group chats when the allow_group key is absent."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "allowed_sender_ids": [],
        "allowed_group_ids": [],
    }

    plugin._check_access(AccessEvent(sender_id="10002"))


def test_empty_group_whitelist_allows_authorized_sender_in_every_group() -> None:
    """Treat an empty group list as unrestricted when group access is enabled."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "allowed_sender_ids": ["10001"],
        "allow_group": True,
        "allowed_group_ids": [],
    }

    plugin._check_access(AccessEvent(group_id="20001"))
    plugin._check_access(AccessEvent(group_id="99999"))


def test_nonempty_group_whitelist_still_limits_authorized_sender() -> None:
    """Keep optional per-group restriction when group identifiers are configured."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "allowed_sender_ids": ["10001"],
        "allow_group": True,
        "allowed_group_ids": ["20001"],
    }

    plugin._check_access(AccessEvent(group_id="20001"))
    with pytest.raises(MODULE.NovelAIWebError, match="群白名单"):
        plugin._check_access(AccessEvent(group_id="20002"))


@pytest.mark.asyncio
async def test_tag_prompt_bypasses_planner_and_success_only_returns_image() -> None:
    """Bypass planning while still applying the global NSFW direction."""
    plugin = build_plugin()
    prompt = "((artist:ame_usari)), [artist:sousouman], 1girl, solo"

    results = [
        result async for result in plugin.generate_image(FakeEvent(), f"生成 {prompt}")
    ]

    plugin._plan_prompt.assert_not_awaited()
    plugin._generate_from_api.assert_awaited_once_with(
        f"nsfw, {prompt}",
        (832, 1216),
        (),
        "",
        (),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=True,
    )
    assert results == []


@pytest.mark.asyncio
async def test_natural_language_still_uses_planner() -> None:
    """Continue expanding concise natural-language scene requests."""
    plugin = build_plugin("1girl, eating ice cream, happy")

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(),
            "生成 一个正在吃冰淇淋的可爱女孩",
        )
    ]

    plugin._plan_prompt.assert_awaited_once()
    plugin._generate_from_api.assert_awaited_once_with(
        "nsfw, 1girl, eating ice cream, happy",
        (832, 1216),
        (),
        "",
        (),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=True,
    )
    assert results == []


@pytest.mark.asyncio
async def test_character_tag_with_chinese_scene_uses_identity_planning() -> None:
    """Plan mixed character tags and natural language instead of leaking raw text."""
    plugin = build_plugin()
    replacements = [
        (
            "__NAI_CHARACTER_SLOT_1__",
            "blaze_the_igniting_spark_(arknights)",
            "girl, blaze the igniting spark (arknights), black hair, animal ears",
            "",
        )
    ]
    plugin._resolve_planned_character_slots = AsyncMock(
        return_value=(
            "__NAI_CHARACTER_SLOT_1__坐在水边",
            replacements,
            [],
            "",
        )
    )
    plugin._plan_prompt = AsyncMock(
        return_value={
            "prompt": "1person, sitting, waterside",
            "character_prompts": {
                "__NAI_CHARACTER_SLOT_1__": "sitting, looking at viewer"
            },
        }
    )

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(),
            "生成 blaze_the_igniting_spark_(arknights)坐在水边",
        )
    ]

    plugin._resolve_planned_character_slots.assert_awaited_once()
    plugin._plan_prompt.assert_awaited_once()
    plugin._generate_from_api.assert_awaited_once_with(
        "nsfw, 1girl, solo, sitting, waterside",
        (832, 1216),
        (
            "girl, blaze the igniting spark (arknights), black hair, animal ears, "
            "sitting, looking at viewer",
        ),
        "multiple girls, multiple boys, multiple views, character sheet, lineup, duplicate",
        ("",),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=True,
    )
    assert results == []


@pytest.mark.asyncio
async def test_character_tag_without_count_uses_identity_planning() -> None:
    """Resolve one bare official character tag before generating it."""
    plugin = build_plugin("1girl, solo, sitting")

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(),
            "生成 blaze_the_igniting_spark_(arknights), sitting",
        )
    ]

    plugin._resolve_planned_character_slots.assert_awaited_once()
    plugin._plan_prompt.assert_awaited_once()
    assert results == []


@pytest.mark.asyncio
async def test_explicit_raw_character_tag_still_skips_planning() -> None:
    """Keep the explicit raw mode untouched for advanced users."""
    plugin = build_plugin()
    prompt = "blaze_the_igniting_spark_(arknights)坐在水边"

    results = [
        result async for result in plugin.generate_image(FakeEvent(), f"原始 {prompt}")
    ]

    plugin._resolve_planned_character_slots.assert_not_awaited()
    plugin._plan_prompt.assert_not_awaited()
    plugin._generate_from_api.assert_awaited_once_with(
        f"nsfw, {prompt}",
        (832, 1216),
        (),
        "",
        (),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=True,
    )
    assert results == []


@pytest.mark.asyncio
async def test_api_failure_only_returns_error() -> None:
    """Return one explicit error and no image when API generation fails."""
    plugin = build_plugin()
    plugin._generate_from_api.side_effect = MODULE.NovelAIWebError("API unavailable")

    results = [
        result
        async for result in plugin.generate_image(FakeEvent(), "生成 1girl, solo")
    ]

    assert results == [("plain", "生成失败：API unavailable")]


@pytest.mark.asyncio
async def test_malformed_nai_command_never_reaches_default_llm() -> None:
    """Return one short usage hint for a missing command separator."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin._check_access = Mock()
    event = FakeEvent()

    results = [result async for result in plugin.reject_malformed_nai_command(event)]

    assert event.call_llm is False
    assert event.stopped is True
    assert results == [
        (
            "plain",
            "NovelAI 指令格式错误。请使用「/n5 <子指令>」，"
            "例如：/n5 生成 1girl；发送 /n5 查看帮助。",
        )
    ]


@pytest.mark.asyncio
async def test_bare_n5_command_returns_full_help() -> None:
    """Show the full command reference when no subcommand is given."""
    plugin = build_plugin()
    event = FakeEvent("/n5")

    results = [result async for result in plugin.generate_image(event, "")]

    assert event.call_llm is False
    assert event.stopped is True
    assert results == [("plain", MODULE.NovelAIWebPlugin._help_text())]


@pytest.mark.asyncio
async def test_outfit_source_is_verified_without_adding_a_visible_character(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep a named wardrobe source out of native visible-character slots."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
        "max_characters_per_prompt": 4,
    }
    plugin.context = SimpleNamespace()
    plugin._identity_alias_lock = asyncio.Lock()
    plugin._get_api_client = Mock()
    cache_path = tmp_path / "identity_aliases.json"
    monkeypatch.setattr(
        MODULE.NovelAIWebPlugin,
        "_identity_alias_state_path",
        staticmethod(lambda: cache_path),
    )

    async def fake_plan_identities(*args, **kwargs):
        assert kwargs["event"] is event
        return (
            [
                PlannedIdentity(
                    source_name="卡缇希娅",
                    work="Wuthering Waves",
                    role="outfit_source",
                    immutable_prompt=(
                        "cartethyia (wuthering waves), girl, long blonde hair"
                    ),
                    verified=True,
                    canonical_tag="cartethyia (wuthering waves)",
                )
            ],
            [],
        )

    monkeypatch.setattr(MODULE, "plan_identities", fake_plan_identities)
    event = FakeEvent()

    (
        description,
        replacements,
        warnings,
        reference_context,
    ) = await plugin._resolve_planned_character_slots(
        event,
        "阿米娅穿着卡缇希娅的衣服",
        [],
        MODULE.RequestImageContext((), "", ""),
    )

    assert replacements == []
    assert warnings == []
    assert reference_context == ""
    assert "cartethyia (wuthering waves)" in description
    assert "not an additional visible character" in description
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert (
        cache["aliases"][identity_alias_key("卡缇希娅", "Wuthering Waves")]
        == "cartethyia (wuthering waves)"
    )


@pytest.mark.asyncio
async def test_creative_reference_context_does_not_create_an_extra_character(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pass a famous technique as trusted scene context for the real subject."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
        "max_characters_per_prompt": 4,
    }
    plugin.context = SimpleNamespace()
    plugin._identity_alias_lock = asyncio.Lock()
    plugin._get_api_client = Mock()
    monkeypatch.setattr(
        MODULE.NovelAIWebPlugin,
        "_identity_alias_state_path",
        staticmethod(lambda: tmp_path / "identity_aliases.json"),
    )

    async def fake_plan_identities(*args, **kwargs):
        return (
            [
                PlannedIdentity(
                    source_name="卡提希娅",
                    work="Wuthering Waves",
                    role="visible_subject",
                    immutable_prompt="cartethyia (wuthering waves), girl",
                    verified=True,
                    canonical_tag="cartethyia (wuthering waves)",
                )
            ],
            [
                PlannedReference(
                    source_name="虚式茈",
                    work="咒术回战",
                    reference_type="technique_reference",
                    canonical_name="Hollow Purple",
                    work_en="Jujutsu Kaisen",
                    visual_blueprint="A violet sphere tears through a destructive corridor.",
                    anchor_tags=("purple energy", "energy sphere"),
                    exclude_subjects=("Satoru Gojo",),
                )
            ],
        )

    monkeypatch.setattr(MODULE, "plan_identities", fake_plan_identities)

    (
        description,
        replacements,
        warnings,
        reference_context,
    ) = await plugin._resolve_planned_character_slots(
        FakeEvent(),
        "让卡提希娅打出虚式茈",
        [],
        MODULE.RequestImageContext((), "", ""),
    )

    assert description.count("__NAI_CHARACTER_SLOT_1__") == 1
    assert len(replacements) == 1
    assert warnings == []
    assert "Hollow Purple" in reference_context
    assert "Satoru Gojo" in reference_context
    assert "__NAI_CHARACTER_SLOT_2__" not in reference_context


def test_creative_reference_bypasses_legacy_character_tag_minimum() -> None:
    """Do not reject a complete reference plan because of old tag counts."""
    description = (
        "让__NAI_CHARACTER_SLOT_1__重现名场面\n"
        "[CREATIVE_REFERENCE_BEGIN]\n"
        "Visual blueprint: A violet sphere tears through a destructive corridor.\n"
        "[CREATIVE_REFERENCE_END]"
    )
    plan = {
        "prompt": "1girl, purple energy. A violet sphere tears through the air.",
        "character_prompts": {
            "__NAI_CHARACTER_SLOT_1__": "arm extended, braced stance"
        },
    }

    errors = MODULE.NovelAIWebPlugin._semantic_plan_errors(description, plan)

    assert not any("人物设计过于简略" in error for error in errors)


@pytest.mark.asyncio
async def test_explicit_nai_command_is_hard_routed_before_default_llm() -> None:
    """Intercept a private-chat slash command before normal conversation."""
    plugin = build_plugin("1girl, silver hair, train station, night")
    event = FakeEvent("/n5 生成 银发少女站在雪夜车站，冷色背光")

    results = [result async for result in plugin.hard_route_nai(event)]

    assert event.call_llm is False
    assert event.stopped is True
    assert results == []
    assert event.sent == [("image", "generated.png")]
    plugin._plan_prompt.assert_awaited_once()


@pytest.mark.asyncio
async def test_natural_nai_mention_is_not_hard_routed() -> None:
    """Leave ordinary conversation that merely mentions NAI untouched."""
    plugin = build_plugin()
    event = FakeEvent("你觉得 nai 的画风怎么样")

    results = [result async for result in plugin.hard_route_nai(event)]

    assert event.call_llm is True
    assert event.stopped is False
    assert results == []


def test_two_girl_spring_hug_plan_passes_semantic_validation() -> None:
    """Accept the exact base semantics requested by the user."""
    raw_response = (
        '{"ok":true,"prompt":"2girls, hugging, outdoors, spring, cherry '
        'blossoms, warm sunlight","character_prompts":{},"error":null}'
    )

    plan = MODULE.NovelAIWebPlugin._parse_planner_response(raw_response, 4000)

    assert (
        MODULE.NovelAIWebPlugin._semantic_plan_errors(
            "A和B两个女孩子在春光下抱在一起",
            plan,
        )
        == []
    )


def test_planner_ignores_unused_extra_fields() -> None:
    """Ignore harmless planner metadata while validating all consumed fields."""
    raw_response = (
        '{"ok":true,"prompt":"1person, standing, snowy train station, night",'
        '"character_prompts":{"__NAI_CHARACTER_SLOT_1__":"standing, looking away"},'
        '"error":null,"reasoning":"expanded the winter scene"}'
    )

    plan = MODULE.NovelAIWebPlugin._parse_planner_response(
        raw_response,
        4000,
        ("__NAI_CHARACTER_SLOT_1__",),
    )

    assert plan == {
        "prompt": "1person, standing, snowy train station, night",
        "character_prompts": {"__NAI_CHARACTER_SLOT_1__": "standing, looking away"},
    }


def test_semantic_validation_does_not_require_topic_specific_tags() -> None:
    """Let the planner express ordinary topics without a fixed vocabulary."""
    plan = {
        "prompt": "two friends meet beneath flowering trees in warm daylight",
        "character_prompts": {},
    }

    assert (
        MODULE.NovelAIWebPlugin._semantic_plan_errors(
            "A和B两个女孩子在春光下抱在一起",
            plan,
        )
        == []
    )


def test_semantic_validation_does_not_require_fixed_action_role_pack() -> None:
    """Leave action wording and character roles to the V5 planner."""
    plan = {
        "prompt": "2people, pushing, dynamic pose",
        "character_prompts": {
            "__NAI_CHARACTER_SLOT_1__": "source#push",
            "__NAI_CHARACTER_SLOT_2__": "target#falling",
        },
    }

    assert (
        MODULE.NovelAIWebPlugin._semantic_plan_errors(
            "__NAI_CHARACTER_SLOT_1__把__NAI_CHARACTER_SLOT_2__推倒",
            plan,
        )
        == []
    )


def test_semantic_validation_has_no_character_tag_count_floor() -> None:
    """Accept concise character captions without a numeric density harness."""
    description = "雪山的圣女__NAI_CHARACTER_SLOT_1__"
    thin_plan = {
        "prompt": "1person, snowy mountain, full body, cold light",
        "character_prompts": {
            "__NAI_CHARACTER_SLOT_1__": (
                "standing, serene expression, looking into distance, cloak"
            )
        },
    }
    assert (
        MODULE.NovelAIWebPlugin._semantic_plan_errors(
            description,
            thin_plan,
        )
        == []
    )


def test_minimal_and_chibi_character_requests_skip_density_guard() -> None:
    """Keep explicit minimal modes exempt from normal character density."""
    plan = {
        "prompt": "1person, simple background",
        "character_prompts": {"__NAI_CHARACTER_SLOT_1__": "standing, smile"},
    }

    assert (
        MODULE.NovelAIWebPlugin._semantic_plan_errors(
            "极简的__NAI_CHARACTER_SLOT_1__",
            plan,
        )
        == []
    )
    assert (
        MODULE.NovelAIWebPlugin._semantic_plan_errors(
            "Q版__NAI_CHARACTER_SLOT_1__",
            plan,
        )
        == []
    )


@pytest.mark.asyncio
async def test_planner_accepts_concise_character_design_without_retry() -> None:
    """Do not retry solely because a character caption has few comma items."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "prompt_planner_enabled": True,
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
    }
    thin_response = Mock(
        completion_text=(
            '{"ok":true,"prompt":"1person, snowy mountain, cold light",'
            '"character_prompts":{"__NAI_CHARACTER_SLOT_1__":'
            '"standing, serene expression, cloak"},"error":null}'
        )
    )
    plugin.context = Mock()
    plugin.context.llm_generate = AsyncMock(return_value=thin_response)

    plan = await plugin._plan_prompt(
        "雪山的圣女__NAI_CHARACTER_SLOT_1__",
        4000,
        ("__NAI_CHARACTER_SLOT_1__",),
    )

    assert plugin.context.llm_generate.await_count == 1
    assert plan["character_prompts"]["__NAI_CHARACTER_SLOT_1__"] == (
        "standing, serene expression, cloak"
    )


@pytest.mark.asyncio
async def test_planner_rejects_invented_slot_and_enforces_empty_contract() -> None:
    """Retry without accepting a character slot invented for a work character."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "prompt_planner_enabled": True,
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
    }
    invented_slot_response = Mock(
        completion_text=(
            '{"ok":true,"prompt":"1girl, rainy alley",'
            '"character_prompts":{"__NAI_CHARACTER_SLOT_1__":"wet clothes"},'
            '"error":null}'
        )
    )
    corrected_response = Mock(
        completion_text=(
            '{"ok":true,"prompt":"1girl, suzuran (arknights), fox ears, '
            'multiple tails, wet clothes, leaning over windowsill, rainy alley",'
            '"character_prompts":{},"error":null}'
        )
    )
    plugin.context = Mock()
    plugin.context.llm_generate = AsyncMock(
        side_effect=[invented_slot_response, corrected_response]
    )

    plan = await plugin._plan_prompt(
        "傍晚雨后的巷子里，明日方舟角色铃兰湿漉漉地趴在窗台偷看",
        4000,
    )

    assert plugin.context.llm_generate.await_count == 2
    assert plan["character_prompts"] == {}
    assert "suzuran (arknights)" in plan["prompt"]
    first_system_prompt = plugin.context.llm_generate.await_args_list[0].kwargs[
        "system_prompt"
    ]
    assert "`character_prompts` 必须严格为 {}" in first_system_prompt
    assert "不得因为出现作品角色名" in first_system_prompt
    retry_prompt = plugin.context.llm_generate.await_args_list[1].kwargs["prompt"]
    assert "`character_prompts` 必须严格为 {}" in retry_prompt
    assert "不得因为出现作品角色名" in retry_prompt


@pytest.mark.asyncio
async def test_planner_system_prompt_lists_only_required_character_slots() -> None:
    """Tell the planner the exact protected character keys for each request."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "prompt_planner_enabled": True,
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
    }
    response = Mock(
        completion_text=(
            '{"ok":true,"prompt":"1person, snowy mountain",'
            '"character_prompts":{"__NAI_CHARACTER_SLOT_2__":'
            '"holy maiden, ceremonial cloak, layered dress, fur trim, high collar, '
            "gold embroidery, holding crystal staff, standing, serene expression, "
            'dignified posture"},"error":null}'
        )
    )
    plugin.context = Mock()
    plugin.context.llm_generate = AsyncMock(return_value=response)

    await plugin._plan_prompt(
        "雪山上的__NAI_CHARACTER_SLOT_2__",
        4000,
        ("__NAI_CHARACTER_SLOT_2__",),
    )

    system_prompt = plugin.context.llm_generate.await_args.kwargs["system_prompt"]
    assert "合法人物槽位恰好为：`__NAI_CHARACTER_SLOT_2__`" in system_prompt
    assert "不得创建列表外的槽位" in system_prompt


def test_native_character_prompts_preserve_identity_and_add_actions() -> None:
    """Keep saved identities separate while applying per-image interactions."""
    replacements = [
        (
            "__NAI_CHARACTER_SLOT_1__",
            "阿红",
            "1girl, solo, red hair, blue eyes",
            "",
        ),
        (
            "__NAI_CHARACTER_SLOT_2__",
            "阿蓝",
            "girl, blue hair, green eyes",
            "",
        ),
    ]
    dynamic_prompts = {
        "__NAI_CHARACTER_SLOT_1__": "girl, mutual#hug, happy",
        "__NAI_CHARACTER_SLOT_2__": "girl, mutual#hug, happy",
    }

    character_prompts = MODULE.NovelAIWebPlugin._build_character_prompts(
        replacements,
        dynamic_prompts,
        4000,
    )

    assert character_prompts == (
        "girl, red hair, blue eyes, mutual#hug, happy",
        "girl, blue hair, green eyes, mutual#hug, happy",
    )


def test_character_subject_counts_come_from_saved_prompts() -> None:
    """Replace planner-guessed counts with protected library subject types."""
    base_prompt = "2people, hugging, outdoors, spring"
    character_prompts = (
        "girl, red hair, mutual#hug",
        "boy, blue hair, mutual#hug",
    )

    result = MODULE.NovelAIWebPlugin._apply_character_subject_counts(
        base_prompt,
        character_prompts,
    )

    assert result == "1girl, 1boy, hugging, outdoors, spring"


def test_single_character_count_replaces_generic_one_person() -> None:
    """Prevent one protected character from becoming 1girl plus 1person."""
    result = MODULE.NovelAIWebPlugin._apply_character_subject_counts(
        "1person, nude, full body, simple background",
        ("girl, cartethyia (wuthering waves), blonde hair",),
    )

    assert result == "1girl, solo, nude, full body, simple background"


def test_explicit_nudity_removes_identity_and_planner_clothing() -> None:
    """Let explicit request clothing state override identity outfit leakage."""
    character_prompts = MODULE.NovelAIWebPlugin._build_character_prompts(
        [
            (
                "__NAI_CHARACTER_SLOT_1__",
                "卡提希娅",
                "girl, cartethyia (wuthering waves), blonde hair, white dress",
                "",
            )
        ],
        {
            "__NAI_CHARACTER_SLOT_1__": (
                "standing, flowing robe, relaxed pose, looking at viewer"
            )
        },
        4000,
        explicit_nudity=True,
    )

    assert character_prompts == (
        "girl, cartethyia (wuthering waves), blonde hair, nude, standing, relaxed pose, looking at viewer",
    )


def test_explicit_nudity_is_a_semantic_anchor() -> None:
    """Retry a planner response that silently replaces nudity with clothing."""
    errors = MODULE.NovelAIWebPlugin._semantic_plan_errors(
        "裸体的__NAI_CHARACTER_SLOT_1__",
        {
            "prompt": "1person, full body, white background",
            "character_prompts": {"__NAI_CHARACTER_SLOT_1__": "white dress, standing"},
        },
    )

    assert errors == ["缺少 nude"]


@pytest.mark.asyncio
async def test_character_generation_uses_native_captions() -> None:
    """Route matched library characters into native V4 captions."""
    plugin = build_plugin()
    replacements = [
        (
            "__NAI_CHARACTER_SLOT_1__",
            "阿红",
            "girl, red hair, blue eyes",
            "extra fingers",
        ),
        (
            "__NAI_CHARACTER_SLOT_2__",
            "阿蓝",
            "girl, blue hair, green eyes",
            "bad eyes",
        ),
    ]
    plugin._resolve_character_slots = AsyncMock(
        return_value=(
            "__NAI_CHARACTER_SLOT_1__和__NAI_CHARACTER_SLOT_2__在春光下抱在一起",
            replacements,
        )
    )
    plugin._plan_prompt = AsyncMock(
        return_value={
            "prompt": "2girls, hugging, outdoors, spring",
            "character_prompts": {
                "__NAI_CHARACTER_SLOT_1__": "girl, mutual#hug",
                "__NAI_CHARACTER_SLOT_2__": "girl, mutual#hug",
            },
        }
    )

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(),
            "生成 阿红和阿蓝在春光下抱在一起",
        )
    ]

    plugin._generate_from_api.assert_awaited_once_with(
        "nsfw, 2girls, hugging, outdoors, spring",
        (832, 1216),
        (
            "girl, red hair, blue eyes, mutual#hug",
            "girl, blue hair, green eyes, mutual#hug",
        ),
        "",
        ("extra fingers", "bad eyes"),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=True,
    )
    assert results == []


@pytest.mark.asyncio
async def test_single_nude_character_adds_solo_nsfw_and_duplicate_guards() -> None:
    """Keep an explicit single-character request singular and globally NSFW."""
    plugin = build_plugin()
    replacements = [
        (
            "__NAI_CHARACTER_SLOT_1__",
            "卡提希娅",
            "girl, cartethyia (wuthering waves), blonde hair, white dress",
            "",
        )
    ]
    plugin._resolve_character_slots = AsyncMock(
        return_value=("裸体的__NAI_CHARACTER_SLOT_1__", replacements)
    )
    plugin._plan_prompt = AsyncMock(
        return_value={
            "prompt": "1person, nude, full body, white background",
            "character_prompts": {"__NAI_CHARACTER_SLOT_1__": "standing, relaxed pose"},
        }
    )

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(),
            "生成 裸体的卡提希娅",
        )
    ]

    plugin._generate_from_api.assert_awaited_once_with(
        "nsfw, 1girl, solo, nude, full body, white background",
        (832, 1216),
        (
            "girl, cartethyia (wuthering waves), blonde hair, nude, standing, relaxed pose",
        ),
        "multiple girls, multiple boys, multiple views, character sheet, lineup, duplicate",
        ("",),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=True,
    )
    assert results == []


@pytest.mark.asyncio
async def test_delivery_ack_timeout_is_confirmed_from_group_history() -> None:
    """Avoid retrying when NapCat history confirms the ambiguous send."""
    plugin = build_plugin()
    event = FakeEvent()
    event.send = AsyncMock(side_effect=AckTimeoutError())
    plugin._delivery_history_contains_image = AsyncMock(return_value=True)

    await plugin._deliver_generated_image(event, Path("generated.png"))

    assert event.send.await_count == 1
    plugin._delivery_history_contains_image.assert_awaited_once()
    assert plugin._update_delivery_task.await_args_list[-1].args[1] == (
        "confirmed_in_history"
    )


@pytest.mark.asyncio
async def test_group_history_confirmation_requires_message_id_and_image_md5(
    tmp_path: Path,
) -> None:
    """Reject size-only history entries and require a strong image fingerprint."""
    plugin = build_plugin()
    output_path = tmp_path / "generated.png"
    output_path.write_bytes(b"image-bytes")
    image_md5 = hashlib.md5(b"image-bytes", usedforsecurity=False).hexdigest()
    event = AccessEvent()
    event.message_obj = SimpleNamespace(raw_message={"self_id": 2806797912})
    event.bot = SimpleNamespace(
        call_action=AsyncMock(
            return_value={
                "messages": [
                    {
                        "time": 200,
                        "sender": {"user_id": 10002},
                        "message": [
                            {
                                "type": "image",
                                "data": {"file_size": len(b"image-bytes")},
                            }
                        ],
                    },
                    {
                        "time": 201,
                        "sender": {"user_id": 2806797912},
                        "message": [
                            {
                                "type": "image",
                                "data": {"file_size": str(len(b"image-bytes"))},
                            }
                        ],
                    },
                    {
                        "message_id": 9001,
                        "time": 202,
                        "sender": {"user_id": 2806797912},
                        "message": [
                            {
                                "type": "image",
                                "data": {
                                    "file_size": str(len(b"image-bytes")),
                                    "file": f"{image_md5.upper()}.image",
                                },
                            }
                        ],
                    },
                ]
            }
        )
    )

    confirmed = await MODULE.NovelAIWebPlugin._delivery_history_contains_image(
        plugin,
        event,
        output_path,
        200,
    )

    assert confirmed is True
    event.bot.call_action.assert_awaited_once_with(
        "get_group_msg_history",
        group_id="20001",
        count=20,
        reverse_order=False,
        disable_get_url=True,
        parse_mult_msg=False,
    )

    event.bot.call_action.reset_mock()
    event.bot.call_action.return_value = {
        "messages": [
            {
                "message_id": 9002,
                "time": 203,
                "sender": {"user_id": 2806797912},
                "message": [
                    {
                        "type": "image",
                        "data": {"file_size": str(len(b"image-bytes"))},
                    }
                ],
            }
        ]
    }
    size_only_confirmation = (
        await MODULE.NovelAIWebPlugin._delivery_history_contains_image(
            plugin,
            event,
            output_path,
            200,
        )
    )

    assert size_only_confirmation is False


@pytest.mark.asyncio
async def test_delivery_ack_timeout_does_not_automatically_resend_image() -> None:
    """Avoid duplicate images when the first send succeeds but its ACK times out."""
    plugin = build_plugin()
    event = FakeEvent()
    event.send = AsyncMock(
        side_effect=[AckTimeoutError(), None],
    )

    await plugin._deliver_generated_image(event, Path("generated.png"))

    assert event.send.await_count == 2
    assert plugin._delivery_history_contains_image.await_count == 1
    assert plugin._update_delivery_task.await_args_list[-1].args[1] == (
        "delivery_uncertain"
    )
    assert event.send.await_args_list[-1].args[0] == (
        "plain",
        "图片发送回执超时，可能已经送达，请检查聊天记录确认。",
    )


@pytest.mark.asyncio
async def test_character_delete_requires_same_user_confirmation(tmp_path: Path) -> None:
    """Delete only after the requesting QQ confirms in the same group."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {"max_character_prompt_length": 2000}
    plugin._character_state_lock = asyncio.Lock()
    plugin._pending_character_changes = {}
    plugin._character_state_path = Mock(return_value=tmp_path / "characters.json")
    plugin._save_character_state(
        {
            "version": 1,
            "libraries": {
                "private:10001": {"prompts": {"撅撅": "cum, sex, steam, wet"}}
            },
        }
    )
    requester = CharacterEvent()
    other_user = CharacterEvent(sender_id="10002")

    staged_name = await plugin._stage_character_deletion(requester, "撅撅")

    assert staged_name == "撅撅"
    assert (
        "撅撅"
        in plugin._load_character_state()["libraries"]["private:10001"]["prompts"]
    )
    with pytest.raises(MODULE.NovelAIWebError, match="没有待确认"):
        await plugin._confirm_character_change(other_user)

    operation, deleted_name = await plugin._confirm_character_change(requester)

    assert (operation, deleted_name) == ("delete", "撅撅")
    assert plugin._load_character_state()["libraries"]["private:10001"]["prompts"] == {}


@pytest.mark.asyncio
async def test_character_delete_confirmation_expires(tmp_path: Path) -> None:
    """Keep a character when its deletion confirmation expires."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {"max_character_prompt_length": 2000}
    plugin._character_state_lock = asyncio.Lock()
    plugin._pending_character_changes = {}
    plugin._character_state_path = Mock(return_value=tmp_path / "characters.json")
    plugin._save_character_state(
        {
            "version": 1,
            "libraries": {
                "private:10001": {"prompts": {"撅撅": "cum, sex, steam, wet"}}
            },
        }
    )
    event = CharacterEvent()
    await plugin._stage_character_deletion(event, "撅撅")
    plugin._pending_character_changes[("private:10001", "10001")]["expires_at"] = (
        MODULE.monotonic() - 1
    )

    with pytest.raises(MODULE.NovelAIWebError, match="已超时"):
        await plugin._confirm_character_change(event)

    assert (
        "撅撅"
        in plugin._load_character_state()["libraries"]["private:10001"]["prompts"]
    )


@pytest.mark.asyncio
async def test_user_image_model_switch_is_persistent_and_user_scoped(
    tmp_path: Path,
) -> None:
    """Persist V5F for one QQ without changing another user's default model."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {"image_model": "nai-diffusion-5-curated"}
    plugin._artist_state_lock = asyncio.Lock()
    plugin._artist_state_path = Mock(return_value=tmp_path / "artist_strings.json")
    first_group = CharacterEvent()
    second_group = CharacterEvent(group_id="20002")
    other_user = CharacterEvent(sender_id="10002")

    assert await plugin._user_image_model(first_group) == MODULE.NOVELAI_MODELS["v5c"]
    assert (
        await plugin._user_image_model(first_group, "V5F")
        == MODULE.NOVELAI_MODELS["v5f"]
    )
    assert await plugin._user_image_model(second_group) == MODULE.NOVELAI_MODELS["v5f"]
    assert await plugin._user_image_model(other_user) == MODULE.NOVELAI_MODELS["v5c"]


@pytest.mark.asyncio
async def test_model_command_switches_to_v5_full() -> None:
    """Expose a copyable chat switch when QQ buttons are unavailable."""
    plugin = build_plugin()
    plugin._user_image_model = AsyncMock(return_value=MODULE.NOVELAI_MODELS["v5f"])
    event = FakeEvent()

    results = [result async for result in plugin.generate_image(event, "模型 V5F")]

    plugin._user_image_model.assert_awaited_once_with(event, "V5F")
    assert results == [("plain", "你的绘图模型已切换为 V5F（Full）。")]


@pytest.mark.asyncio
async def test_character_negative_prompt_is_saved_and_resolved(tmp_path: Path) -> None:
    """Bind a shared character negative caption without changing its identity."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {"max_character_prompt_length": 2000}
    plugin._character_state_lock = asyncio.Lock()
    plugin._pending_character_changes = {}
    plugin._character_state_path = Mock(return_value=tmp_path / "characters.json")
    event = CharacterEvent()

    requires_confirmation = await plugin._add_character(
        event,
        "霜音",
        "1girl, silver hair, blue eyes",
        "extra fingers, bad hands",
    )
    slotted_description, replacements = await plugin._resolve_character_slots(
        event,
        "霜音正在吃冰淇淋",
    )

    assert requires_confirmation is False
    assert "__NAI_CHARACTER_SLOT_1__" in slotted_description
    assert replacements == [
        (
            "__NAI_CHARACTER_SLOT_1__",
            "霜音",
            "1girl, silver hair, blue eyes",
            "extra fingers, bad hands",
        )
    ]
    assert await plugin._character_text(event, "霜音") == (
        "人物「霜音」\n"
        "Prompt：1girl, silver hair, blue eyes\n"
        "负面：extra fingers, bad hands"
    )


@pytest.mark.asyncio
async def test_private_character_is_available_in_every_group(tmp_path: Path) -> None:
    """Share one user's saved character library across private and group chats."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "max_character_prompt_length": 2000,
        "max_characters_per_prompt": 4,
    }
    plugin._character_state_lock = asyncio.Lock()
    plugin._pending_character_changes = {}
    plugin._character_state_path = Mock(return_value=tmp_path / "characters.json")
    private_event = AccessEvent(sender_id="10001", private=True)
    first_group = CharacterEvent(sender_id="10001", group_id="20001")
    second_group = CharacterEvent(sender_id="10001", group_id="20002")

    await plugin._add_character(
        private_event,
        "狐莉",
        "1girl, fox girl, white hair, heterochromia",
        "",
    )
    first_description, first_replacements = await plugin._resolve_character_slots(
        first_group,
        "雪山的圣女狐莉",
    )
    second_description, second_replacements = await plugin._resolve_character_slots(
        second_group,
        "圣女狐莉",
    )

    assert first_description == "雪山的圣女__NAI_CHARACTER_SLOT_1__"
    assert second_description == "圣女__NAI_CHARACTER_SLOT_1__"
    assert first_replacements == second_replacements
    assert first_replacements[0][1:3] == (
        "狐莉",
        "1girl, fox girl, white hair, heterochromia",
    )


@pytest.mark.asyncio
async def test_chibi_planning_keeps_hard_style_and_removes_realism() -> None:
    """Keep Q-version proportions ahead of ordinary semantic expansion."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "prompt_planner_enabled": True,
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
    }
    response = Mock(
        completion_text=(
            '{"ok":true,"prompt":"1girl, cute, realistic proportions, '
            'photorealistic, eating ice cream, outdoors",'
            '"character_prompts":{},"error":null}'
        )
    )
    plugin.context = Mock()
    plugin.context.llm_generate = AsyncMock(return_value=response)

    plan = await plugin._plan_prompt("Q版女孩正在吃冰淇淋", 4000)

    assert plan["prompt"].startswith("chibi, super deformed, ")
    assert "realistic proportions" not in plan["prompt"]
    assert "photorealistic" not in plan["prompt"]
    system_prompt = plugin.context.llm_generate.await_args.kwargs["system_prompt"]
    assert "NovelAI Diffusion V5 Curated" in system_prompt
    assert "[llm][v5-no-density-target]" in system_prompt
    assert "[deterministic][single-subject-solo]" in system_prompt
    assert "[deterministic][v5-rendered-text-block]" in system_prompt
    assert "[llm][base-character-responsibility]" in system_prompt
    assert "机器输出协议与 API 安全边界 > 用户本次明确要求" in system_prompt
    assert "角色展示、环境叙事、尺度对比奇观或物体中心" in system_prompt
    assert "本图专属的身份呈现、主题服装" in system_prompt
    assert "不设固定项目数、Tag 数、字数或句数" in system_prompt
    assert "最小必要的一组紧凑标签" in system_prompt
    assert "至少输出 `painter" not in system_prompt
    assert "A把B推倒" not in system_prompt
    assert "至少 3 个可见信号" not in system_prompt


def test_official_knowledge_is_model_scoped_and_traceable() -> None:
    """Keep every runtime official rule tied to a declared official source."""
    manifest = json.loads(MODULE.OFFICIAL_SOURCE_MANIFEST_PATH.read_text("utf-8"))
    rules = json.loads(MODULE.OFFICIAL_RULES_PATH.read_text("utf-8"))
    preferences = json.loads(MODULE.LOCAL_PREFERENCES_PATH.read_text("utf-8"))
    source_ids = {
        source["id"]
        for source in manifest["sources"]
        if source["authority"] == "official"
    }

    assert set(rules["models"]) == set(MODULE.NOVELAI_MODELS.values())
    assert rules["rules"]
    assert all(rule["sources"] for rule in rules["rules"])
    assert all(set(rule["sources"]) <= source_ids for rule in rules["rules"])
    assert all(
        rule["enforcement"] in {"deterministic", "llm", "soft"}
        for rule in rules["rules"]
    )
    assert preferences["priority"].startswith("Local preferences apply only after")


def test_global_nsfw_preserves_the_terminal_text_block_verbatim() -> None:
    """Keep official Text content and blank-line separators at the absolute end."""
    prompt = "comic, Panel 1 action, rating:general\nText: Hello, world!\n\n砰！"

    normalized = MODULE.NovelAIWebPlugin._apply_global_nsfw_prompt(prompt)

    assert normalized == ("nsfw, comic, Panel 1 action\nText: Hello, world!\n\n砰！")
    assert normalized.endswith("Text: Hello, world!\n\n砰！")


@pytest.mark.asyncio
async def test_v5_payload_uses_global_nsfw_without_content_rating() -> None:
    """Send global NSFW while removing content ratings from the V5 payload."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "steps": 23,
        "max_total_pixels": 1_048_576,
        "max_steps": 28,
        "timeout_seconds": 180,
        "max_response_bytes": 16 * 1024 * 1024,
        "quality_toggle": False,
        "uc_preset": 3,
    }
    plugin._read_subscription = AsyncMock(return_value={"active": True, "tier": 3})
    plugin._validate_and_save_image = Mock(return_value=Path("generated.png"))

    image_buffer = BytesIO()
    Image.new("RGB", (832, 1216), "white").save(image_buffer, format="PNG")
    archive_buffer = BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("image.png", image_buffer.getvalue())

    class FakeResponse:
        """Expose one successful streamed ZIP response."""

        status_code = 200
        headers = {"content-type": "application/zip"}

        async def __aenter__(self):
            """Enter the fake response context."""
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            """Leave the fake response context."""

        async def aiter_bytes(self):
            """Yield the complete fake ZIP body."""
            yield archive_buffer.getvalue()

    class CapturingClient:
        """Capture the outgoing NovelAI payload."""

        def __init__(self) -> None:
            """Initialize without a captured payload."""
            self.payload = None

        def stream(self, _method, _endpoint, *, json, **_kwargs):
            """Capture request JSON and return a fake stream.

            Args:
                _method: HTTP method ignored by this fake.
                _endpoint: Request endpoint ignored by this fake.
                json: Outgoing JSON body.
                **_kwargs: Remaining HTTP options ignored by this fake.

            Returns:
                Successful fake response context manager.
            """
            self.payload = json
            return FakeResponse()

    client = CapturingClient()
    plugin._get_api_client = Mock(return_value=client)

    result = await plugin._generate_from_api(
        "1girl, solo, rating:explicit, NSFW",
        (832, 1216),
        image_model=MODULE.NOVELAI_MODELS["v5f"],
    )

    assert result == Path("generated.png")
    assert client.payload["input"] == "nsfw, 1girl, solo"
    assert client.payload["model"] == "nai-diffusion-5-full"
    parameters = client.payload["parameters"]
    assert parameters["params_version"] == 4
    assert parameters["qualityToggle"] is False
    assert parameters["ucPreset"] == 3
    assert parameters["extra_noise_seed"] == parameters["seed"]


@pytest.mark.asyncio
async def test_default_artist_and_explicit_original_are_distinct(
    tmp_path: Path,
) -> None:
    """Apply the global snapshot unless the user explicitly chooses original."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "default_artist_string_name": "千代noob",
        "default_artist_string": "artist:test,",
    }
    plugin._artist_state_lock = asyncio.Lock()
    plugin._artist_state_path = Mock(return_value=tmp_path / "artist_strings.json")
    event = CharacterEvent()

    assert await plugin._active_artist_string(event) == ("千代noob", "artist:test")

    await plugin._switch_artist_string(event, "原生")
    assert await plugin._active_artist_string(event) is None

    await plugin._switch_artist_string(event, "默认")
    assert await plugin._active_artist_string(event) == ("千代noob", "artist:test")


@pytest.mark.asyncio
async def test_status_reports_queue_and_models_without_generation_lock() -> None:
    """Expose live local queue state while one request owns the semaphore."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "steps": 23,
        "max_total_pixels": 1_048_576,
        "max_steps": 28,
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
    }
    plugin._check_access = Mock()
    plugin._user_image_model = AsyncMock(return_value=MODULE.NOVELAI_MODELS["v5f"])
    plugin._user_nsfw_enabled = AsyncMock(return_value=True)
    plugin._active_artist_string = AsyncMock(return_value=("千代noob", "artist:test"))
    plugin._generation_queue_lock = asyncio.Lock()
    plugin._generation_queue_size = 3
    plugin._generation_semaphore = asyncio.Semaphore(0)
    plugin._read_subscription = AsyncMock(
        return_value={
            "active": True,
            "tier": 3,
            "trainingStepsLeft": {
                "fixedTrainingStepsLeft": 9000,
                "purchasedTrainingSteps": 0,
            },
        }
    )

    results = [result async for result in plugin.generation_status(FakeEvent())]

    assert len(results) == 1
    status = results[0][1]
    assert "队列: 生成中 1，等待 2，总计 3" in status
    assert "Prompt 模型: deepseek/deepseek-v4-flash-vision-exp" in status
    assert "绘图模型: V5F（Full）" in status
    assert "当前画风: 千代noob" in status
    assert "NSFW: 开" in status
    plugin._read_subscription.assert_awaited_once()


@pytest.mark.asyncio
async def test_generation_size_keyword_landscape() -> None:
    """Use 1216x832 for one request without changing the default size."""
    plugin = build_plugin()

    results = [
        result
        async for result in plugin.generate_image(FakeEvent(), "生成 雪夜少女 横图")
    ]

    assert plugin._plan_prompt.await_args.args[0] == "雪夜少女"
    plugin._generate_from_api.assert_awaited_once_with(
        "nsfw, planned prompt",
        (1216, 832),
        (),
        "",
        (),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=True,
    )
    assert results == []


@pytest.mark.asyncio
async def test_generation_size_keyword_square() -> None:
    """Use 1024x1024 for one request without changing the default size."""
    plugin = build_plugin()

    results = [
        result
        async for result in plugin.generate_image(FakeEvent(), "生成 雪夜少女 方图")
    ]

    assert plugin._plan_prompt.await_args.args[0] == "雪夜少女"
    plugin._generate_from_api.assert_awaited_once_with(
        "nsfw, planned prompt",
        (1024, 1024),
        (),
        "",
        (),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=True,
    )
    assert results == []


@pytest.mark.asyncio
async def test_generation_size_keyword_ultrawide() -> None:
    """Use 1536x640 for one cinematic request within the pixel cap."""
    plugin = build_plugin()

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(), "生成 雪夜少女 电影超宽屏"
        )
    ]

    assert plugin._plan_prompt.await_args.args[0] == "雪夜少女"
    plugin._generate_from_api.assert_awaited_once_with(
        "nsfw, planned prompt",
        (1536, 640),
        (),
        "",
        (),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=True,
    )
    assert results == []


COMIC_STORYBOARD_DICT = {
    "reading_order": "right-to-left",
    "panels": [
        {
            "panel": 1,
            "placement": "右侧竖长主格",
            "shot": "medium shot",
            "camera": "eye level",
            "scene": "neon street at night",
            "characters": [
                {
                    "slot": "",
                    "identity": "girl, silver hair",
                    "state": "holding umbrella, surprised",
                    "dialogue": "雨下大了",
                }
            ],
            "narration": "",
        },
        {
            "panel": 2,
            "placement": "左侧小格",
            "shot": "close-up",
            "camera": "low angle",
            "scene": "",
            "characters": [
                {
                    "slot": "",
                    "identity": "girl, black hair",
                    "state": "smiling",
                    "dialogue": "",
                }
            ],
            "narration": "第二天",
        },
    ],
}


def test_comic_storyboard_parser_accepts_valid_panels() -> None:
    """Accept a two-panel storyboard with dialogue and narration."""
    import json as json_module

    raw = json_module.dumps(
        {"ok": True, **COMIC_STORYBOARD_DICT, "error": None},
        ensure_ascii=False,
    )

    storyboard = MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(raw, ())

    assert storyboard["reading_order"] == "right-to-left"
    assert [panel["panel"] for panel in storyboard["panels"]] == [1, 2]
    assert storyboard["panels"][1]["narration"] == "第二天"


def test_comic_storyboard_parser_rejects_unknown_slot() -> None:
    """Reject panel characters that reference slots outside the cast."""
    import copy as copy_module
    import json as json_module

    payload = copy_module.deepcopy(COMIC_STORYBOARD_DICT)
    payload["panels"][0]["characters"][0]["slot"] = "__NAI_CHARACTER_SLOT_9__"
    raw = json_module.dumps({"ok": True, **payload, "error": None}, ensure_ascii=False)

    with pytest.raises(MODULE.NovelAIWebError, match="未知人物槽位"):
        MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(
            raw, ("__NAI_CHARACTER_SLOT_1__",)
        )


def test_comic_storyboard_parser_rejects_too_many_panels() -> None:
    """Reject storyboards larger than a single page."""
    import copy as copy_module
    import json as json_module

    payload = copy_module.deepcopy(COMIC_STORYBOARD_DICT)
    for number in range(3, 6):
        extra = copy_module.deepcopy(payload["panels"][0])
        extra["panel"] = number
        payload["panels"].append(extra)
    raw = json_module.dumps({"ok": True, **payload, "error": None}, ensure_ascii=False)

    with pytest.raises(MODULE.NovelAIWebError, match="1 到 4"):
        MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(raw, ())


def test_comic_build_skips_saved_appearance() -> None:
    """Keep only identity plus panel state, never saved fixed appearance."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    replacements = [
        (
            "__NAI_CHARACTER_SLOT_1__",
            "芙宁娜",
            "furina (genshin impact), blue eyes, long hair, blue dress",
            "",
        )
    ]
    storyboard = {
        "reading_order": "right-to-left",
        "panels": [
            {
                "panel": 1,
                "placement": "整页单格",
                "shot": "",
                "camera": "",
                "scene": "",
                "characters": [
                    {
                        "slot": "__NAI_CHARACTER_SLOT_1__",
                        "identity": "furina (genshin impact), girl",
                        "state": "holding umbrella",
                        "dialogue": "",
                    }
                ],
                "narration": "",
            }
        ],
    }

    base, captions, summary = plugin._build_comic_prompts(storyboard, replacements)

    assert "1-panel manga page" in base
    assert captions == ["furina (genshin impact), girl, holding umbrella"]
    assert "blue eyes" not in captions[0]
    assert "long hair" not in captions[0]
    assert "第 1 格" in summary


def test_comic_build_dedupes_repeated_identity_in_panel() -> None:
    """Collapse identical characters that the planner repeats in one panel."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    character = {
        "slot": "",
        "identity": "cartethyia (wuthering waves), girl",
        "state": "waking up",
        "dialogue": "",
    }
    storyboard = {
        "reading_order": "right-to-left",
        "panels": [
            {
                "panel": 1,
                "placement": "整页单格",
                "shot": "",
                "camera": "",
                "scene": "",
                "characters": [character, dict(character), dict(character)],
                "narration": "",
            }
        ],
    }

    base, captions, summary = plugin._build_comic_prompts(storyboard, [])

    assert captions == ["cartethyia (wuthering waves), girl, waking up"]
    assert summary.count("cartethyia (wuthering waves), girl") == 1
    assert "1-panel manga page" in base


def test_comic_slot_centers_follow_stagger_table() -> None:
    """Stagger slot coordinates per the comic skill table."""
    assert MODULE.NovelAIWebPlugin._comic_slot_centers(1) == [0.1]
    assert MODULE.NovelAIWebPlugin._comic_slot_centers(3) == [0.1, 0.3, 0.5]
    assert MODULE.NovelAIWebPlugin._comic_slot_centers(5) == [0.1, 0.3, 0.5, 0.7, 0.9]
    spread = MODULE.NovelAIWebPlugin._comic_slot_centers(6)
    assert spread == sorted(spread) and spread[0] > 0 and spread[-1] < 1


@pytest.mark.asyncio
async def test_comic_command_uses_comic_payload() -> None:
    """Plan a storyboard and generate with comic API parameters."""
    import copy as copy_module

    plugin = build_plugin()
    plugin._plan_comic_storyboard = AsyncMock(
        return_value=copy_module.deepcopy(COMIC_STORYBOARD_DICT)
    )
    event = FakeEvent()

    results = [
        result async for result in plugin.generate_image(event, "漫画 银发少女的雨夜")
    ]

    plugin._plan_prompt.assert_not_awaited()
    plugin._plan_comic_storyboard.assert_awaited_once()
    assert plugin._plan_comic_storyboard.await_args.args[0] == "银发少女的雨夜"
    plugin._generate_from_api.assert_awaited_once_with(
        "nsfw, 2-panel manga page, asymmetric comic layout, right-to-left, "
        "masterpiece, best quality, Panel 1 (右侧竖长主格): medium shot, "
        "eye level. neon street at night, Panel 2 (左侧小格): close-up, "
        "low angle, clean panel borders, white gutter",
        (832, 1216),
        (
            "girl, silver hair, holding umbrella, surprised, "
            'speech bubble, text"雨下大了"',
            'speech bubble, rectangular narration box, text"第二天"',
            "girl, black hair, smiling",
        ),
        "",
        ("", "", ""),
        image_model=MODULE.NOVELAI_MODEL,
        scale=7.0,
        uc_preset_override=0,
        use_coords=True,
        slot_centers=(0.1, 0.3, 0.5),
        apply_nsfw=True,
    )
    assert results == []
    assert event.sent[0][0] == "image"


@pytest.mark.asyncio
async def test_comic_empty_plot_returns_usage() -> None:
    """Reject a comic command without a plot."""
    plugin = build_plugin()

    results = [
        result
        async for result in plugin.generate_image(event=FakeEvent(), prompt="漫画 ")
    ]

    assert results == [("plain", "用法：/n5 漫画 <内容>")]
    plugin._plan_comic_storyboard = AsyncMock()
    plugin._plan_comic_storyboard.assert_not_awaited()


@pytest.mark.asyncio
async def test_comic_size_suffix_applies_to_request() -> None:
    """Honor a trailing size keyword on comic requests."""
    import copy as copy_module

    plugin = build_plugin()
    plugin._plan_comic_storyboard = AsyncMock(
        return_value=copy_module.deepcopy(COMIC_STORYBOARD_DICT)
    )

    results = [
        result
        async for result in plugin.generate_image(FakeEvent(), "漫画 银发少女 横图")
    ]

    assert plugin._plan_comic_storyboard.await_args.args[0] == "银发少女"
    assert plugin._generate_from_api.await_args.args[1] == (1216, 832)
    assert results == []


@pytest.mark.asyncio
async def test_nsfw_command_toggles_switch() -> None:
    """Flip between NSFW and safe mode when no selection is given."""
    plugin = build_plugin()
    plugin._user_nsfw_enabled = AsyncMock(side_effect=[True, False, False, True])
    event = FakeEvent()

    results = [result async for result in plugin.generate_image(event, "nsfw")]

    assert results == [
        (
            "plain",
            "你的 NSFW 已关闭（safe 安全模式）。生图时改用 rating:safe，不再加入 nsfw。",
        )
    ]
    results = [result async for result in plugin.generate_image(event, "nsfw")]

    assert results == [("plain", "你的 NSFW 已开启。生图时会自动加入 nsfw 方向词。")]


@pytest.mark.asyncio
async def test_nsfw_command_explicit_on_off() -> None:
    """Persist an explicit NSFW switch selection."""
    plugin = build_plugin()
    plugin._user_nsfw_enabled = AsyncMock(return_value=False)
    event = FakeEvent()

    results = [result async for result in plugin.generate_image(event, "nsfw safe")]

    plugin._user_nsfw_enabled.assert_awaited_once_with(event, "safe")
    assert results == [
        (
            "plain",
            "你的 NSFW 已关闭（safe 安全模式）。生图时改用 rating:safe，不再加入 nsfw。",
        )
    ]


@pytest.mark.asyncio
async def test_nsfw_safe_mode_applies_safe_rating() -> None:
    """Apply rating:safe while still removing user-supplied rating tags."""
    plugin = build_plugin()
    plugin._user_nsfw_enabled = AsyncMock(return_value=False)

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(), "生成 1girl, rating:general"
        )
    ]

    plugin._generate_from_api.assert_awaited_once_with(
        "rating:safe, 1girl",
        (832, 1216),
        (),
        "",
        (),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=False,
    )
    assert results == []


@pytest.mark.asyncio
async def test_nsfw_switch_is_persistent_and_user_scoped(
    tmp_path: Path,
) -> None:
    """Persist the NSFW switch for one QQ without changing another user."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {}
    plugin._artist_state_lock = asyncio.Lock()
    plugin._artist_state_path = Mock(return_value=tmp_path / "artist_strings.json")
    first_user = CharacterEvent()
    other_user = CharacterEvent(sender_id="10002")

    assert await plugin._user_nsfw_enabled(first_user) is False
    assert await plugin._user_nsfw_enabled(first_user, "开") is True
    assert await plugin._user_nsfw_enabled(first_user) is True
    assert await plugin._user_nsfw_enabled(other_user) is False


@pytest.mark.asyncio
async def test_emote_forces_square_and_chibi_locks() -> None:
    """Lock emotes to square canvas with chibi style tags."""
    plugin = build_plugin()

    results = [
        result
        async for result in plugin.generate_image(FakeEvent(), "表情包 大笑 横图")
    ]

    assert plugin._plan_prompt.await_args.args[0] == "大笑"
    plugin._generate_from_api.assert_awaited_once_with(
        "nsfw, planned prompt, chibi, super deformed, upper body, "
        "white background, simple background, no text",
        (1024, 1024),
        (),
        "text, captions, speech bubbles, subtitles, watermark, signature",
        (),
        image_model=MODULE.NOVELAI_MODEL,
        scale=5,
        uc_preset_override=None,
        use_coords=False,
        slot_centers=None,
        apply_nsfw=True,
    )
    assert results == []


@pytest.mark.asyncio
async def test_emote_caption_appended_as_text_block() -> None:
    """Append short captions to the emote prompt text block."""
    plugin = build_plugin()

    results = [
        result
        async for result in plugin.generate_image(FakeEvent(), "表情包 大笑 配字 哈哈")
    ]

    assert plugin._plan_prompt.await_args.args[0] == "大笑"
    prompt = plugin._generate_from_api.await_args.args[0]
    assert prompt.endswith("\nText: 哈哈")
    assert "chibi, super deformed" in prompt
    assert results == []


@pytest.mark.asyncio
async def test_emote_caption_too_long_returns_usage_error() -> None:
    """Reject captions longer than the sticker text budget."""
    plugin = build_plugin()

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(), "表情包 大笑 配字 哈哈哈哈哈哈哈哈哈"
        )
    ]

    assert results == [("plain", "配字过长，请控制在 8 字以内。")]
    plugin._generate_from_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_emote_empty_emotion_returns_usage() -> None:
    """Reject an emote command without an emotion description."""
    plugin = build_plugin()

    results = [result async for result in plugin.generate_image(FakeEvent(), "表情包 ")]

    assert results == [("plain", "用法：/n5 表情包 <内容>")]
    plugin._generate_from_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_emote_ascii_input_still_uses_planner() -> None:
    """Never treat emote input as a direct tag prompt."""
    plugin = build_plugin()

    results = [
        result
        async for result in plugin.generate_image(FakeEvent(), "表情包 happy dance")
    ]

    plugin._plan_prompt.assert_awaited_once()
    assert plugin._plan_prompt.await_args.args[0] == "happy dance"
    assert results == []
