"""Plan and lock character identities before scene prompt expansion."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from astrbot.api import star

try:
    from .novelai_tags import NovelAITagResolver
except ImportError:
    from novelai_tags import NovelAITagResolver


IDENTITY_SYSTEM_PROMPT = """You identify named fictional characters for NovelAI Diffusion V5.
Return exactly one JSON object and no Markdown:
{"characters":[{"source_name":"name as written in the request","work":"English work name or empty","candidate_tags":["romanized name (work)"],"appearance":"comma-separated stable visual identity tags"}]}
Rules:
- Include only explicitly named fictional characters or a clearly depicted reference-image subject.
- Never invent a famous identity for a generic girl, occupation, title, or unnamed person.
- Give up to four plausible NovelAI/Danbooru-style candidate tags, strongest first.
- For a named character, appearance must preserve distinctive hair, eyes, anatomy, signature accessories, and other stable identity traits; do not include pose, scene, camera, temporary clothing, artist tags, or quality tags.
- If an attached image has NovelAI metadata, treat metadata identity as more reliable than visual guessing.
- Ignore protected tokens matching __NAI_CHARACTER_SLOT_<number>__ because the plugin already resolved them.
- Use an empty characters list when there is no resolvable character.
"""


@dataclass(frozen=True, slots=True)
class PlannedIdentity:
    """Hold one planner identity and its NovelAI resolution.

    Args:
        source_name: Name that should be replaced in the request.
        immutable_prompt: Canonical tag and stable appearance tags.
        verified: Whether NovelAI returned an exact canonical tag.
    """

    source_name: str
    immutable_prompt: str
    verified: bool


async def plan_identities(
    context: star.Context,
    provider_id: str,
    description: str,
    image_urls: tuple[str, ...],
    metadata_prompt: str,
    resolver: NovelAITagResolver,
) -> list[PlannedIdentity]:
    """Extract identities with DS4F Vision and verify them through NovelAI.

    Args:
        context: AstrBot plugin context.
        provider_id: Multimodal provider identifier.
        description: User request after saved-character slot replacement.
        image_urls: Request-local image inputs.
        metadata_prompt: Prompt metadata extracted from NovelAI PNG files.
        resolver: Official NovelAI tag resolver.

    Returns:
        Ordered immutable identity prompts.
    """
    user_prompt = description
    if metadata_prompt:
        user_prompt += (
            "\n\nNovelAI PNG metadata (trusted before visual inference):\n"
            + metadata_prompt
        )
    response = await context.llm_generate(
        chat_provider_id=provider_id,
        prompt=user_prompt,
        image_urls=list(image_urls),
        system_prompt=IDENTITY_SYSTEM_PROMPT,
        request_max_retries=2,
        temperature=0,
    )
    raw = str(response.completion_text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL | re.I)
    if fenced:
        raw = fenced.group(1)
    payload = json.loads(raw)
    characters = payload.get("characters", []) if isinstance(payload, dict) else []
    if not isinstance(characters, list):
        return []

    identities: list[PlannedIdentity] = []
    for item in characters[:6]:
        if not isinstance(item, dict):
            continue
        source_name = str(item.get("source_name") or "").strip()
        appearance = str(item.get("appearance") or "").strip(" ,")
        raw_candidates = item.get("candidate_tags", [])
        candidates = (
            [str(value).strip() for value in raw_candidates if str(value).strip()]
            if isinstance(raw_candidates, list)
            else []
        )
        if not source_name or CHARACTER_SLOT_PATTERN.search(source_name):
            continue
        resolution = await resolver.resolve(source_name, candidates)
        identity_tag = resolution.canonical_tag or resolution.candidate or source_name
        immutable_prompt = ", ".join(
            value for value in (identity_tag.strip(" ,"), appearance) if value
        )
        if immutable_prompt:
            identities.append(
                PlannedIdentity(
                    source_name=source_name,
                    immutable_prompt=immutable_prompt,
                    verified=resolution.canonical_tag is not None,
                )
            )
    return identities


CHARACTER_SLOT_PATTERN = re.compile(r"__NAI_CHARACTER_SLOT_\d+__", re.IGNORECASE)
