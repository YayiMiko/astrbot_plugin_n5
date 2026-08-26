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

STORYBOARD_JSON = json.dumps(
    {
        "page_layout": (
            "vertical page; Panel 1 wide top; Panel 2 small middle-left; "
            "Panel 3 small middle-right; Panel 4 wide bottom"
        ),
        "reading_order": "Panel 1 -> Panel 2 -> Panel 3 -> Panel 4",
        "visual_continuity": "same clothes, location, props, and light",
        "panels": [
            {
                "panel": number,
                "beat": f"beat {number}",
                "shot": "medium shot",
                "camera": "eye level",
                "composition": "clear subject and prop placement",
                "characters": [],
                "action": f"visible action {number}",
                "state_change": f"visible change {number}",
                "text_elements": [],
            }
            for number in range(1, 5)
        ],
    },
    ensure_ascii=False,
    separators=(",", ":"),
)


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
    plugin._user_generation_size = AsyncMock(return_value=(832, 1216))
    plugin._user_image_model = AsyncMock(return_value=MODULE.NOVELAI_MODEL)
    plugin._user_negative_prompt = AsyncMock(return_value="")
    plugin._join_generation_queue = AsyncMock(return_value=2)
    plugin._leave_generation_queue = AsyncMock()
    plugin._plan_prompt = AsyncMock(
        return_value={"prompt": planned_prompt, "character_prompts": {}},
    )
    plugin._plan_comic_storyboard = AsyncMock(return_value=STORYBOARD_JSON)
    plugin._restore_character_slots = Mock(side_effect=lambda prompt, _items: prompt)
    plugin._generate_from_api = AsyncMock(return_value=Path("generated.png"))
    plugin._remember_last_prompt = AsyncMock()
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
    plugin._plan_comic_storyboard.assert_not_awaited()
    plugin._generate_from_api.assert_awaited_once_with(
        f"nsfw, {prompt}",
        (832, 1216),
        (),
        "",
        (),
        image_model=MODULE.NOVELAI_MODEL,
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
    plugin._plan_comic_storyboard.assert_not_awaited()
    plugin._generate_from_api.assert_awaited_once_with(
        "nsfw, 1girl, eating ice cream, happy",
        (832, 1216),
        (),
        "",
        (),
        image_model=MODULE.NOVELAI_MODEL,
    )
    assert results == []


@pytest.mark.asyncio
async def test_comic_mode_keeps_repeated_character_and_panel_concepts() -> None:
    """Use comic planning without applying single-image duplicate guards."""
    plugin = build_plugin()
    replacements = [
        (
            "__NAI_CHARACTER_SLOT_1__",
            "狐莉",
            "girl, white hair, fox ears, fox tail",
            "bad ears",
        )
    ]
    plugin._resolve_character_slots = AsyncMock(
        return_value=(
            "__NAI_CHARACTER_SLOT_1__起床后发现尾巴缠在毯子里",
            replacements,
        )
    )
    plugin._plan_prompt = AsyncMock(
        return_value={
            "prompt": (
                "comic, 4koma, vertical four-panel page. Panel 1 shows the girl "
                "waking up. Panel 2 shows her tail tangled in a blanket."
            ),
            "character_prompts": {
                "__NAI_CHARACTER_SLOT_1__": (
                    "same pajamas in every panel, sleepy, surprised, struggling"
                )
            },
        }
    )
    plugin._user_negative_prompt = AsyncMock(
        return_value="multiple views, duplicate, panels, lowres"
    )

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(),
            "漫画 狐莉起床后发现尾巴缠在毯子里",
        )
    ]

    assert plugin._plan_prompt.await_args.kwargs["comic_mode"] is True
    plugin._generate_from_api.assert_awaited_once_with(
        (
            "nsfw, no text, comic, 4koma, vertical four-panel page. Panel 1 shows the girl "
            "waking up. Panel 2 shows her tail tangled in a blanket."
        ),
        (832, 1216),
        (
            "girl, white hair, fox ears, fox tail, same pajamas in every panel, "
            "sleepy, surprised, struggling",
        ),
        "lowres, text, captions, speech bubbles, subtitles, watermark, signature",
        ("bad ears",),
        image_model=MODULE.NOVELAI_MODEL,
    )
    assert results == []


