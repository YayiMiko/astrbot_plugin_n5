# Changelog

## 0.4.1 - 2026-09-08

- Add per-user `/n5 nsfw [开|关]` switch for the global NSFW direction; `rating:` tags are still stripped when disabled. The switch is stored per QQ, defaults to on, and is shown in `/n5 状态`.
- Open generation to everyone when `allowed_sender_ids` is empty; a non-empty list still restricts usage. Bug-report admin notices no longer fall back to the sender whitelist.
- Default `allow_group` to true so group chats work without configuration.
- Fix comic delivery: collapse duplicate identities in one panel, and drop the storyboard text so comic requests only send the image.

## 0.4.0 - 2026-09-08

- Add `/n5 漫画` multi-panel comic generation: storyboard planning (1-4 panels, refused above the cap), skill-based Base/slot assembly, staggered slot coordinates with `use_coords`, per-panel `text` dialogue, fixed Heavy UC preset and scale 7.0.
- Credit the NAI5 comic method to the `nai5-prompt-expert` SKILL by 某单机游戏爱好者 in README and SKILL docs.

## 0.3.0 - 2026-09-08

- Remove the `/n5 尺寸` family of persistent size commands; the default output is now always 832x1216.
- Support per-request sizes with trailing keywords: 横图 uses 1216x832, 方图 uses 1024x1024, 超宽屏/电影超宽屏 uses 1536x640.
- Show the full command reference on a bare `/n5`; the `/n5 help` subcommand is gone.

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
