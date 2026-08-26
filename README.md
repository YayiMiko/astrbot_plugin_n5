# astrbot_plugin_n5

一个独立的 **AstrBot** 插件，通过 **NovelAI 官方 API** 生成 **NovelAI Diffusion V5 Curated / Full** 图片。它把前端提示词改为「请求级 DS4F Vision 多模态规划 + NovelAI 官方身份词表校验」，让自然语言描述能稳定落地成可执行的 V5 Prompt。

---

## 功能特性

- **多模态提示词规划**：自然语言描述由 DeepSeek（默认 `deepseek/deepseek-v4-flash-vision-exp`）规划为标签 + 自然语言的混合 Prompt，支持原生图片输入（`/n5 参考`）。
- **V5 漫画模式**：`/n5 漫画` 使用独立页面规划，按分格描述布局、动作和短对白，并允许同一角色跨格重复出现。
- **镜头级漫画分镜**：漫画请求先独立规划每格的景别、机位、人物站位、动作、状态变化、连续性和对白，再转译为 V5 Prompt。
- **漫画抽卡**：只提供一个或多个角色，分镜规划器随机创作或扩写指定剧情的完整四拍事件，再生成充分展开的四格 V5 漫画 Prompt。
- **版本化官方规则库**：NovelAI V5 模型行为以可追溯的官方来源清单和机读规则为事实层，运行时校验适用模型和来源引用，再与低优先级的本地偏好和语义补全提示组合。
- **联网译名纠错与官方身份锁定**：首次候选未命中时调用 AstrBot 已配置的 Tavily 搜索，通过角色原名与作品名查找官方英文名或通用罗马字，再由 NovelAI `suggest-tags` 精确复核；验证成功的别名会缓存，未验证结果不会冒充官方 Tag。
- **同人创作引用规划**：名招、名场面、构图、地点与道具会和人物分流；插件联网整理其可见动作、镜头、空间几何、特效、色光与环境响应，再交给 NAI5 以“必要标签锚点 + 充分英文自然语言”重建，不会把原作施术者自动画进来。
- **图源解析**：优先读取本条消息中的图片，其次读取引用消息中的图片。
- **NovelAI PNG 元数据优先**：带图请求会先读取 PNG 内嵌的 `Description` / `Comment` 元数据，身份事实优先于视觉猜测。
- **人物库**：群成员可保存「全局人物」，命中角色名时自动注入固定身份与固有外观，再由规划器补全本图服装、道具与动态。
- **画师串**：预设与切换画师串（`artist:` 风格串），由插件在最终 Prompt 前独立拼接；规划器只生成主 Prompt，不生成或修改画师串。
- **负面提示词**：每个用户可独立设置自己的 Undesired Content。
- **全局 NSFW 语义方向**：所有生图请求保留字面 `nsfw`，同时删除 `rating:` 内容分级词；具体细节由规划器按用户意图决定，不套用固定 Tag 包。
- **每用户生成尺寸**：竖图 / 横图 / 方图 / 自定义尺寸。
- **V5 模型切换**：WebUI 下拉设置默认模型，聊天中可按用户切换 V5C / V5F。
- **免费生成防护**：Opus 身份校验、总像素上限、Steps 上限、响应大小上限，以及 429 排队重试。
- **队列与并发控制**：串行生成 + 排队计数，避免并发打爆账号。
- **可靠图片交付**：插件直接发送不带引用的纯图片；NapCat 回执超时时核验最近消息，未确认才自动重试一次，并保留请求级状态供 `/n5 重发` 使用。
- **Bug 反馈**：`/n5 bug反馈`，反馈会记录并私聊通知管理员。
- **旧插件迁移**：首次启动时自动复制旧插件 `astrbot_plugin_novelai` 的兼容数据。

---

## 工作原理

1. 用户发送 `/n5 生成 <描述>`，插件按「本条图片 → 引用图片」的优先级解析请求级图片与 NovelAI PNG 元数据。
2. 若输入是现成的 NovelAI 标签 Prompt（或使用 `/n5 原始`），则**跳过自然语言规划**；除全局补入 `nsfw` 并删除 `rating:` 分级词外，其余内容直通。
3. 第一阶段同时提取“实际出场人物”和“创作引用”。人物先生成官方名称候选并查询 NovelAI 词表；首次未命中时才联网检索译名，并将搜索证据交给 LLM 纠错后再次精确验证。服装、外观或扮演来源不会被计为额外出场人物。
4. 名招、名场面、构图、地点和道具引用走独立的联网视觉研究，形成英文可视蓝图；随后把描述与蓝图交给 DeepSeek 规划器，输出严格单行 JSON：`{"ok":true,"prompt":"...","character_prompts":{...},"error":null}`。所有自然语言规划都不套用固定 Tag 数量、人物 Tag 最低项数或固定句数，`max_prompt_length` 仅作为接口字符安全上限。
5. 插件按「机器协议/API 安全 > 用户明确要求 > NovelAI V5 官方规则 > 本地偏好 > 语义启发」校验规划结果。代码只对人数、单人 `solo`、人物槽位、显式服装状态、禁止字段和长度等可确定事项做硬校验；不再为画师、拥抱、推倒或某类情绪设固定 Tag 词包。
6. 命中人物库角色时，把角色名替换为 `__NAI_CHARACTER_SLOT_<数字>__` 槽位，规划器只负责该角色的本图动态 Prompt，固定身份与固有外观由人物库注入。
7. 最终按「当前画师串 + 主 Prompt」拼接，调用 NovelAI 官方 `/ai/generate-image`，解析 ZIP / JSON / SSE 返回的图片，校验尺寸一致后保存。

