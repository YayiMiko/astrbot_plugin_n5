# astrbot_plugin_n5

An independent AstrBot plugin for NovelAI Diffusion V5 Curated. It keeps the
official NovelAI API generation path while replacing the prompt front end with
request-scoped DS4F Vision planning and NovelAI-native character tag checks.

## Design

- Exact `/n5` routing only. The plugin deliberately does not register `/nai`.
- Direct image first, quoted image second, and no shared `latest` image fallback.
- NovelAI PNG metadata is read before visual inference when available.
- DS4F Vision extracts named identities and reference-image appearance.
- NovelAI `suggest-tags` is the primary identity vocabulary authority.
- Existing tag prompts can bypass planning with `/n5 原始`.
- The proven V5 payload, Opus guard, free-generation limits, queue, ZIP parsing,
  artist presets, character presets, negative prompts, and per-user sizes remain.

## Commands

```text
/n5 生成 <description>
/n5 参考 <change request>
/n5 原始 <NovelAI prompt>
/n5 再来
/n5 角色 [name]
/n5 画风 [name|默认|原生]
/n5 负面 [prompt|清空]
/n5 尺寸 竖图|横图|方图|<width>x<height>
/n5 状态
/n5 诊断
```

Advanced preset mutation remains available under `/n5 添加画师串`,
`/n5 创建人物`, `/n5 删除人物`, and `/n5 确认`.

## Configuration

Set `NOVELAI_API_TOKEN` in the AstrBot runtime environment. Restrict callers
with `allowed_sender_ids`; `allow_group=true` plus an empty
`allowed_group_ids` list enables authorized users in every group.

The default prompt provider is
`deepseek/deepseek-v4-flash-vision-exp`. It must support native image input for
`/n5 参考`.

## Development

```bash
uv run pytest -q
uv run ruff format .
uv run ruff check .
```
