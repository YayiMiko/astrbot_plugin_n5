"""Regression tests for NovelAI generation routing and replies."""

import asyncio
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
    plugin.config = {"max_prompt_length": 4000}
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
        side_effect=lambda _event, description, replacements, _image_context: (
            description,
            replacements,
            [],
            "",
        ),
    )
    plugin._user_generation_size = AsyncMock(return_value=(832, 1216))
    plugin._user_negative_prompt = AsyncMock(return_value="")
    plugin._join_generation_queue = AsyncMock(return_value=2)
    plugin._leave_generation_queue = AsyncMock()
    plugin._plan_prompt = AsyncMock(
        return_value={"prompt": planned_prompt, "character_prompts": {}},
    )
    plugin._restore_character_slots = Mock(side_effect=lambda prompt, _items: prompt)
    plugin._generate_from_api = AsyncMock(return_value=Path("generated.png"))
    plugin._remember_last_prompt = AsyncMock()
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
    """Keep a complete NovelAI tag prompt byte-for-byte unchanged."""
    plugin = build_plugin()
    prompt = "((artist:ame_usari)), [artist:sousouman], 1girl, solo"

    results = [
        result async for result in plugin.generate_image(FakeEvent(), f"生成 {prompt}")
    ]

    plugin._plan_prompt.assert_not_awaited()
    plugin._generate_from_api.assert_awaited_once_with(
        prompt,
        (832, 1216),
        (),
        "",
        (),
    )
    assert results == [("image", "generated.png")]


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
        "1girl, eating ice cream, happy",
        (832, 1216),
        (),
        "",
        (),
    )
    assert results == [("image", "generated.png")]


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
            "例如：/n5 生成 1girl；发送 /n5 help 查看帮助。",
        )
    ]


@pytest.mark.asyncio
async def test_empty_n5_command_returns_copyable_examples() -> None:
    """Explain generation modes with commands users can copy directly."""
    plugin = build_plugin()
    event = FakeEvent("/n5")

    results = [result async for result in plugin.generate_image(event, "")]

    assert event.call_llm is False
    assert event.stopped is True
    assert results == [
        (
            "plain",
            "请输入生图描述。\n"
            "示例：/n5 生成 雪夜车站里的银发少女\n"
            "其他模式：\n"
            "/n5 参考 <修改要求>：结合本条或引用消息中的图片生成\n"
            "/n5 原始 <Prompt>：跳过提示词优化\n"
            "发送 /n5 help 查看完整帮助。",
        )
    ]


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
            [PlannedIdentity(
                source_name="卡缇希娅",
                work="Wuthering Waves",
                role="outfit_source",
                immutable_prompt=(
                    "cartethyia (wuthering waves), girl, long blonde hair"
                ),
                verified=True,
                canonical_tag="cartethyia (wuthering waves)",
            )],
            [],
        )

    monkeypatch.setattr(MODULE, "plan_identities", fake_plan_identities)
    event = FakeEvent()

    description, replacements, warnings, reference_context = (
        await plugin._resolve_planned_character_slots(
            event,
            "阿米娅穿着卡缇希娅的衣服",
            [],
            MODULE.RequestImageContext((), "", ""),
        )
    )

    assert replacements == []
    assert warnings == []
    assert reference_context == ""
    assert "cartethyia (wuthering waves)" in description
    assert "not an additional visible character" in description
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["aliases"][
        identity_alias_key("卡缇希娅", "Wuthering Waves")
    ] == "cartethyia (wuthering waves)"


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

    description, replacements, warnings, reference_context = (
        await plugin._resolve_planned_character_slots(
            FakeEvent(),
            "让卡提希娅打出虚式茈",
            [],
            MODULE.RequestImageContext((), "", ""),
        )
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
    assert results == [("image", "generated.png")]
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
            "character_prompts": {
                "__NAI_CHARACTER_SLOT_1__": "white dress, standing"
            },
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
        "2girls, hugging, outdoors, spring",
        (832, 1216),
        (
            "girl, red hair, blue eyes, mutual#hug",
            "girl, blue hair, green eyes, mutual#hug",
        ),
        "",
        ("extra fingers", "bad eyes"),
    )
    assert results == [("image", "generated.png")]


