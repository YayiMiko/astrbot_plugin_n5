"""Resolve request-scoped image context for NovelAI prompt planning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image as PillowImage

from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import Image, Reply
from astrbot.core.utils.media_utils import MediaResolver
from astrbot.core.utils.quoted_message import extract_quoted_message_images


@dataclass(frozen=True, slots=True)
class RequestImageContext:
    """Hold images and trusted NovelAI metadata for one request.

    Args:
        image_urls: Local paths or URLs accepted by the multimodal provider.
        metadata_prompt: Prompt facts recovered from NovelAI PNG metadata.
        source: Selected input source, either direct or quoted.
    """

    image_urls: tuple[str, ...]
    metadata_prompt: str
    source: str


async def resolve_request_images(event: AstrMessageEvent) -> RequestImageContext:
    """Resolve direct images before quoted images without global fallback.

    Args:
        event: Current message event.

    Returns:
        Request-local image references and any embedded NovelAI prompt metadata.
    """
    direct_components = [
        component
        for component in event.message_obj.message
        if isinstance(component, Image)
    ]
    source = "direct" if direct_components else ""
    image_urls: list[str] = []
    for component in direct_components:
        try:
            image_urls.append(await component.convert_to_file_path())
        except (OSError, ValueError):
            continue

    if not image_urls and any(
        isinstance(component, Reply) for component in event.message_obj.message
    ):
        image_urls = await extract_quoted_message_images(event)
        source = "quoted" if image_urls else ""

    metadata_parts: list[str] = []
    for image_ref in image_urls[:4]:
        try:
            local_path = image_ref
            if not Path(local_path).is_file():
                local_path = await MediaResolver(
                    image_ref,
                    media_type="image",
                    default_suffix=".png",
                ).to_path()
            with PillowImage.open(local_path) as image:
                info: dict[str, Any] = dict(image.info)
        except (OSError, ValueError):
            continue

        description = info.get("Description") or info.get("description")
        if isinstance(description, str) and description.strip():
            metadata_parts.append(description.strip())
        comment = info.get("Comment") or info.get("comment")
        if not isinstance(comment, str) or not comment.strip():
            continue
        try:
            parsed_comment = json.loads(comment)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed_comment, dict):
            continue
        for key in ("prompt", "input", "caption"):
            value = parsed_comment.get(key)
            if isinstance(value, str) and value.strip():
                metadata_parts.append(value.strip())
        characters = parsed_comment.get("characters")
        if isinstance(characters, list):
            for character in characters:
                if not isinstance(character, dict):
                    continue
                value = character.get("prompt") or character.get("char_caption")
                if isinstance(value, str) and value.strip():
                    metadata_parts.append(value.strip())

    metadata_prompt = "\n".join(dict.fromkeys(metadata_parts))[:12_000]
    return RequestImageContext(tuple(image_urls[:4]), metadata_prompt, source)
