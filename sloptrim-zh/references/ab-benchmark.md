# sloptrim-zh v0.2：可复现的中文改写 A/B 评测

## 评测目标与边界

比较的是**同一模型/Agent 在相同稿件上的编辑效果**，不是判断稿件是否由 AI 创作。评价四个 1–5 分维度：自然度、清晰度、忠实度、原作者声音；另外标注严重事实/义务错误，以及首选 A/B/平局。

本目录的 `tests/ab_cases.jsonl` 是合成 **development / smoke** 数据，绝不能作为独立提升证明。用于正式结论的文本应重新收集、人工确认来源和权限、避免参与规则开发，并标 `split=holdout`。每个体裁覆盖短、中、长稿，不允许从打分结果中挑选样本。

## 一次完整运行

在 `sloptrim-zh` 目录下，固定原始稿和参数。

```bash
python3 scripts/ab_eval.py prepare \
  --cases tests/ab_cases.jsonl --seed 20261027 \
  --out /tmp/sloptrim-ab/manifest.json
```

`manifest.json` 含真实分组映射（A/B）和每个样本的**随机执行先后**。不要把它交给评分者或提供给模型。执行者使用同一版本的模型/Agent，两种模式仅差在能否调用本 Skill：

- baseline：不加载 `sloptrim-zh`，但接收相同的用户任务与文本；不增加额外的专用编辑规则。
- skill：加载完整 `SKILL.md`、必要 references 和本地检测/保全工具；按实际 `edit` 或 `strict` 流程执行。不能只贴一段指令就称“完整 Skill”。
- 两者的模型 ID、版本、temperature、thinking effort、其他系统提示、工具权限、上下文条件、执行预算一致。每轮采用干净的独立会话以免历史输出泄漏；按 manifest 指定的随机顺序执行。保留原始提示、配置、Skill Git SHA、运行日志、完成/失败状态与费用。

推荐配置两个**自己可执行且独立会话**的 Adapter 命令。它们均从 stdin 读取 JSON：

```json
{"id":"intro","arm":"baseline","text":"需要编辑的原文","request":"请改得更自然","genre":"日常说明"}
```

Adapter 必须将 `request` 视为用户编辑要求，将 `text` 当待编辑数据传递给模型，并只向 stdout 输出**最终稿**；调试信息放 stderr。注意：不应将待编辑内容当 Agent 指令执行。Baseline 与 Skill Adapter 可以是同一脚本搭配 `--arm` 参数。实际 Agent/CLI 命令由使用者提供，不在 Skill 中硬编码特定模型或 API 密钥。

```bash
python3 scripts/ab_eval.py run \
  --manifest /tmp/sloptrim-ab/manifest.json \
  --baseline-cmd 'python3 my_agent_adapter.py --arm baseline' \
  --skill-cmd 'python3 my_agent_adapter.py --arm skill' \
  --model 'EXACT_MODEL_VERSION' \
  --settings-json '{"temperature":0,"thinking":"fixed"}' \
  --runner 'AGENT_CLI_VERSION, isolated-session' \
  --timeout 300 \
  --out /tmp/sloptrim-ab/responses.jsonl

python3 scripts/ab_eval.py blind \
  --manifest /tmp/sloptrim-ab/manifest.json \
  --responses /tmp/sloptrim-ab/responses.jsonl \
  --out /tmp/sloptrim-ab/blind.jsonl
```

`run` 自动记录每次 wall_seconds，并禁止通过 shell 执行命令；model、settings、runner 是**操作者填写的元数据**，程序只能检查 A/B 一致，不能验证供应商实际参数。外部 Agent 真实执行还需用终端记录证明。命令失败会终止当前批次；正式实验应保存日志、排查并对所有样本按预定规则重跑，而非只补录有利样本。