@pytest.mark.asyncio
async def test_comic_draw_mode_invents_story_for_multiple_saved_characters() -> None:
    """Route only the requested cast into creative four-panel planning."""
    plugin = build_plugin()
    replacements = [
        (
            "__NAI_CHARACTER_SLOT_1__",
            "狐莉",
            "girl, white hair, fox ears, fox tail",
            "bad ears",
        ),
        (
            "__NAI_CHARACTER_SLOT_2__",
            "鲸鱼娘",
            "girl, blue hair, whale hair ornament",
            "bad ornament",
        ),
    ]
    plugin._resolve_character_slots = AsyncMock(
        return_value=(
            "__NAI_CHARACTER_SLOT_1__和__NAI_CHARACTER_SLOT_2__",
            replacements,
        )
    )
    plugin._plan_prompt = AsyncMock(
        return_value={
            "prompt": (
                "comic, 4koma, vertical four-panel page. Panel 1 establishes a "
                "seaside picnic. Panel 2 introduces a runaway lunchbox. Panel 3 "
                "shows both girls chasing it. Panel 4 ends with a tiny crab inside."
            ),
            "character_prompts": {
                "__NAI_CHARACTER_SLOT_1__": (
                    "summer dress, Panel 1 smiling, Panel 2 surprised, "
                    "Panel 3 running, Panel 4 laughing"
                ),
                "__NAI_CHARACTER_SLOT_2__": (
                    "sailor dress, Panel 1 arranging food, Panel 2 pointing, "
                    "Panel 3 running, Panel 4 holding the crab"
                ),
            },
        }
    )

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(),
            "漫画抽卡 狐莉和鲸鱼娘",
        )
    ]

    assert plugin._plan_prompt.await_args.kwargs == {
        "comic_mode": True,
        "comic_draw_mode": True,
        "comic_draw_plot_seed": "",
        "comic_storyboard": STORYBOARD_JSON,
        "comic_text_allowed": True,
    }
    assert plugin._plan_comic_storyboard.await_args.kwargs == {
        "comic_draw_mode": True,
        "comic_draw_plot_seed": "",
        "comic_text_allowed": True,
    }
    assert plugin._plan_comic_storyboard.await_args.args[1] == (
        "__NAI_CHARACTER_SLOT_1__",
        "__NAI_CHARACTER_SLOT_2__",
    )
    generated_call = plugin._generate_from_api.await_args
    assert "Panel 1" in generated_call.args[0]
    assert "Panel 4" in generated_call.args[0]
    assert len(generated_call.args[2]) == 2
    assert "solo" not in generated_call.args[0]
    assert results == []


