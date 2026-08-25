# 视觉规划方法

## 1. 画面槽位

把描述拆成以下槽位。年龄、性别、人数、身份和人物固定外观没有依据时留空；动作呈现、表情姿态、普通道具、镜头、环境、光照和氛围可以围绕用户意图主动补全，但不要为了数量加入无关细节。

| 槽位 | 典型内容 |
|---|---|
| 主体 | 人数、用户明确提供的年龄或性别表达、角色类型、物种 |
| 外观 | 发型、发色、眼睛、体型、表情、可见特征 |
| 服装与道具 | 服装层次、材质、配饰、手持物、关键颜色 |
| 动作 | 姿势、手部动作、视线、移动方向、人物互动 |
| 镜头 | 景别、视角、镜头倾斜、焦点、透视 |
| 构图 | 主体位置、对称、留白、前中后景、反射、分割 |
| 环境 | 室内外、地点、时间、天气、季节、背景元素 |
| 光色 | 主光方向、轮廓光、体积光、明暗关系、色彩主题 |
| 氛围与效果 | 宁静、紧张、梦幻、粒子、雾、运动感 |

## 2. 从抽象词到可见细节

抽象词只作为方向，需要用少量视觉信号落地：

- “电影感”：`cinematic composition, dramatic lighting, depth of field`，再结合明确景别和光源。
- “压迫感”：`low angle, close framing, looming, strong contrast, deep shadows`。
- “梦幻”：`soft lighting, pastel colors, glowing particles, mist, ethereal atmosphere`。
- “速度感”：`dynamic pose, motion blur, wind, flowing clothes, dutch angle`。
- “孤独”：`solo, wide shot, negative space, distant background, muted colors`。

这些是候选映射，不是固定套餐。每次选择 2–4 个真正支持用户意图的信号。

## 3. 景别与视角

常用景别：

- `close-up`：面部和局部情绪。
- `upper body` / `portrait`：人物上半身。
- `cowboy shot`：约大腿以上，兼顾人物与动作。
- `full body`：完整姿态和服装。
- `wide shot` / `very wide shot`：环境叙事和尺度感。

常用视角与镜头：

- `from above`, `from below`, `from side`, `from behind`
- `low angle`, `high angle`, `dutch angle`
- `fisheye`, `wide-angle lens`, `foreshortening`
- `pov`, `looking at viewer`, `looking away`
- `depth of field`, `sharp focus`, `foreground blur`

每次优先选一个主景别和一个主视角。除非用户要求特殊画面，不混用互斥镜头。

## 4. 构图

资料中反复出现的稳定构图手段包括：

- 对称与中心：`symmetrical composition, centered composition`。
- 对角线与动势：`diagonal composition, dynamic angle, dutch angle`。
- 留白与孤立：`negative space, off-center composition`。
- 层次：明确 `foreground / midground / background` 中的物体。
- 镜像与反射：`mirror, reflection, water reflection`。
- 双人关系：`back-to-back, facing each other, side by side`。
- 分割：`split screen, divided composition`，仅在用户真的需要并列场景时使用。
- 水面边界：`half underwater, waterline`，同时指定水上和水下元素。

构图标签应服务主体，不要一次堆叠多个互相抢夺画面的模板。

## 5. 光照与色彩

先确定主光逻辑，再加氛围效果：

- 方向：`backlighting, side lighting, rim lighting, window light`。
- 明暗：`chiaroscuro, hard shadows, soft shadows, high contrast`。
- 空气：`volumetric lighting, light rays, dappled sunlight, fog`。
- 色彩：`blue theme, warm colors, limited palette, monochrome, complementary colors`。
- 时间：`golden hour, sunset, night, moonlight`。

不要同时要求柔和均匀光和极端硬阴影，除非明确区分主光与环境光。

## 6. 多人物

当前自动化使用 NAI5 原生主 caption 与独立 character captions：

1. 先写准确人数及用户明确提供的人物条件，不自行补写年龄或性别。
2. 在主 Prompt 写双方共有场景、总体构图、共享动作与关系。
3. 在各自 `character_prompts` 中写本图服装、配饰、手持物、个体动作、表情和视线，固定外观由人物库另行注入。
4. 用位置、服装颜色或动作区分人物，但不要在主 Prompt 混写无法归属的个人服装。
5. 用明确关系短语连接，例如 `facing each other`、`holding hands`，并在双方人物 Prompt 中分配配对动作。
6. 避免输出字面 `char1:`、`char2:`；只使用运行协议提供的人物槽位键。

多人画面信息密度高，应优先保证人数、位置和互动，但每个人仍需保留足以区分身份的服装轮廓、主配色和关键饰件。

## 7. 自然语言压缩

将中文描述转成标签时：

1. 删除叙事连接词、原因、评价和不可见心理活动。
2. 保留名词、动作、空间关系、材质、颜色和光照。
3. 合并同义项，选择更具体的表达。
4. 把复杂空间关系保留为一句短英文片段。
5. 最后按主次顺序重排，而不是照着中文句序逐词翻译。

例如“她在雨夜车站等人，心里很失落”可以落实为：单人、站台、雨夜、等待姿态、低垂视线、湿衣、冷色、远景和留白；不要直接把“心里很失落”当作抽象标签堆入。
