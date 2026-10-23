# 中文检测器的语料校准配方

改 `_ZH_PATTERNS` 任一条阈值、或修任何解析逻辑之后，按这个顺序验。
判据是**分离是否仍成立**，不是「分数看起来对不对」。

## 第一层：夹具分离检验（秒级，每次改都跑）

```bash
cd ~/.hermes/skills/productivity/sloptrim-zh/tests
python3 validation_corpus_zh.py
```

判据：`AI 组最低分 > 人写组最高分`。当前基线：人写最高 20（业务文档组），
AI 最低 48，间隔 28。**间隔变窄就是回归。**

## 第二层：真实飞书语料外验（防夹具过拟合）

数据源在 `~/clawd/kb/`，三个 jsonl：
`sow-space-all.jsonl`、`prod-space-all.jsonl`、`sow-standard.jsonl`。
每条一个 JSON，正文在 `markdown` 字段，另有 `title` / `url` / `sha1`。

去重与筛选（两个都必须做）：

- 按 `sha1` 去重——同一个文档在多个空间重复出现，不去重会把同一篇算多次，
  直接拉偏分布。
- 只收 ≥800 字——过短文档被统计类 pattern 系统性抬分，混进来污染基线。

```python
import json, pathlib, re, importlib.util
spec = importlib.util.spec_from_file_location(
    "dz", "/Users/ronny/.hermes/skills/productivity/sloptrim-zh/scripts/detect_zh.py")
dz = importlib.util.module_from_spec(spec); spec.loader.exec_module(dz)

kb = pathlib.Path.home() / "clawd/kb"
def zh_count(s):
    return (len(re.findall(r"[\u4e00-\u9fff]", s)) + len(re.findall(r"[A-Za-z]+", s))
            + len(re.findall(r"\d+", s)))

docs = {}
for fn in ["sow-space-all.jsonl", "prod-space-all.jsonl", "sow-standard.jsonl"]:
    for line in (kb / fn).open(errors="ignore"):
        try: r = json.loads(line)
        except Exception: continue
        md = r.get("markdown") or ""
        if zh_count(md) < 800: continue
        docs.setdefault(r.get("sha1"), {"title": r.get("title", ""), "md": md})

for sha, d in docs.items():
    m = dz.scan(d["md"])["_metrics"]
    print(m["ai_tell_score"], d["title"][:50])
```

当前基线（468 篇）：415 篇 0 分，中位 0，唯一 ≥25 分是真阳性（图注重复 73 次）。
**0 分篇数下降就是回归。**

## 第三层：本地 docx 抽取实测

```python
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location(
    "dz", "/Users/ronny/.hermes/skills/productivity/sloptrim-zh/scripts/detect_zh.py")
dz = importlib.util.module_from_spec(spec); spec.loader.exec_module(dz)
for p in sorted((pathlib.Path.home()/"Downloads").glob("*.docx"))[:15]:
    if p.name.startswith("~$"): continue          # Word 锁文件，不是文档
    text, _ = dz.extract_text(p)
    han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    print(p.name[:40], len(text), han, dz.scan(text)["_metrics"]["ai_tell_score"])
```

判据：**抽不到汉字 = 抽取链路坏了**（不是「文档干净」）。15 篇真实 docx
（SOW／需求／战略／画质要求）应全部有汉字、全部低分。

## 夹具新增样本的规矩
- **新样本截到与 AI 组同量级**（约 200–250 字）。密度是每千字算的，人写片段
  截 800 字跟 AI 样本 200 字比，密度口径就不一致，结论无效。
- **人写侧要按体裁铺开**，不能全是同一种文体。已有的 `BUSINESS` 组专门覆盖
  「术语密集但人写」：SOW、岗位 JD、产品线战略。新增时优先补**没覆盖的体裁**，
  不要堆同类。
- 夹具里的文本必须**真实来源**，不能用想象的人写文风冒充——自己编的「人写样本」
  会带自己以为的人写特征，校准出来的是自己的偏见。
- 每加一篇，在 `validation_corpus_zh.py` 顶部的 docstring 里登记来源一句。

## 灰区文档：逐篇读，不要只看汇总

噪声线以上的每一篇都要拉出命中原文逐条判断，四分类（真命中／体裁误配／噪声／
确认 bug）见 SKILL.md「灰区文档怎么判」。要点：

- **报给用户时逐篇给全事实**：标题、分数、字数、命中模式与次数/密度、被命中的原句、
  链接。不能用「那几篇」带过。
- 把「实测复现的 bug」和「我的口径推断」分开写，后者要用户拍板。
- 用户已拍板：企业文书腔（赋能／打造／进行+动词／闭环）在内部文档里**也算 AI 腔**，
  不豁免。

## 拉单篇命中原文

```python
res = dz.scan(md)
for k, v in res.items():
    if k.startswith("_") or v.get("tier") == "report":
        continue
    print(f"[{v['label']}] x{v['count']} 密度{v['density']}")
    for s in v.get("samples", [])[:3]:
        print("   …", s.strip(), "…")
```

`_metrics` 里的 `confidence` 和 `band` 一起看：分数高但 `confidence=low` 且字数很小，
基本是抽样噪声。