规则库位于 `skills/novelai-n5-prompt-planner/knowledge/`：`source-manifest.json` 保存官方来源与适用模型，`official-rules.json` 保存可机读的官方规则和执行级别，`local-preferences.json` 仅保存本插件的低优先级产品偏好。

---

## 安装

> 当前版本 `0.1.0`，要求 AstrBot `>=4.26, <5`，支持平台 `aiocqhttp`（QQ OneBot）。

将本仓库放入 AstrBot 的 `data/plugins/` 目录（或按 AstrBot 插件规范安装），并安装依赖：

```bash
pip install -r requirements.txt
```

依赖：`httpx>=0.27,<1`；`Pillow` 由 AstrBot 自带。

---

## 配置

在 AstrBot 的插件配置面板中设置以下参数（均有默认值）：

| 参数 | 说明 | 默认 |
|---|---|---|
| `allowed_sender_ids` | 允许使用 NovelAI 的 QQ 白名单；**留空时拒绝所有指令** | `[]` |
| `bug_report_admin_ids` | Bug 反馈私聊通知的 QQ 列表；留空时回退到控制者白名单 | `[]` |
| `allow_group` | 是否允许群聊触发（总开关） | `false` |
| `allowed_group_ids` | 允许使用 NovelAI 的群号白名单；留空时所有群开放 | `[]` |
| `max_total_pixels` | 免费生成总像素上限 | `1048576` |
| `max_steps` | 免费生成 Steps 上限 | `28` |
| `steps` | NovelAI API 生成 Steps（Opus 免费生成不得超过 `max_steps`） | `23` |
| `image_model` | 默认绘图模型，可在 WebUI 下拉选择 V5C / V5F | `nai-diffusion-5-curated` |
| `negative_prompt` | 兼容用默认 Undesired Content（默认留空） | `""` |
| `default_artist_string_name` | 全局默认画风名称 | `千代NAI1` |
| `default_artist_string` | 全局默认画师串 | `artist:deyui, artist:yukisiannn, ...` |
| `quality_toggle` | 是否使用 NovelAI 自动质量标签（NAI5 默认关闭） | `false` |
| `uc_preset` | NovelAI UC Preset 编号（`3`=None，仅用 `/n5 负面`） | `3` |
| `max_prompt_length` | Prompt 最大字符数 | `4000` |
| `prompt_planner_enabled` | 是否启用 DeepSeek Prompt 规划 | `true` |
| `prompt_planner_provider_id` | Prompt 规划模型 Provider ID | `deepseek/deepseek-v4-flash-vision-exp` |
| `max_character_prompt_length` | 单个人物 Prompt 最大字符数 | `2000` |
| `max_characters_per_prompt` | 单次描述自动引用人物的上限（`1`–`6`） | `4` |
| `timeout_seconds` | 等待 NovelAI API 生成的超时秒数 | `180` |
| `delivery_verify_delay_seconds` | 图片 ACK 超时后等待 NapCat 历史核验的秒数 | `3` |
| `rate_limit_max_retries` | 429 最大排队重试次数 | `8` |
| `rate_limit_wait_seconds` | 429 固定重试间隔秒数 | `5` |
| `max_response_bytes` | NovelAI API 响应最大字节数 | `16777216` |
| `max_image_bytes` | 返回 QQ 的单图最大字节数 | `33554432` |
| `max_image_pixels` | 结果图最大总像素 | `16777216` |

> 说明：`prompt_planner_provider_id` 与群聊默认模型相互独立。它必须支持原生图片输入（`/n5 参考` 需要）。

---

## 认证

插件需要一个 **NovelAI 持久化 API Token（PAT）**，两种方式二选一：

**方式一：环境变量（推荐，Linux / 容器）**

```bash
export NOVELAI_API_TOKEN="pst-你的token"
```

**方式二：Windows DPAPI 加密文件**

Windows 下用随附脚本把 PAT 用 DPAPI 加密存储，避免明文落盘：

```bash
python scripts/configure_pat.py
```

运行后会生成 `data/plugin_data/astrbot_plugin_n5/novelai_pat.dpapi`。

> 注意：`/n5` 的免费生成路径要求当前账号为**有效的 NovelAI Opus**（`tier=3` 且 `active`），否则会被拒绝。

