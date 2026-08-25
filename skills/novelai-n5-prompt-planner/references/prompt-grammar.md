# Prompt 语法与标签组织

## 1. 主 Prompt 的基本形态

使用英文逗号分隔的标签序列：

```text
1girl, solo, silver hair, blue eyes, white coat, standing, looking at viewer, cowboy shot, snowy street, night, rim lighting, blue theme, high complexity, depthness, masterpiece, no text. Cold blue backlight outlines the girl while snow drifts across the quiet street.
```

高信息量、决定题意的标签放前面；装饰、氛围和质量词放后面。不要在同一 Prompt 中反复添加同义质量词或多个完整质量包。

## 2. 权重

资料中大量使用 NAI 4.5 数字权重组：

```text
1.3::low angle, looking up::
0.8::fog, floating particles::
-1.5::multiple views::
```

- `权重::标签或短语::` 控制一组内容，必须成对闭合。
- 大于 1 强化，小于 1 弱化；负值属于强抑制手段，应谨慎使用。
- 默认只给核心动作、关键构图或容易被模型忽略的道具加权。
- 优先采用温和范围，例如 `1.1`–`1.4`；只有经过验证的特殊串才使用更极端值。
- 旧式 `{tag}` / `{{tag}}` 强化和 `[tag]` / `[[tag]]` 弱化在资料中也很常见，但新生成的 V5 Prompt 优先使用数字权重，避免混合多套注意力语法和深层嵌套。

## 3. 自然语言片段

标签无法稳定表达复杂空间关系时，可以加入一句简短、具体、可见的英文描述：

```text
two adults, back-to-back, one looking left and the other looking right
```

不要写故事背景、人物心理活动、因果解释或模型无法直接画出的抽象判断。长描述应压缩为主体、动作、空间、环境和视觉效果。

## 4. 质量词

NovelAI 的 Add Quality Tags 是模型专用功能，而且添加内容可以不显示在用户编辑的 Prompt 中。因此：

- `quality_toggle` 开启时，不再手写叠加一整套质量词。
- `quality_toggle` 关闭时，可根据用户题材使用少量具体媒介或审美表达，但没有固定基线。
- 不同时叠加多套 `masterpiece / amazing quality / highres / ultra detailed` 变体。
- 用户要求原始标签、极简 Prompt 或特定风格测试时，可以完全省略质量表达。
- 插件会独立拼接画师串；这里不加入画师、年份或合作抑制标签。

## 5. Undesired Content

当前 QQ 插件默认只规划主 Prompt，API 的 negative prompt 默认留空。群成员可通过 `/n5 负面` 独立设置基础负面提示词，人物库也可保存对应的 V5 人物负面 caption；这些内容不交给规划模型改写。若调用方明确要求生成负面提示，应保持单独字段，并根据画面选择少量必要项，例如：

```text
lowres, blurry, bad anatomy, bad hands, extra digits, missing fingers, text, signature, watermark, multiple views
```

不要机械复制整套负面词；有意的模糊、裁切、单色、景深或多视图不应同时被否定。

## 6. 冲突处理

在输出前清理以下常见冲突：

- `close-up` 与 `full body`，除非有明确的分屏或插图布局。
- `day` 与 `night`，或互斥天气、季节。
- `from above` 与 `from below`。
- `looking at viewer` 与明确的闭眼、背对镜头要求。
- `solo` 与两人或多人互动。
- 静止站立与高速奔跑等互斥动作。
- 主 Prompt 中的期望效果与 Undesired Content 中的同名否定项。

用户明确要求冲突效果时，保留要求并用可实现的构图解释，例如 `split screen`、`reflection` 或 `double exposure`，不要静默删除。

## 7. 资料优先级

模型行为以 `knowledge/official-rules.json` 为准，并通过 `knowledge/source-manifest.json` 追溯 NovelAI 官方文档。下列个人法典只是历史语法与范例来源，属于低优先级经验；与当前 V5 官方规则、用户本次明确要求或运行协议冲突时必须舍弃。

历史参考包括：

- `所长常规NovelAI个人法典（2026.5.20版，一般所长整理）.docx`
- `所长色色NovalAI个人法典（上）（2026.5.20版，一般所长整理）.docx`
- `所长色色NovalAI个人法典（下）（2026.5.20版，一般所长整理）.docx`

三份文档合计约 3.57 万个非空段落，包含大量标签、数字权重、构图组合和自然语言范例。这里只保留跨题材、适合 V5 的稳定方法；未收录年龄含混、未成年、非自愿、剥削性或其他不安全类别。`粉红之书.docx` 与 `魔法咒語.docx` 不在截图指定的三份文档中，因此没有作为本 skill 的来源。