@pytest.mark.asyncio
async def test_comic_draw_mode_extracts_user_plot_after_cast() -> None:
    """Pass a user event after the cast as a hard comic plot seed."""
    plugin = build_plugin()
    replacements = [
        (
            "__NAI_CHARACTER_SLOT_1__",
            "空",
            "boy, aether (genshin impact), blonde hair",
            "",
        ),
        (
            "__NAI_CHARACTER_SLOT_2__",
            "荧",
            "girl, lumine (genshin impact), blonde hair",
            "",
        ),
    ]
    plugin._resolve_character_slots = AsyncMock(
        return_value=(
            "__NAI_CHARACTER_SLOT_1__和__NAI_CHARACTER_SLOT_2__，抢夺包子",
            replacements,
        )
    )
    plugin._plan_prompt = AsyncMock(
        return_value={
            "prompt": (
                "comic, 4koma. Panel 1 shows one steamed bun between the twins. "
                "Panel 2 shows both reaching for it. Panel 3 shows a fast tug-of-war. "
                "Panel 4 reveals the bun split evenly in their hands."
            ),
            "character_prompts": {
                "__NAI_CHARACTER_SLOT_1__": "Panel 1 watching the bun, Panel 2 reaching, Panel 3 pulling, Panel 4 smiling",
                "__NAI_CHARACTER_SLOT_2__": "Panel 1 holding a plate, Panel 2 reaching, Panel 3 pulling, Panel 4 smiling",
            },
        }
    )

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(),
            "漫画抽卡 原神空和荧，抢夺包子",
        )
    ]

    assert plugin._plan_prompt.await_args.kwargs["comic_draw_plot_seed"] == "抢夺包子"
    assert plugin._plan_comic_storyboard.await_args.kwargs[
        "comic_draw_plot_seed"
    ] == "抢夺包子"
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
            "/n5 漫画 <剧情>：规划并生成完整的多格漫画页\n"
            "/n5 漫画抽卡 <角色>[，剧情]：随机创作或扩写指定剧情\n"
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
async def test_comic_planner_receives_page_and_text_block_rules() -> None:
    """Inject comic-only page layout and API text instructions into planning."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "prompt_planner_enabled": True,
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
    }
    storyboard_payload = json.loads(STORYBOARD_JSON)
    storyboard_payload["panels"][0]["text_elements"] = [
        {
            "kind": "dialogue",
            "content": "Good morning!",
            "speaker": "fox girl",
            "placement": "speech bubble above her head",
            "style": "small handwritten black text in a white bubble",
        }
    ]
    comic_storyboard = json.dumps(
        storyboard_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    response = Mock(
        completion_text=json.dumps(
            {
                "ok": True,
                "prompt": (
                    "comic, 4koma, reading order Panel 1 to Panel 2 to Panel 3 "
                    'to Panel 4. Panel 1 shows a fox girl with a white speech '
                    'bubble saying "Good morning!". Panel 2 shows breakfast. '
                    "Panel 3 shows a rush. Panel 4 shows her leaving.\n"
                    "Text: Good morning!"
                ),
                "character_prompts": {},
                "error": None,
            }
        )
    )
    plugin.context = Mock()
    plugin.context.llm_generate = AsyncMock(return_value=response)

    plan = await plugin._plan_prompt(
        "画一页狐娘起床的四格漫画",
        4000,
        comic_mode=True,
        comic_storyboard=comic_storyboard,
    )

    system_prompt = plugin.context.llm_generate.await_args.kwargs["system_prompt"]
    assert "本次请求使用 NovelAI V5 漫画模式" in system_prompt
    assert "唯一的 `Text:` 块" in system_prompt
    assert "不得加入 `solo`" in system_prompt
    planner_prompt = plugin.context.llm_generate.await_args.kwargs["prompt"]
    assert "[COMIC_STORYBOARD_BEGIN]" in planner_prompt
    assert '"content":"Good morning!"' in planner_prompt
    assert "Text:" not in plan["prompt"]
    assert plan["prompt"].startswith("comic, 4koma")
    assert plan["prompt"].endswith("Panel 4 shows her leaving.")
    assert plugin.context.llm_generate.await_count == 1


@pytest.mark.asyncio
async def test_visual_only_comic_planner_retries_invented_rendered_text() -> None:
    """Retry when final comic planning invents captions the user did not request."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "prompt_planner_enabled": True,
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
    }
    with_text = Mock(
        completion_text=json.dumps(
            {
                "ok": True,
                "prompt": (
                    "comic, 4koma. Panel 1 Text: Start. Panel 2 action. "
                    "Panel 3 reaction. Panel 4 result."
                ),
                "character_prompts": {},
                "error": None,
            }
        )
    )
    visual_only = Mock(
        completion_text=json.dumps(
            {
                "ok": True,
                "prompt": (
                    "comic, 4koma. Panel 1 setup. Panel 2 action. "
                    "Panel 3 reaction. Panel 4 visual result."
                ),
                "character_prompts": {},
                "error": None,
            }
        )
    )
    plugin.context = Mock()
    plugin.context.llm_generate = AsyncMock(side_effect=[with_text, visual_only])

    plan = await plugin._plan_prompt(
        "无对白的四格漫画",
        4000,
        comic_mode=True,
        comic_storyboard=STORYBOARD_JSON,
        comic_text_allowed=False,
    )

    assert plugin.context.llm_generate.await_count == 2
    system_prompt = plugin.context.llm_generate.await_args_list[0].kwargs[
        "system_prompt"
    ]
    assert "本次用户明确要求纯画面" in system_prompt
    assert "Text:" not in plan["prompt"]


