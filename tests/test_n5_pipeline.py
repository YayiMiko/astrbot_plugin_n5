"""Tests for request-scoped images and NovelAI-native identity resolution."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image as PillowImage
from PIL.PngImagePlugin import PngInfo

from astrbot.core.message.components import Image

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from identity_planner import identity_alias_key, plan_identities  # noqa: E402
from image_context import resolve_request_images  # noqa: E402
from novelai_tags import NovelAITagResolver  # noqa: E402


class ImageEvent:
    """Expose one direct image component to the resolver."""

    def __init__(self, path: Path) -> None:
        """Build the event.

        Args:
            path: Local test image path.
        """
        self.message_obj = SimpleNamespace(
            message=[Image.fromFileSystem(path)],
        )


@pytest.mark.asyncio
async def test_request_image_metadata_is_local_and_request_scoped(tmp_path: Path) -> None:
    """Read direct NovelAI metadata without consulting a latest-image file."""
    image_path = tmp_path / "novelai.png"
    metadata = PngInfo()
    metadata.add_text("Description", "mornye (wuthering waves), silver hair")
    metadata.add_text("Comment", json.dumps({"prompt": "night station"}))
    PillowImage.new("RGB", (8, 8), "white").save(image_path, pnginfo=metadata)

    result = await resolve_request_images(ImageEvent(image_path))

    assert result.source == "direct"
    assert result.image_urls == (str(image_path.resolve()),)
    assert "mornye (wuthering waves)" in result.metadata_prompt
    assert "night station" in result.metadata_prompt


@pytest.mark.asyncio
async def test_official_suggestions_require_an_exact_normalized_match() -> None:
    """Use NovelAI's canonical spelling and reject merely similar tags."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["model"] == "nai-diffusion-5-curated"
        return httpx.Response(
            200,
            json={
                "tags": [
                    {"tag": "mornye (wuthering waves)", "confidence": 0.0},
                    {"tag": "morgiana", "confidence": 0.67},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = NovelAITagResolver(
            client,
            "https://image.novelai.net",
            "nai-diffusion-5-curated",
        )
        result = await resolver.resolve(
            "莫宁",
            ["mornye_(wuthering_waves)"],
        )

    assert result.canonical_tag == "mornye (wuthering waves)"


@pytest.mark.asyncio
async def test_identity_planner_locks_verified_tag_and_appearance() -> None:
    """Keep the official identity tag outside the scene planner's control."""

    class FakeContext:
        async def llm_generate(self, **kwargs):
            assert kwargs["image_urls"] == ["reference.png"]
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "characters": [
                            {
                                "source_name": "莫宁",
                                "work": "wuthering waves",
                                "candidate_tags": [
                                    "mornye (wuthering waves)"
                                ],
                                "appearance": "girl, silver hair, blue eyes",
                            }
                        ]
                    }
                )
            )

    class FakeResolver:
        async def resolve(self, source_name: str, candidates: list[str]):
            from novelai_tags import TagResolution

            return TagResolution(
                source_name,
                "mornye (wuthering waves)",
                candidates[0],
            )

    identities = await plan_identities(
        FakeContext(),
        "deepseek/deepseek-v4-flash-vision-exp",
        "莫宁站在雪夜车站",
        ("reference.png",),
        "",
        FakeResolver(),
    )

    assert identities[0].verified is True
    assert identities[0].immutable_prompt == (
        "mornye (wuthering waves), girl, silver hair, blue eyes"
    )


@pytest.mark.asyncio
async def test_identity_planner_repairs_failed_romanization_with_web_evidence() -> None:
    """Search translated names only after the first NovelAI lookup fails."""

    class FakeToolSet:
        @staticmethod
        def get_tool(name: str):
            assert name == "web_search_tavily"
            return SimpleNamespace(name=name, active=True)

    class FakeToolManager:
        @staticmethod
        def get_full_tool_set():
            return FakeToolSet()

    class FakeContext:
        @staticmethod
        async def llm_generate(**kwargs):
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "characters": [
                            {
                                "source_name": "卡缇希娅",
                                "work": "Wuthering Waves",
                                "role": "outfit_source",
                                "candidate_tags": [
                                    "Catthy (Wuthering Waves)",
                                    "Catthy",
                                ],
                                "appearance": "girl, long blonde hair",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            )

        @staticmethod
        def get_llm_tool_manager():
            return FakeToolManager()

        @staticmethod
        async def tool_loop_agent(**kwargs):
            assert kwargs["max_steps"] == 3
            assert kwargs["tools"].get_tool("web_search_tavily") is not None
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "official_name": "Cartethyia",
                        "work_en": "Wuthering Waves",
                        "candidate_tags": [
                            "cartethyia (wuthering waves)",
                            "cartethyia",
                        ],
                    }
                )
            )

    class FakeResolver:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def resolve(self, source_name: str, candidates: list[str]):
            from novelai_tags import TagResolution

            self.calls.append(candidates)
            if "cartethyia (wuthering waves)" in candidates:
                return TagResolution(
                    source_name,
                    "cartethyia (wuthering waves)",
                    "cartethyia (wuthering waves)",
                )
            return TagResolution(source_name, None, candidates[0])

    resolver = FakeResolver()
    identities = await plan_identities(
        FakeContext(),
        "deepseek/deepseek-v4-flash-vision-exp",
        "明日方舟角色阿米娅穿着鸣潮角色卡缇希娅的衣服",
        (),
        "",
        resolver,
        event=SimpleNamespace(),
    )

    assert len(resolver.calls) == 2
    assert identities[0].verified is True
    assert identities[0].role == "outfit_source"
    assert identities[0].canonical_tag == "cartethyia (wuthering waves)"


@pytest.mark.asyncio
async def test_verified_alias_cache_skips_network_repair() -> None:
    """Trust only a canonical alias that was previously NovelAI-verified."""

    class FakeContext:
        @staticmethod
        async def llm_generate(**kwargs):
            return SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "characters": [
                            {
                                "source_name": "卡缇希娅",
                                "work": "Wuthering Waves",
                                "role": "visible_subject",
                                "candidate_tags": ["Catthy"],
                                "appearance": "girl, long blonde hair",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            )

    class RejectUnexpectedResolver:
        @staticmethod
        async def resolve(source_name: str, candidates: list[str]):
            raise AssertionError("verified cache should skip official lookup")

    canonical = "cartethyia (wuthering waves)"
    identities = await plan_identities(
        FakeContext(),
        "deepseek/deepseek-v4-flash-vision-exp",
        "卡缇希娅",
        (),
        "",
        RejectUnexpectedResolver(),
        verified_aliases={
            identity_alias_key("卡缇希娅", "Wuthering Waves"): canonical
        },
    )

    assert identities[0].verified is True
    assert identities[0].canonical_tag == canonical
