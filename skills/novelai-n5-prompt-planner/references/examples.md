# 规划范例

以下范例用于校准结构与信息密度，不是必须逐字复用的模板。

## 单人物与环境

输入：

```text
一位银发蓝眼的成年女性穿白色长外套，夜晚站在下雪的街道上，冷色，半身镜头，背光。
```

输出：

```text
1woman, adult, solo, silver hair, blue eyes, white long coat, standing, upper body, snowy street, night, backlighting, rim lighting, blue theme, cold colors, high complexity, depthness, masterpiece, no text. Cold blue backlight traces the woman's silhouette while fine snow drifts through the empty street.
```

## 动作与低机位

输入：

```text
成年女剑士从废墟上跃下，披风被风吹起，低机位，动作感强，夕阳。
```

输出：

```text
1woman, adult, solo, swordswoman, holding sword, 1.3::jumping down, dynamic pose, foreshortening::, flowing cape, low angle, from below, ruins, sunset, wind, dramatic lighting, motion blur, high complexity, depthness, masterpiece, no text. The swordswoman descends from a broken wall as the low camera catches her cape sweeping across the sunset.
```

## 双人物关系

输入：

```text
两位成年女性背靠背站在雨中的霓虹街道，一人红发黑外套，一人蓝发白外套，广角全身。
```

输出：

```text
2women, adults, back-to-back, standing, full body, one woman with red hair and a black coat, the other woman with blue hair and a white coat, wide-angle lens, neon street, rain, night, wet pavement, colorful reflections, cinematic lighting, high complexity, depthness, masterpiece, no text. The two women stand back-to-back in the center of a rain-soaked street while neon signs reflect across the pavement around them.
```

## 抽象氛围落地

输入：

```text
一个成年旅行者走在巨大空旷的盐湖上，要非常孤独、安静。
```

输出：

```text
1adult, solo, traveler, walking, small figure, very wide shot, centered horizon, vast salt flat, distant mountains, negative space, muted colors, soft natural lighting, still atmosphere, high complexity, depthness, masterpiece, no text. The traveler is reduced to a small silhouette crossing the immense salt flat beneath a pale, empty sky.
```

## 保持极简

输入：

```text
1girl，不要给我添加服装和背景，只优化基础质量。
```

输出：

```text
1girl, solo, medium complexity, masterpiece, no text
```

## 冲突修正

输入：

```text
一个人，全身大特写，白天夜景，从上方仰拍。
```

不要静默输出互斥标签。若调用方支持错误协议，应返回“镜头与时间约束冲突”；若必须产出，则只按最后一个明确约束解析，并记录内部冲突，不在 Prompt 中同时保留两套互斥词。