def test_storyboard_parser_validates_sequential_panels_and_cast() -> None:
    """Accept a complete storyboard with only the request-scoped cast."""
    slot = "__NAI_CHARACTER_SLOT_1__"
    payload = json.loads(STORYBOARD_JSON)
    for panel in payload["panels"]:
        panel["characters"] = [slot]
    payload["ok"] = True

    storyboard = MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(
        json.dumps(payload, ensure_ascii=False),
        (slot,),
        exact_four_panels=True,
    )

    assert len(storyboard["panels"]) == 4
    assert storyboard["panels"][0]["characters"] == [slot]


def test_storyboard_parser_rejects_foreign_cast_slot() -> None:
    """Reject a storyboard that silently introduces another identity."""
    payload = json.loads(STORYBOARD_JSON)
    payload["ok"] = True
    payload["panels"][0]["characters"] = ["__NAI_CHARACTER_SLOT_2__"]

    with pytest.raises(MODULE.NovelAIWebError, match="请求之外"):
        MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(
            json.dumps(payload, ensure_ascii=False),
            ("__NAI_CHARACTER_SLOT_1__",),
            exact_four_panels=True,
        )


def test_storyboard_parser_enforces_structured_text_and_full_cast_panels() -> None:
    """Validate structured text while rejecting text in visual-only mode."""
    slots = ("__NAI_CHARACTER_SLOT_1__", "__NAI_CHARACTER_SLOT_2__")
    payload = json.loads(STORYBOARD_JSON)
    payload["ok"] = True
    for panel in payload["panels"]:
        panel["characters"] = list(slots)
        panel["shot"] = "medium two-shot"

    storyboard = MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(
        json.dumps(payload, ensure_ascii=False),
        slots,
        exact_four_panels=True,
        require_full_cast_each_panel=True,
    )
    assert storyboard["page_layout"] == (
        "vertical page; Panel 1 wide top; Panel 2 small middle-left; "
        "Panel 3 small middle-right; Panel 4 wide bottom"
    )
    assert storyboard["reading_order"] == (
        "Panel 1 -> Panel 2 -> Panel 3 -> Panel 4"
    )

    payload["page_layout"] = "vertical four-panel page"
    with pytest.raises(MODULE.NovelAIWebError, match="逐格说明"):
        MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(
            json.dumps(payload, ensure_ascii=False),
            slots,
            exact_four_panels=True,
            require_full_cast_each_panel=True,
        )
    payload["page_layout"] = storyboard["page_layout"]

    payload["panels"][1]["text_elements"] = [
        {
            "kind": "dialogue",
            "content": "快一点！",
            "speaker": slots[0],
            "placement": "speech bubble above the first character",
            "style": "bold black text in a white bubble",
        }
    ]
    payload["panels"][0]["text_elements"] = [
        {
            "kind": "title",
            "content": "雪人大战",
            "speaker": "",
            "placement": "page top",
            "style": "large bold display lettering",
        }
    ]
    payload["panels"][2]["text_elements"] = [
        {
            "kind": "sfx",
            "content": "砰！",
            "speaker": "",
            "placement": "beside the collapsing snowman",
            "style": "large jagged lettering",
        }
    ]
    payload["panels"][3]["text_elements"] = [
        {
            "kind": "narration",
            "content": "胜负已分",
            "speaker": "",
            "placement": "lower-right corner",
            "style": "small neat text in a pale box",
        }
    ]
    with pytest.raises(MODULE.NovelAIWebError, match="明确要求纯画面"):
        MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(
            json.dumps(payload, ensure_ascii=False),
            slots,
            exact_four_panels=True,
            allow_rendered_text=False,
            require_full_cast_each_panel=True,
        )

    accepted_with_text = MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(
        json.dumps(payload, ensure_ascii=False),
        slots,
        exact_four_panels=True,
        require_full_cast_each_panel=True,
    )
    assert accepted_with_text["panels"][1]["text_elements"][0]["kind"] == (
        "dialogue"
    )
    assert [
        panel["text_elements"][0]["kind"]
        for panel in accepted_with_text["panels"]
    ] == ["title", "dialogue", "sfx", "narration"]

    payload["panels"][1]["text_elements"][0]["content"] = (
        "像这样蹲低是不是就可以让它慢慢靠近我了"
    )
    with pytest.raises(MODULE.NovelAIWebError, match="漫画文字过长"):
        MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(
            json.dumps(payload, ensure_ascii=False),
            slots,
            exact_four_panels=True,
            require_full_cast_each_panel=True,
        )
    payload["panels"][1]["text_elements"][0]["content"] = "快一点！"

    payload["panels"][1]["text_elements"] = []
    payload["panels"][2]["shot"] = "close-up"
    with pytest.raises(MODULE.NovelAIWebError, match="景别不足"):
        MODULE.NovelAIWebPlugin._parse_comic_storyboard_response(
            json.dumps(payload, ensure_ascii=False),
            slots,
            exact_four_panels=True,
            require_full_cast_each_panel=True,
        )