---

## 指令列表

| 指令 | 说明 |
|---|---|
| `/n5 生成 <内容>` | 自然语言扩写；附图时使用 DS4F Vision 参考 |
| `/n5 漫画 <剧情>` | 规划并生成完整的多格漫画页 |
| `/n5 漫画抽卡 <角色>[，剧情]` | 为指定角色随机创作，或扩写指定剧情并生成四格漫画 |
| `/n5 参考 <修改要求>` | 使用本条或引用消息中的图片作参考 |
| `/n5 原始 <Prompt>` | 跳过自然语言规划，原样生成 |
| `/n5 再来` | 复用自己上一次成功生成的最终 Prompt（等价 `/n5 重抽`） |
| `/n5 重发` | 重发当前会话最近生成的图片，不重新调用 NovelAI |
| `/n5 最近` | 查看当前会话最近一次图片交付状态 |
| `/n5 角色 [名称]` | 列出或查看自己的角色（等价 `/n5 人物`） |
| `/n5 画风 [名称\|默认\|原生]` | 查看或切换画风（等价 `画师串` / `切换画师串` / `查看画师串`） |
| `/n5 负面` | 查看自己的当前负面提示词 |
| `/n5 负面 <内容>\|清空` | 设置或清空自己的负面提示词 |
| `/n5 模型 [V5C\|V5F]` | 查看或切换自己的 NovelAI V5 绘图模型 |
| `/n5 尺寸 竖图\|横图\|方图\|<宽>x<高>` | 设置免费尺寸（等价 `切换大小` / `自定义大小`） |
| `/n5 状态` | 检查 PAT、Opus、Anlas 与免费生成参数 |
| `/n5 诊断` | 显示当前模型、指令路由与图片来源策略 |
| `/n5 bug反馈 <内容>` | 记录 Bug 反馈并通知管理员 |

**画师串与人物库管理：**

| 指令 | 说明 |
|---|---|
| `/n5 添加画师串 <名称> <内容>` | 保存本群画师串 |
| `/n5 创建人物 <角色名> <Prompt> [--负面 <内容>]` | 保存全局人物，命中名字时自动引用 |
| `/n5 删除人物 <角色名>` | 删除全局人物 |
| `/n5 确认` | 在 60 秒内确认「覆盖 / 删除人物」操作 |

---

## 权限控制

插件默认**失败即关闭**（fail-closed）：

- 只有 `allowed_sender_ids` 白名单中的 QQ 才能执行 NovelAI 指令，私聊和群聊统一校验；**列表留空时拒绝所有指令**。
- `allow_group=false` 时禁止群聊使用。
- 群聊中即使开启了 `allow_group`，也仍只允许 `allowed_sender_ids` 中的账号执行。
- `allowed_group_ids` 留空时允许所有群，非空时只放行白名单群号。

---

## 使用示例

```text
/n5 生成 一位银发蓝眼的成年女性穿白色长外套，夜晚站在下雪的街道上
/n5 漫画 狐莉起床后发现尾巴缠在毯子里，制作成四格漫画
/n5 漫画抽卡 狐莉和鲸鱼娘
/n5 漫画抽卡 原神空和荧，抢夺包子
/n5 参考 把背景改成雨夜的霓虹街道（配合一张参考图）
/n5 原始 1girl, silver hair, blue eyes, solo, standing, white long coat
/n5 画风 千代NAI1
/n5 画风 原生
/n5 尺寸 竖图
/n5 状态
```

---

## 常见问题

- **提示「当前 QQ 不在白名单」**：在 AstrBot 配置中把 QQ 号加入 `allowed_sender_ids`。
- **提示「不是有效的 NovelAI Opus」**：免费生成路径只对有效的 Opus（`tier=3`）开放。
- **提示「未配置 PAT」**：设置 `NOVELAI_API_TOKEN` 环境变量，或用 `configure_pat.py` 生成 DPAPI 文件。
- **`/n5 参考` 不生效**：该指令需要本条消息或引用消息中带有图片，且规划模型必须支持原生图片输入。
- **Prompt 规划失败**：检查 `prompt_planner_provider_id` 是否可用；规划器默认三次重试，超时或 JSON 无效时会提示「Prompt 规划暂时失败」，不会把未经校验的模型回复当作 Prompt。
- **图片生成后没有出现**：插件会在 NapCat ACK 超时时核验最近消息并至多自动重试一次；仍失败时可发送 `/n5 最近` 查看状态，或发送 `/n5 重发` 直接重发已有图片，不会消耗新的生成次数。

---

## 开发

```bash
uv run pytest -q
uv run ruff format .
uv run ruff check .
```

---

## 免责声明

本插件为个人创作工具，仅通过 NovelAI 官方 API 生成图片，请遵守 NovelAI 的服务条款与所在地法律法规。生成的图片版权归对应作者与平台所有，请勿用于任何侵权或违规用途。
