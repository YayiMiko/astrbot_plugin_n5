# Changelog

## 0.2.0 - 2026-09-08

- Remove `/n5 漫画`, `/n5 漫画抽卡`, `/n5 参考`, `/n5 再来`, `/n5 重抽`, `/n5 重发`, `/n5 最近`, `/n5 负面`, and `/n5 诊断` along with their planners, state, and skill references.
- Stop sending the admin help text as a private message on `/n5 help`.
- Add `novelai_api_token` to the plugin config page; a non-empty value takes precedence over the environment variable and DPAPI file.
- Mention the config page entry in PAT-missing error messages.

## 0.1.0 - 2026-08-25

- Add NovelAI Diffusion V5 Curated and Full generation through the official API.
- Add DS4F Vision multimodal prompt planning with request-scoped image references.
- Add NovelAI-native character identity verification and localized-name repair.
- Add user-scoped character, artist-string, negative-prompt, size, and model settings.
- Add natural-language, reference-image, direct-prompt, and redraw generation modes.
- Add versioned official V5 prompt knowledge with deterministic protocol safeguards.
- Add guarded Opus generation, serialized request handling, rate-limit retries, and image validation.
- Add global NSFW prompt direction without content-rating tags.