@pytest.mark.asyncio
async def test_comic_text_is_appended_as_the_final_api_prompt_block() -> None:
    """Append structured visible text only after every visual prompt instruction."""
    plugin = build_plugin(
        'comic, 4koma, Panel 1 page title "雪人大战". '
        'Panel 2 speech bubble "快一点！". Panel 3 impact sfx "砰！". '
        'Panel 4 narration box "胜负已分".'
    )
    storyboard_payload = json.loads(STORYBOARD_JSON)
    storyboard_payload["panels"][0]["text_elements"] = [
        {
            "kind": "title",
            "content": "雪人大战",
            "speaker": "",
            "placement": "page top",
            "style": "large bold blue display text",
        }
    ]
    storyboard_payload["panels"][1]["text_elements"] = [
        {
            "kind": "dialogue",
            "content": "快一点！",
            "speaker": "fox girl",
            "placement": "speech bubble above her head",
            "style": "black handwritten text in a white bubble",
        }
    ]
    storyboard_payload["panels"][2]["text_elements"] = [
        {
            "kind": "sfx",
            "content": "砰！",
            "speaker": "",
            "placement": "beside the collapsing snowman",
            "style": "large jagged blue sound-effect lettering",
        }
    ]
    storyboard_payload["panels"][3]["text_elements"] = [
        {
            "kind": "narration",
            "content": "胜负已分",
            "speaker": "",
            "placement": "small box in the lower-right corner",
            "style": "neat black text in a pale rectangular box",
        }
    ]
    plugin._plan_comic_storyboard = AsyncMock(
        return_value=json.dumps(
            storyboard_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    results = [
        result
        async for result in plugin.generate_image(
            FakeEvent(),
            "漫画 狐娘醒来问候朋友",
        )
    ]

    assert plugin._plan_prompt.await_args.kwargs["comic_text_allowed"] is True
    assert plugin._plan_comic_storyboard.await_args.kwargs[
        "comic_text_allowed"
    ] is True
    generated_call = plugin._generate_from_api.await_args
    assert "no text" not in generated_call.args[0]
    assert generated_call.args[0].startswith("text, chinese text, nsfw")
    assert generated_call.args[0].endswith(
        "Text: 雪人大战\n\n快一点！\n\n砰！\n\n胜负已分"
    )
    assert generated_call.args[0].count("Text:") == 1
    assert generated_call.args[3] == ""
    assert results == []


@pytest.mark.asyncio
async def test_comic_storyboard_retries_then_returns_compact_json() -> None:
    """Retry an incomplete draw storyboard before prompt translation."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
    }
    slot = "__NAI_CHARACTER_SLOT_1__"
    complete = json.loads(STORYBOARD_JSON)
    complete["ok"] = True
    for panel in complete["panels"]:
        panel["characters"] = [slot]
    plugin.context = Mock()
    plugin.context.llm_generate = AsyncMock(
        side_effect=[
            Mock(completion_text='{"ok":true,"panels":[]}'),
            Mock(completion_text=json.dumps(complete, ensure_ascii=False)),
        ]
    )

    storyboard = await plugin._plan_comic_storyboard(
        slot,
        (slot,),
        comic_draw_mode=True,
        comic_draw_plot_seed="抢夺包子",
    )

    assert plugin.context.llm_generate.await_count == 2
    assert all(
        call.kwargs["temperature"] == 0.7
        for call in plugin.context.llm_generate.await_args_list
    )
    assert "抢夺包子" in plugin.context.llm_generate.await_args_list[0].kwargs["prompt"]
    assert (
        "[TEXT_POLICY]\nALLOW_STORY_TEXT\n[/TEXT_POLICY]"
        in plugin.context.llm_generate.await_args_list[0].kwargs["prompt"]
    )
    assert "上一次分镜无效" in plugin.context.llm_generate.await_args_list[1].kwargs[
        "prompt"
    ]
    assert json.loads(storyboard)["panels"][3]["panel"] == 4


@pytest.mark.parametrize(
    ("prompt_text", "forbids_text"),
    [
        ("漫画抽卡 狐莉和鲸鱼娘", False),
        ("漫画抽卡 狐莉和鲸鱼娘，无对白", True),
        ("漫画抽卡 狐莉和鲸鱼娘，纯画面", True),
        ("漫画抽卡 狐莉和鲸鱼娘，no text", True),
    ],
)
def test_comic_text_is_disabled_only_by_an_explicit_visual_only_request(
    prompt_text: str,
    forbids_text: bool,
) -> None:
    """Keep useful story text unless the user explicitly requests no text."""
    assert bool(MODULE.COMIC_TEXT_FORBID_PATTERN.search(prompt_text)) is forbids_text


@pytest.mark.asyncio
async def test_comic_draw_planner_retries_incomplete_panel_plan() -> None:
    """Reject short comic prompts until all four panels and cast roles are explicit."""
    plugin = MODULE.NovelAIWebPlugin.__new__(MODULE.NovelAIWebPlugin)
    plugin.config = {
        "prompt_planner_enabled": True,
        "prompt_planner_provider_id": "deepseek/deepseek-v4-flash-vision-exp",
    }
    incomplete = Mock(
        completion_text=(
            '{"ok":true,"prompt":"comic, two girls having a picnic",'
            '"character_prompts":{"__NAI_CHARACTER_SLOT_1__":"summer dress"},'
            '"error":null}'
        )
    )
    complete = Mock(
        completion_text=(
            '{"ok":true,"prompt":"comic, 4koma. Panel 1 starts a picnic. '
            'Panel 2 shows a rolling lunchbox. Panel 3 shows a chase. Panel 4 '
            'reveals a crab.","character_prompts":{"__NAI_CHARACTER_SLOT_1__":'
            '"summer dress, Panel 1 smiling, Panel 2 surprised, Panel 3 running, '
            'Panel 4 laughing"},"error":null}'
        )
    )
    plugin.context = Mock()
    plugin.context.llm_generate = AsyncMock(side_effect=[incomplete, complete])

    plan = await plugin._plan_prompt(
        "__NAI_CHARACTER_SLOT_1__",
        4000,
        ("__NAI_CHARACTER_SLOT_1__",),
        comic_mode=True,
        comic_draw_mode=True,
        comic_draw_plot_seed="抢夺包子",
    )

    assert plugin.context.llm_generate.await_count == 2
    assert all(
        call.kwargs["temperature"] == 0.7
        for call in plugin.context.llm_generate.await_args_list
    )
    system_prompt = plugin.context.llm_generate.await_args_list[0].kwargs[
        "system_prompt"
    ]
    assert "本次请求是“漫画抽卡”" in system_prompt
    assert "Panel 4" in plan["prompt"]
    assert "4koma" not in plan["prompt"]
    assert "four-panel comic page" in plan["prompt"]
    first_prompt = plugin.context.llm_generate.await_args_list[0].kwargs["prompt"]
    assert "[COMIC_DRAW_PLOT_SEED]" in first_prompt
    assert "抢夺包子" in first_prompt
    assert "只能扩写" in first_prompt
    retry_prompt = plugin.context.llm_generate.await_args_list[1].kwargs["prompt"]
    assert "Panel 1 至 Panel 4" in retry_prompt
    assert "抢夺包子" in retry_prompt


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
    )
    assert results == []


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
        "nsfw, 2girls, hugging, spring",
        (832, 1216),
        character_prompts,
        "lowres",
        ("extra fingers", "bad eyes"),
        image_model=MODULE.NOVELAI_MODEL,
    )
    plugin._remember_last_prompt.assert_awaited_once_with(
        event,
        "nsfw, 2girls, hugging, spring",
        character_prompts,
        "lowres",
        ("extra fingers", "bad eyes"),
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
        "图片发送回执超时，可能已经送达；如果没有看到，发送 /n5 重发，"
        "不会重新消耗 NAI 点数。",
    )


@pytest.mark.asyncio
async def test_resend_uses_latest_scoped_output_without_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resend the latest surviving output without another NovelAI request."""
    plugin = build_plugin()
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    output_path = output_dir / "latest.png"
    output_path.write_bytes(b"png")
    monkeypatch.setattr(
        MODULE.star.StarTools,
        "get_data_dir",
        lambda _name: tmp_path,
    )
    plugin._last_delivery_task = AsyncMock(
        return_value={
            "task_id": "task-existing",
            "created_at": "2026-08-25T22:00:00+09:00",
            "sender_id": "10001",
            "conversation": "group:20001",
            "group_id": "20001",
            "output_path": str(output_path),
            "generated": True,
            "delivery_status": "send_failed_after_retry",
            "retry_count": 1,
            "error": "timeout",
        }
    )
    plugin._deliver_generated_image = AsyncMock()
    event = FakeEvent()

    results = [result async for result in plugin.generate_image(event, "重发")]

    assert results == []
    plugin._generate_from_api.assert_not_awaited()
    plugin._deliver_generated_image.assert_awaited_once_with(
        event,
        output_path.resolve(),
        task_id="task-existing",
        retry_count=2,
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
    prompt = (
        "comic, Panel 1 action, rating:general\n"
        "Text: Hello, world!\n\n砰！"
    )

    normalized = MODULE.NovelAIWebPlugin._apply_global_nsfw_prompt(prompt)

    assert normalized == (
        "nsfw, comic, Panel 1 action\nText: Hello, world!\n\n砰！"
    )
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
    plugin._user_generation_size = AsyncMock(return_value=(832, 1216))
    plugin._user_image_model = AsyncMock(return_value=MODULE.NOVELAI_MODELS["v5f"])
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
    assert "绘图模型: V5F（Full）" in status
    assert "当前画风: 千代noob" in status
    plugin._read_subscription.assert_awaited_once()
