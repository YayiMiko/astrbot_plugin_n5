# AstrBot / DeepSeek 运行契约

## 推荐系统提示

插件运行时只对自然语言描述调用规划器。它先加载 `knowledge/source-manifest.json`、`knowledge/official-rules.json` 和 `knowledge/local-preferences.json`，验证模型、来源与执行级别，再按顺序拼接 `runtime-system-prompt.txt` 与 `runtime-semantic-expansion.txt`。已经包含 NovelAI 标签、权重或画师字段的 Prompt 必须跳过模型；在 API 前全局补入一次 `nsfw` 并删除所有 `rating:` 分级词，其余内容直通。用户原始画面描述放入单独 user message。不要在代码中复制这些规则。

## 调用建议

- Prompt 规划使用独立 Provider，不要复用群聊会话历史。
- `persist=False`，避免前一位群成员的描述污染后一位。
- 温度宜低，目标是稳定结构化转换而非自由聊天。
- 将规划结果长度设为接口安全上限；不把它当成内容长度目标，也不设最少 Tag 数、最少字数或最少句数。
- JSON 解析或语义校验失败时最多重试两次，并在重试提示中附上原始描述与错误摘要，不要把模型原文直接送入 NovelAI API。
- 对 `prompt` 做长度、控制字符和禁止字段检查，再与插件管理的画师串拼接。
- 每次调用都注入本次唯一合法的人物槽位键集合。若输入含人物占位符，校验 `character_prompts` 的键集合完全一致；值中保留本图服装、道具和动态设计，校验通过后再由插件与人物库的固定身份 Prompt 拼接为 NAI5 原生 character caption。若没有合法槽位，强制 `character_prompts` 为 `{}`，并禁止规划器把作品角色名、普通姓名或示例伪造成内部槽位。
- 日志记录请求 ID、群号、用户号、耗时和失败类型；不要记录登录 Cookie、Authorization 或完整敏感 Prompt。

## 拼接顺序

推荐由插件完成：

```text
主 caption: <当前画师串（若用户已选择）>, <planner 返回的 prompt>
character caption: <人物库固定身份 Prompt>, <planner 返回的本图服装、道具与动态 Prompt>
```

规划器永远不知道也不修改画师串。这样同一个规划结果可以安全地复用于不同群、不同用户的画师串状态。

## 失败回退

- Provider 超时或 JSON 无效：提示“Prompt 规划暂时失败”，不要把未验证的模型回复当成 Prompt。
- 描述过短：允许根据语义需要使用简洁规划；只要保留了用户意图并通过机器协议校验，不因结果短而重试。
- 描述矛盾：优先保留最后一个明确约束；仍无法判断时返回可读错误，不随机选择。

当前固定的机器协议为：

```json
{"ok":true,"prompt":"...","character_prompts":{},"error":null}
```

或在硬约束确实无法消解时：

```json
{"ok":false,"prompt":null,"character_prompts":{},"error":"conflicting_constraints"}
```

不得让模型自行增加字段。`error` 目前只允许 `conflicting_constraints`。
