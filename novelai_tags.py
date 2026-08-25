"""Resolve character identities against NovelAI's own tag vocabulary."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

SUGGEST_TAGS_ENDPOINT = "/ai/generate-image/suggest-tags"


@dataclass(frozen=True, slots=True)
class TagResolution:
    """Describe one NovelAI tag lookup result.

    Args:
        source_name: Character name supplied by the user or vision model.
        canonical_tag: Best exact NovelAI vocabulary match, if available.
        candidate: Candidate that produced the match.
    """

    source_name: str
    canonical_tag: str | None
    candidate: str | None


class NovelAITagResolver:
    """Query NovelAI suggestions without treating Danbooru as a veto."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
    ) -> None:
        """Initialize the resolver.

        Args:
            client: Shared asynchronous HTTP client.
            base_url: NovelAI image API base URL.
            model: NovelAI image model identifier.
        """
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._model = model

    @staticmethod
    def _normalize(value: str) -> str:
        """Normalize equivalent NovelAI tag spellings.

        Args:
            value: Raw tag text.

        Returns:
            Case-folded tag with equivalent separators collapsed.
        """
        value = value.replace("_", " ").casefold()
        value = re.sub(r"\s+", " ", value)
        return re.sub(r"\s*([()])\s*", r"\1", value).strip(" ,")

    async def resolve(
        self,
        source_name: str,
        candidates: list[str],
    ) -> TagResolution:
        """Resolve the first exact candidate returned by NovelAI.

        Args:
            source_name: Original character name.
            candidates: Romanized or English candidates from the planner.

        Returns:
            Exact canonical NovelAI tag when found.
        """
        cleaned_candidates = list(
            dict.fromkeys(
                item.strip(" ,")
                for item in candidates
                if isinstance(item, str) and item.strip(" ,")
            )
        )[:8]
        for candidate in cleaned_candidates:
            try:
                response = await self._client.get(
                    f"{self._base_url}{SUGGEST_TAGS_ENDPOINT}",
                    params={"model": self._model, "prompt": candidate},
                    timeout=12,
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError):
                continue
            tags = payload.get("tags", []) if isinstance(payload, dict) else []
            normalized_candidate = self._normalize(candidate)
            for item in tags:
                tag = item.get("tag") if isinstance(item, dict) else None
                if (
                    isinstance(tag, str)
                    and self._normalize(tag) == normalized_candidate
                ):
                    return TagResolution(source_name, tag.strip(), candidate)
        return TagResolution(
            source_name,
            None,
            cleaned_candidates[0] if cleaned_candidates else None,
        )
