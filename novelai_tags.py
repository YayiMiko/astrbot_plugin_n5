"""Resolve character identities against NovelAI's own tag vocabulary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

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
        *,
        reconcile_researched: bool = False,
    ) -> TagResolution:
        """Resolve the first exact candidate returned by NovelAI.

        Args:
            source_name: Original character name.
            candidates: Romanized or English candidates from the planner.
            reconcile_researched: Whether candidates were grounded by web research and
                may be reconciled with a near-identical official character suggestion.

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
            if not reconcile_researched:
                continue
            name_match = re.fullmatch(r"(.+?)\s*\(([^()]*)\)", candidate)
            if not name_match:
                continue
            researched_name = self._normalize(name_match.group(1))
            researched_work = self._normalize(name_match.group(2))
            if not researched_name or not researched_work:
                continue
            work_confirmed = any(
                isinstance(item, dict)
                and isinstance(item.get("tag"), str)
                and self._normalize(item["tag"]) == researched_work
                for item in tags
            )
            if not work_confirmed:
                try:
                    work_response = await self._client.get(
                        f"{self._base_url}{SUGGEST_TAGS_ENDPOINT}",
                        params={"model": self._model, "prompt": name_match.group(2)},
                        timeout=12,
                    )
                    work_response.raise_for_status()
                    work_payload = work_response.json()
                except (httpx.HTTPError, ValueError, TypeError):
                    work_payload = {}
                work_tags = (
                    work_payload.get("tags", [])
                    if isinstance(work_payload, dict)
                    else []
                )
                work_confirmed = any(
                    isinstance(item, dict)
                    and isinstance(item.get("tag"), str)
                    and self._normalize(item["tag"]) == researched_work
                    for item in work_tags
                )
            if not work_confirmed:
                continue
            suggestion_sets = [tags]
            for token in researched_name.split():
                if len(token) < 4:
                    continue
                try:
                    token_response = await self._client.get(
                        f"{self._base_url}{SUGGEST_TAGS_ENDPOINT}",
                        params={"model": self._model, "prompt": token},
                        timeout=12,
                    )
                    token_response.raise_for_status()
                    token_payload = token_response.json()
                except (httpx.HTTPError, ValueError, TypeError):
                    continue
                token_tags = (
                    token_payload.get("tags", [])
                    if isinstance(token_payload, dict)
                    else []
                )
                suggestion_sets.append(token_tags)
            researched_tokens = researched_name.split()
            for suggestions in suggestion_sets:
                for item in suggestions[:8]:
                    tag = item.get("tag") if isinstance(item, dict) else None
                    if not isinstance(tag, str):
                        continue
                    suggested_name = self._normalize(tag)
                    if "(" in suggested_name or ")" in suggested_name:
                        continue
                    suggested_tokens = suggested_name.split()
                    if (
                        len(suggested_tokens) != len(researched_tokens)
                        or not set(suggested_tokens).intersection(researched_tokens)
                        or SequenceMatcher(
                            None,
                            suggested_name,
                            researched_name,
                        ).ratio()
                        < 0.92
                    ):
                        continue
                    return TagResolution(source_name, tag.strip(), candidate)
        return TagResolution(
            source_name,
            None,
            cleaned_candidates[0] if cleaned_candidates else None,
        )

    async def resolve_researched(
        self,
        source_name: str,
        candidates: list[str],
    ) -> TagResolution:
        """Resolve candidates grounded by web identity research.

        Args:
            source_name: Original localized character name.
            candidates: Evidence-backed romanizations and work-qualified tags.

        Returns:
            Exact or tightly reconciled official NovelAI character tag.
        """
        return await self.resolve(
            source_name,
            candidates,
            reconcile_researched=True,
        )