@pytest.mark.asyncio
async def test_single_nude_character_adds_solo_rating_and_duplicate_guards() -> None:
    """Keep an explicit single-character request singular and content-rated."""
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
            "character_prompts": {
                "__NAI_CHARACTER_SLOT_1__": "standing, relaxed pose"
            },
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
        "1girl, solo, nude, rating:explicit, full body, white background",
        (832, 1216),
        (
            "girl, cartethyia (wuthering waves), blonde hair, nude, standing, relaxed pose",
        ),
        "multiple girls, multiple boys, multiple views, character sheet, lineup, duplicate",
        ("",),
    )
    assert results == [("image", "generated.png")]


@pytest.mark.asyncio
async def test_redraw_reuses_native_character_captions() -> None:
    """Keep both base and character prompts unchanged when redrawing."""
    plugin = build_plugin()
    character_prompts = (
        "girl, red hair, mutual#hug",
        "girl, blue hair, mutual#hug",
    )
    plugin._last_successful_prompt = AsyncMock(
        return_value=(
            "2girls, hugging, spring",
            character_prompts,
            "lowres",
            ("extra fingers", "bad eyes"),
        ),
    )
    event = FakeEvent()

    results = [result async for result in plugin.generate_image(event, "重抽")]

    plugin._generate_from_api.assert_awaited_once_with(
        "2girls, hugging, spring",
        (832, 1216),
        character_prompts,
        "lowres",
        ("extra fingers", "bad eyes"),
    )
    plugin._remember_last_prompt.assert_awaited_once_with(
        event,
        "2girls, hugging, spring",
        character_prompts,
        "lowres",
        ("extra fingers", "bad eyes"),
    )
    assert results == [("image", "generated.png")]


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
async def test_user_negative_prompt_is_scoped_by_user_and_group(
    tmp_path: Path,
) -> None:
    """Keep each QQ user's negative prompt isolated per conversation."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {}
    plugin._artist_state_lock = asyncio.Lock()
    plugin._artist_state_path = Mock(return_value=tmp_path / "artist_strings.json")
    first_group = CharacterEvent()
    second_group = CharacterEvent(group_id="20002")
    other_user = CharacterEvent(sender_id="10002")

    assert await plugin._user_negative_prompt(first_group) == ""
    assert (
        await plugin._user_negative_prompt(
            first_group,
            " lowres,  extra fingers, ",
        )
        == "lowres, extra fingers"
    )

    assert await plugin._user_negative_prompt(first_group) == "lowres, extra fingers"
    assert await plugin._user_negative_prompt(second_group) == ""
    assert await plugin._user_negative_prompt(other_user) == ""
    assert await plugin._user_negative_prompt(first_group, "") == ""


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

    assert rules["model"] == MODULE.NOVELAI_MODEL
    assert rules["rules"]
    assert all(rule["sources"] for rule in rules["rules"])
    assert all(set(rule["sources"]) <= source_ids for rule in rules["rules"])
    assert all(
        rule["enforcement"] in {"deterministic", "llm", "soft"}
        for rule in rules["rules"]
    )
    assert preferences["priority"].startswith("Local preferences apply only after")


@pytest.mark.asyncio
async def test_v5_payload_uses_protocol_v4_and_explicit_style_controls() -> None:
    """Send the V5 model fields without hidden quality or UC presets."""
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

    result = await plugin._generate_from_api("1girl, solo", (832, 1216))

    assert result == Path("generated.png")
    assert client.payload["model"] == "nai-diffusion-5-curated"
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
    plugin._user_generation_size = AsyncMock(return_value=(832, 1216))
    plugin._active_artist_string = AsyncMock(return_value=("千代noob", "artist:test"))
    plugin._user_negative_prompt = AsyncMock(return_value="")
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
    assert f"绘图模型: {MODULE.NOVELAI_MODEL}" in status
    assert "当前画风: 千代noob" in status
    plugin._read_subscription.assert_awaited_once()