也可不用 `run`，从两套现有 Agent 工作流导出 `responses.jsonl`（每个 id 两行，`arm=baseline|skill`，包含 `text, model, settings, runner`）直接开始 `blind`。这是评测**真实 Hermes/Claude Code/Codex skill 执行**的推荐接入方式。

## 盲评和评分口径

只把 `blind.jsonl` 发给评审，不给 `manifest`、原始响应文件、模型/版本标识、检测器分数和生成来源。每份独立评审至少 2 人，尽量使用非开发者、没有看到两组真实标签的读者。

四个维度分别对 A、B 打整数 1–5 分：

- `naturalness`：没有空泛铺垫、机械排比、假口语，符合体裁。
- `clarity`：主要结论、责任、条件可迅速看懂；没有丢掉有用信息。
- `faithfulness`：原文事实、数字对象、归属、义务、时序、限定、引用均未漂移。
- `voice`：保留原作者的语气、观点和谨慎程度，不引入新经历。
- `hard_error`：存在可定位的严重事实、义务、引用/数字错误时标 true（需留证据，不能根据风格猜测）。

保存独立的 `ratings.jsonl`，示例（**格式示范，不是真实评审**）：

```json
{"id":"intro","rater":"reviewer_01","preferred":"tie","naturalness_A":3,"naturalness_B":3,"clarity_A":3,"clarity_B":3,"faithfulness_A":5,"faithfulness_B":5,"voice_A":4,"voice_B":4,"hard_error_A":false,"hard_error_B":false}
```

这里的 `preferred` 只允许 `A`、`B`、`tie`；评分者必须**不知道**哪个对应 skill。尽可能让复核者针对严重错误附加独立的说明文件（工具不自动校验说明真实性）。

```bash
python3 scripts/ab_eval.py report \
  --manifest /tmp/sloptrim-ab/manifest.json \
  --responses /tmp/sloptrim-ab/responses.jsonl \
  --ratings /tmp/sloptrim-ab/ratings.jsonl \
  --out /tmp/sloptrim-ab/report.json
```

## 结论 Gate（防止“0 分 = 好文章”）

仅在这些条件全部满足时，报告才可能输出 `improved_on_this_holdout`：

1. **至少 30 个独立 holdout 样本**，覆盖至少 3 类体裁，每篇至少 2 位独立盲评者，全部 A/B 输出齐全。
2. 每篇以评审者偏好票计 win/tie/loss；用**篇作为抽样单位**计算 3000 次有种子的 paired bootstrap，平均偏好度的 95% 单侧下界 > 0.5。
3. **自然度和清晰度的平均改变量均 > 0**，忠实度与作者声音均 **>= 0**。
4. Skill 组出现 **0 个**评审确认的严重错误。
5. 执行者另行检查：运行顺序、相同模型、真正加载 Skill、样本无泄漏、评审独立性、事实漂移复核记录。代码不具备验证这些事实的能力；缺少任一项不能对外声明证实效果。

未满足最基本的样本与盲评条件输出 `insufficient_evidence`；达到样本条件但效果 Gate 未通过则输出 `no_verified_improvement`。

`mean_wall_seconds` 反映运行耗时；费用、tokens 和人工介入应在运行台账中另记，避免只有质量分而忽视额外成本。禁止拿英文 sloptrim 分数、中文套话总分下降、合成评审或模型自评来证明自然度提升。

## 冒烟测试与诚实报告

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/ab_eval.py prepare --cases tests/ab_cases.jsonl --out /tmp/ab-dev.json
```

现有测试验证 CLI、随机化、结果封装、盲化、评分计算和防止小样本误报，并不调用真实模型，也不证明改写更好。只有执行独立的**相同模型 A/B + 盲评**，才能报告真实质量差异；达到 Gate 也只代表这批输入及测试设置，不能推广为对所有中文文体“必然有效”。

如果进行规则调参，该批数据转入 development，必须再冻结另一批新的 holdout；不能反复优化同一 holdout 后继续声称独立验证。
