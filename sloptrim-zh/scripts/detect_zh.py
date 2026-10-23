#!/usr/bin/env python3
"""detect_zh.py — 中文 AI 腔检测器。

设计原则（对齐上游 sloptrim 的架构，中文重做语言层）：

1.  语言无关层**复用**上游 detect.py：字符清洗（零宽字符、同形异码、
    非标准空格）、Markdown 剥离、引文剥离、格式提取（docx/epub/odt…）。
    找不到上游时降级为纯正则实现，检测器仍可独立运行。
2.  语言相关层全部中文重做：
      - 分句：。！？；…（不按换行切；标题、列表已在剥 markdown 时移除）
      - 计数：汉字按字计，英文按词计，数字按个数计
      - 命中判据：**密度**（每千字命中数）而非绝对次数。
        理由：中文信息密度远高于英文，同一绝对次数在 200 字和 5000 字
        的文本里意义完全不同。实测全部计分 pattern 合计密度：中文人写
        ≤2.23/千字，模型生成 ≥26.2/千字（见 SKILL.md「校准」）。
3.  有一部分上游 pattern 在中文里**不成立**，明确降级或排除：
      - 破折号（——）：中文正常标点，降为 report-only
      - 缩写、contraction absence、title case 等：无中文对应物
4.  分数是**分诊启发式**，描述文本不描述作者。

用法：
    python3 detect_zh.py file.md            # JSON 报告
    python3 detect_zh.py --clean file.md    # 字符层清洗后文本
    python3 detect_zh.py < file.md          # stdin
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import statistics
import sys
import unicodedata
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- 上游复用
def _find_upstream() -> Path | None:
    """定位上游 detect.py。优先 skill 目录，其次通用搜索。"""
    cands: list[Path] = []
    env = os.environ.get("SLOPTRIM_DETECT_PY")
    if env:
        cands.append(Path(env))
    # 快路径：与本 skill 同级的 sloptrim（标准安装布局），免去整树 rglob。
    here = Path(__file__).resolve()
    cands.append(here.parent.parent.parent / "sloptrim" / "scripts" / "detect.py")
    for p in cands:
        if p.is_file():
            return p
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    for root in (home / "skills", Path.home() / ".hermes" / "skills"):
        if not root.is_dir():
            continue
        for p in root.rglob("sloptrim/scripts/detect.py"):
            cands.append(p)
    for p in cands:
        if p.is_file():
            return p
    return None


def _load_upstream():
    p = _find_upstream()
    if not p:
        return None
    try:
        spec = importlib.util.spec_from_file_location("sloptrim_detect_upstream", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_UP = _load_upstream()


# -------------------------------------------------- 降级实现（无上游时）
def _fallback_strip_markdown_furniture(text: str) -> str:
    text = _strip_feishu_tables(text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+.*$", " ", text, flags=re.M)
    text = re.sub(r"^\s{0,3}[-*+]\s+", " ", text, flags=re.M)
    text = re.sub(r"^\s{0,3}\d+[.)]\s+", " ", text, flags=re.M)
    text = re.sub(r"^\s{0,3}>.*$", " ", text, flags=re.M)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s*[-*_]{3,}\s*$", " ", text, flags=re.M)
    text = re.sub(r"\|", " ", text)
    return text


# 飞书导出的 markdown 用 <lark-table> / <lark-tr> / <lark-td> 表示表格，
# 用 <at id=...> 提及人，用 {align="center"} 之类的属性标记。
# 这些标记的标签名（larktd、larktr）本身是英文，若不剥掉会污染 n-gram。
_LARK_TAG = re.compile(r"</?lark-[a-z-]+(?:\s[^>]*)?>", re.I)
_LARK_AT = re.compile(r"<at\b[^>]*>.*?</at>", re.I | re.S)
_LARK_ATTR = re.compile(r"\{[a-zA-Z_]+=\"[^\"]*\"\}", re.I)
_HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^>]{0,200})?>", re.S)

# 整块表格：**连内容一起去掉**。
# 只剥标签会把每个单元格变成一段孤立文本（短行、编号列表、重复的表头），
# 这些会被当成散文——实测一份 TMC 文档因表头重复 11 次被判成「退化重复」。
# 表格是结构化数据，整块移除才是干净的边界。
_LARK_TABLE_BLOCK = re.compile(r"<lark-table\b.*?</lark-table>", re.I | re.S)
_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)


def _strip_feishu_tables(text: str) -> str:
    """剥掉飞书 markdown 的 XML 风格标记与整块表格，只保留正文。"""
    text = _LARK_TABLE_BLOCK.sub("\n", text)
    text = _MD_TABLE_ROW.sub("\n", text)
    text = _LARK_AT.sub(" ", text)
    text = _LARK_TAG.sub(" ", text)
    text = _LARK_ATTR.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    return text


def _fallback_strip_quoted_spans(text: str) -> str:
    lines = text.split("\n")
    quoted = [bool(re.match(r"^[ \t]{0,3}>[ \t]?", ln)) for ln in lines]
    out = []
    for ln, q in zip(lines, quoted):
        out.append("" if q else ln)
    return "\n".join(out)


def _fallback_clean_text(text: str) -> str:
    """字符层清洗：去零宽字符（非功能）、折叠同形异码、规范空格。"""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("Cf", "Cc") and ch not in "\t\n\r":
            continue
        if cat in ("Zl", "Zp"):
            out.append("\n")
            continue
        out.append(ch)
    return "".join(out)


def strip_markdown_furniture(text: str) -> str:
    """剥 markdown 装饰。

    先剥飞书的 XML 风格标记——上游不认识 <lark-table>，标签名（larktd、larktr）
    本身是英文，会污染 n-gram 并让表格内容被当成散文。
    """
    text = _strip_feishu_tables(text)
    if _UP:
        try:
            return _UP.strip_markdown_furniture(text)
        except Exception:
            pass
    return _fallback_strip_markdown_furniture(text)


def strip_quoted_spans(text: str) -> str:
    return _UP.strip_quoted_spans(text) if _UP else _fallback_strip_quoted_spans(text)


def clean_text(text: str) -> str:
    if _UP:
        text = _UP.clean_text(text, syntax="markdown")
    else:
        text = _fallback_clean_text(text)
    # 上游把汉字旁的 ZWSP 当「无空格文字的断行提示」保留（对泰文、老挝文成立）。
    # 中文按 UAX#14 任意两字之间都可断行，不需要 ZWSP——汉字旁的 ZWSP 几乎都是
    # 复制粘贴残渣或水印，这里补删。
    return _ZWSP_NEAR_HAN.sub("", text)


# 汉字或全角标点旁边的 ZWSP / 词连接符 / BOM。
_HAN_OR_FULLWIDTH = r"[㐀-䶿一-鿿豈-﫿\u3000-\u303f\uff00-\uffef]"
_ZWSP_NEAR_HAN = re.compile(
    rf"(?<={_HAN_OR_FULLWIDTH})[\u200b\u2060\ufeff]+|[\u200b\u2060\ufeff]+(?={_HAN_OR_FULLWIDTH})")


# 降级版不可见字符检测。只抓「scrub 会删掉的」那几类：零宽空格/连接符、
# 词连接符、BOM、软连字符。ZWJ 夹在两个 emoji 之间是功能性的（家庭/职业组合
# emoji），不算。
_INVISIBLE_FALLBACK = set("\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e\u2061\u2062\u2063\u2064")
_EMOJI_RE = re.compile(r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF]")


def detect_invisible(text: str) -> int:
    """数 scrub 会删掉的不可见字符。优先用上游（与 --clean 判据一致），
    再补上上游放过、但本 skill 的 clean_text 会删的「汉字旁 ZWSP」。"""
    if _UP and hasattr(_UP, "detect_invisible_chars"):
        try:
            n = int(_UP.detect_invisible_chars(text).get("count", 0))
            if not _ZWSP_NEAR_HAN.search(text):
                return n          # 快路径：上游 clean_text 逐字走，大文档很贵
            residual = _UP.clean_text(text, syntax="markdown")
            return n + sum(len(m.group(0)) for m in _ZWSP_NEAR_HAN.finditer(residual))
        except Exception:
            pass
    n = 0
    for i, ch in enumerate(text):
        if ch not in _INVISIBLE_FALLBACK:
            continue
        if ch == "\u200d" and 0 < i < len(text) - 1 and \
                _EMOJI_RE.match(text[i - 1]) and _EMOJI_RE.match(text[i + 1]):
            continue
        n += 1
    return n


# ---------------------------------------------------------------- 中文基础
_HAN = r"㐀-䶿一-鿿豈-﫿"
_HAN_RE = re.compile(rf"[{_HAN}]")
_EN_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_SENT_END = "。！？!?；;…"
_SENT_SPLIT_RE = re.compile(rf"(?<=[{_SENT_END}])")


def zh_count(text: str) -> int:
    """中文按字计，英文按词计，数字按个数计。"""
    return (len(_HAN_RE.findall(text))
            + len(_EN_RE.findall(text))
            + len(_NUM_RE.findall(text)))


def zh_split_sentences(text: str) -> list[str]:
    parts = _SENT_SPLIT_RE.split(text)
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    # 单行超长（无标点）也算一句，避免把整段当一句
    return out


def zh_split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def burstiness(sents: list[str]) -> dict:
    """句长变异系数（CV）。中文按字数。cv 越低越接近「节奏平」。

    最小句数门槛 5（2026-10-22 用户拍板方案 A）。原来 ≥3 句就判，实测在 3 句
    样本上不可靠：一篇 122 字文档 3 句 [44,41,37] 得 cv=0.071 被判单调；
    另一篇 3 句 [257,274,127]，127 与 274 差一倍多、人读起来节奏并不平，
    cv=0.297 仍压到 0.35 阈值下被判单调。3 个数据点算变异系数，均值稍偏
    CV 即翻倍或腰斩，无统计意义。
    """
    lengths = [zh_count(s) for s in sents if zh_count(s) > 0]
    if len(lengths) < 5:
        return {"sentences": len(lengths), "cv": None, "monotonous": False}
    mean = statistics.mean(lengths)
    cv = statistics.pstdev(lengths) / mean if mean else 0.0
    return {"sentences": len(lengths), "cv": round(cv, 3), "monotonous": cv < 0.35}


def paragraph_monotony(paras: list[str]) -> dict:
    lengths = [zh_count(p) for p in paras if zh_count(p) > 0]
    if len(lengths) < 4:
        return {"paragraphs": len(lengths), "cv": None, "monotonous": False}
    mean = statistics.mean(lengths)
    cv = statistics.pstdev(lengths) / mean if mean else 0.0
    return {"paragraphs": len(lengths), "cv": round(cv, 3), "monotonous": cv < 0.40}


def detect_opener_repetition(paras: list[str]) -> list[dict]:
    """段首重复。中文取前 4 字（英文取前 2 词）。

    只在**中文散文段落**上跑。说话人标签（对话逐字稿里的「罗：」「Gemini：」）
    和日期开头（章节按年份分）会天然重复，不构成 AI 味，故排除。
    """
    heads = []
    for p in paras:
        if _is_structured_line(p):
            continue
        h = re.sub(r"^[^\w㐀-䶿一-鿿]+", "", p)[:4]
        # 段首是说话人标签 / 日期 / 数字编号：不是散文开头
        if len(h) < 2:
            continue
        if _HAN_RE.match(h):
            # 纯数字或数字开头（日期、编号）——排除
            if re.match(r"^[\d一二三四五六七八九十]+", h):
                continue
            # 标签形态「名字：」——排除
            if re.match(r"^[㐀-䶿一-鿿]{1,4}[:：]", p.strip()[:6]):
                continue
        else:
            continue  # 英文/数字开头，不算中文段首重复
        heads.append(h)
    total = len(heads)
    if total < 4:
        return []
    seen: dict[str, int] = {}
    for h in heads:
        seen[h] = seen.get(h, 0) + 1
    hits = []
    for h, c in seen.items():
        if c >= 3 and c / total >= 0.30:
            hits.append({"opener": h, "count": c})
    return hits


def detect_degenerate_repetition(text: str) -> dict:
    """退化重复：同一长片段反复出现。

    与上游的差异（中文必需）：上游用 6 个**词**做 n-gram，所以英文技术串
    不会误伤。中文没有词边界，直接用 6 个**字**会误伤一切——「.googleapis.com」
    这类技术串重复 28 次就够触发。这里做三件事：

    1. 只保留**汉字、字母数字、空白**参与 n-gram。
       理由不只是技术串：飞书导出的 markdown 里混着 `<>{=""}` 这类合并字段
       残渣，把 ASCII 逐个剥掉会留下 `<>={}` 的标点渣滓，凑成假的 12 字
       重复片段——实测一份小米 SOW 因此被误判成「退化重复」，12 字片段
       重复 50 次（全是 `<><><><><=""`）。
    2. n-gram 长度取 12 字。
    3. 只在**散文段落**上跑（列表项、表格行是结构化数据，重复是正常的）。
    """
    paras = [p for p in zh_split_paragraphs(text) if not _is_structured_line(p)]
    scope = "\n\n".join(paras)
    # n-gram 只走**汉字**。
    # 理由：中文文档里常嵌大段英文专名（法律实体名、产品英文名），它们会
    # 重复出现但不是散文——实测一份 SOW 因英文实体名重复 4 次被判成「退化重复」。
    # 汉字流才代表中文散文本身。
    t = "".join(ch for ch in scope if "一" <= ch <= "鿿")
    n = 12
    if len(t) < n * 3:
        return {"repeated": 0, "ratio": 0.0, "max_count": 0}
    spans = [t[i:i + n] for i in range(len(t) - n + 1)]
    counts: dict[str, int] = {}
    for s in spans:
        counts[s] = counts.get(s, 0) + 1
    repeated = sum(c - 1 for c in counts.values() if c > 1)
    return {"repeated": repeated, "ratio": round(repeated / len(spans), 3),
            "max_count": max(counts.values()) if counts else 0}


def _is_structured_line(p: str) -> bool:
    """列表项 / 表格行 / 标题行：结构化数据，不参与散文统计。

    飞书表格的每个单元格在剥掉 <lark-td> 标签后，会变成一个**独立段落**。
    单元格内容形态多样：单个词（「版本号」）、编号列表（「1.皮肤分割\\n2.人像分割…」）、
    多行文本。判据是「短且不成句」或「整段都是编号项」。
    """
    for ln in p.split("\n"):
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("-", "*", "+", "|", ">", "#")) or re.match(r"^\d+[.)]\s", s):
            return True
        if s.startswith("`") or "|" in s:
            return True
    stripped = re.sub(r"\s+", "", p)
    if not stripped:
        return True
    # 整段都是编号项（表格单元格里装了一个列表）
    lines = [x.strip() for x in p.split("\n") if x.strip()]
    if len(lines) >= 2 and all(re.match(r"^\d+[.)]\s*\S", x) for x in lines):
        return True
    # 孤立的短行：表格单元格残留。没有句末标点且不超过 12 字。
    if len(stripped) <= 12 and not re.search(r"[。！？；，、,.!?;:]", stripped):
        return True
    return False


# ---------------------------------------------------------------- 机器痕迹
# 与语言无关，但在中文里同样是「机器写的」硬证据。
# label 必须是人话——报告会直接把 label 打给模型看，不能是 pattern key。
#
# 分两类：
#   decisive —— 自动化留下的硬痕迹（同形异码、跟踪参数、聊天标记、零宽字符）。
#               这些只有机器会留下，是 AI 味的最硬证据。
#   defect  —— 文档缺陷（占位符没填）。中文语境下极常见：会议纪要的待办表里
#               写「[待定，例如：10月27日]」是**人写的**工作文档，不是 AI 味。
#               它值得被报告（该填的没填），但不该触发 decisive 抬分底线。
_DECISIVE_RE = {
    "62_invisible_chars": (None, "零宽字符", "defect"),
    "63_placeholder_text": (
        re.compile(
            r"【[^】]{0,12}(?:待补|占位|TBD|xxx|XXX|某)[^】]{0,12}】|"
            r"\{\{[^}]{1,24}\}\}|"
            r"\[[^\]\n]{0,20}(?:待定|待补|占位|姓名|公司名|TBD)[^\]\n]{0,20}\]|"
            r"某某公司|某某某"
        ),
        "占位符残留",
        "defect",
    ),
    "64_chatbot_ref_markup": (
        # 聊天 UI 的引用标记漏进正文（对齐上游 #64）。不收「资料来源：」「[1] 来源：」
        # ——人写研报、论文里很常见，不是机器痕迹。
        re.compile(
            r"【\d+†[^】]*】|\bcite(?:turn|start|nav)\w*|\boai_citation\w*|"
            r"contentReference\[oaicite:\d+\]|\bcontentReference\b|\[\^\d+\^\]"
        ),
        "聊天引用标记泄漏",
        "decisive",
    ),
    "65_ai_tracking_params": (
        # 只抓**聊天 / AI 产品**带出来的跟踪参数。普通 utm（wechat、newsletter）
        # 是运营正常用法——曾经一律判 decisive，一条活动链接就把人写文档抬到 45 分。
        re.compile(
            r"utm_source=(?:chatgpt\.com|chat\.openai\.com|openai|chatgpt|perplexity(?:\.ai)?|"
            r"claude(?:\.ai)?|gemini(?:\.google\.com)?|copilot(?:\.microsoft\.com)?|poe(?:\.com)?|"
            r"deepseek|kimi|doubao|yuanbao|tongyi|qianwen)(?![\w])|[?&]from=chatgpt",
            re.I,
        ),
        "AI 跟踪参数",
        "decisive",
    ),
    # 聊天机器人残留（上游 #47）：交付物里留着对话口吻。精度极高。
    # 注意：「如果有任何疑问或改进建议，欢迎随时反馈」是人类通知常见结尾，
    # 必须要求「其他/进一步的问题」或「告诉我/向我提问」才算聊天残留。
    "47_chatbot_artifacts": (
        re.compile(
            r"好的[！!，,。]\s*(?:以下|下面|这是|我来|我将|为您|为你)|"
            r"(?:以下|下面)是(?:为您|为你|我为您|我为你)|"
            r"希望(?:这|以上|本文|这些)?(?:内容|信息|回答)?(?:能)?(?:对您|对你)(?:有所)?(?:帮助|启发)|"
            r"如果您?你?(?:还)?有(?:任何)?(?:其他|其它|进一步的)(?:问题|疑问|需要)|"
            r"欢迎随时(?:告诉|向我|问我|提问)|"
            r"当然可以[！!]|"
            r"作为一[个名](?:AI|人工智能)(?:语言模型|助手|模型)?|我是一[个名](?:AI|人工智能)"
        ),
        "聊天机器人残留",
        "decisive",
    ),
    # 知识截止声明（上游 #50）。
    "50_cutoff_disclaimer": (
        re.compile(
            r"截至我的(?:知识|训练)(?:截止|更新)|我的(?:知识|训练数据)(?:截止|只到|更新至)|"
            r"(?:在|截至)我(?:最后一次)?(?:训练|知识更新)(?:时|的时候)"
        ),
        "知识截止声明",
        "decisive",
    ),
    "66_homoglyphs": (None, "同形异码字符", "decisive"),
}


# ---------------------------------------------------------------- 中文词表
# 每条：family -> (label, 正则, 每千字密度阈值, 权重档)
#   权重档 decisive(3.0) / strong(1.6) / soft(1.0)
#   阈值来自实测校准：中文人写文本 ≤2.4/千字，模型生成 ≥8/千字。
_ZH_PATTERNS: dict[str, tuple[str, str, float, str]] = {
    # --- 词汇：空洞大词 ---
    "1_ai_bigwords": (
        "空洞大词",
        r"赋能|闭环|底层逻辑|认知升级|长期主义|关键抓手|价值沉淀|深度融合|"
        r"多维度|全方位|体系化|精细化|势能|颗粒度|组合拳|心智|场域|"
        r"一体化|平台化|数字化转型|深层次|深度赋能|精准赋能|全新升级|"
        r"全新打造|全新定义|重新定义|全新范式|生态闭环|能力沉淀|知识体系重塑",
        3.0, "soft"),
    # --- 词汇：万能动词（密度阈值调高：人写也常用）---
    # 「赋能」「全方位」已在 1_ai_bigwords，不在这里重复计（否则一个词算两族）。
    "2_universal_verbs": (
        "万能动词",
        r"推进|助力|打造|构建|深化|夯实|筑牢|护航|聚焦|锚定|统筹|"
        r"协同|整合|抓手|持续发力|保驾护航|全链路",
        4.5, "soft"),
    # --- 修辞：意义拔高 ---
    "3_significance_inflation": (
        "意义拔高",
        r"具有里程碑意义|开启了?新的篇章|注入了?新的活力|增添了浓墨重彩的一笔|"
        r"书写了?新的篇章|划时代的|具有深远意义|具有重大意义|意义重大|"
        r"至关重要|不可或缺|举足轻重|具有里程碑式的|全新里程碑",
        1.2, "strong"),
    # --- 修辞：公式句式（不是X而是Y）---
    "5_formula_frame": (
        "公式句式",
        r"不是[^，。；！？\n]{1,24}而是|既[^，。；！？\n]{1,24}又[^，。；！？\n]{1,24}|"
        r"不仅[^，。；！？\n]{1,24}(?:而且|还|更)|"
        r"不仅仅?是[^，。；！？\n]{1,24}更是[^，。；！？\n]{1,24}",
        2.0, "strong"),
    # --- 修辞：空洞承诺 ---
    "8_empty_promise": (
        "空洞承诺",
        r"将持续(?:努力|投入|推进)|必将|势必|一定会(?:带来|实现|达成)|"
        r"我们相信[^，。；\n]{2,30}(?:能够|会)(?:实现|达成|带来)|"
        r"让我们(?:一起)?(?:拭目以待|共同期待)|未来将(?:持续|不断)|"
        r"未来(?:将|会)?(?:更加|愈发)",
        1.2, "strong"),
    # --- 结构：万能开头 ---
    "9_formula_opener": (
        "万能开头",
        r"(?:众所周知|在这个[^，。\n]{2,16}的时代|随着[^，。\n]{2,18}的[^，。\n]{2,10}"
        r"(?:飞速|快速|持续|不断|日新月异)发展)|"
        r"(?:在当今(?:这个)?[^，。\n]{0,10}(?:的时代|社会|背景下))|"
        r"(?:在这个信息爆炸的时代)",
        0.8, "strong"),
    # --- 语域：情感扁平 ---
    "10_flat_emotion": (
        "情感扁平",
        r"感到(?:一阵|一股|强烈的)?(?:深深的|莫名的)?(?:悲伤|愤怒|喜悦|感动|震撼|触动)|"
        r"内心(?:深处)?(?:充满了|涌动着)[^，。；\n]{1,8}|"
        r"心中(?:涌起|泛起)[^，。；\n]{1,8}|五味杂陈|百感交集|思绪万千|感慨万千",
        0.8, "soft"),
    # --- 证据：无来源引用 ---
    "11_unsourced_evidence": (
        "无来源证据",
        # 「据研究/据统计/据调查」是无来源断言标记。必须加负向后顾，否则
        # 「数据统计」里的子串「据统计」会被命中——实测《0514阶跃合作议题讨论》
        # 因「AGE数据统计」被判「无来源证据」。
        r"(?<![数依根证字单])据(?:统计|调查|研究)|"
        r"(?:大量|大量)(?:实践证明|研究表明|调查显示|统计显示)|"
        r"相关(?:研究|数据|报告|调查)(?:表明|显示|证明)|"
        r"有(?:数据|研究)(?:表明|显示|证明)|"
        r"实践(?:证明|表明)了",
        1.0, "strong"),
    # --- 翻译腔 ---
    "6_translationese": (
        "翻译腔",
        r"进行了?(?:一次|了?一?次)?[^，。；\n]{0,12}的?(?:探讨|分析|研究|讨论|梳理|总结|回顾|评估|优化|整合|升级)|"
        r"实现了?[^，。；\n]{0,12}的?(?:落地|突破|提升|增长|覆盖)|"
        r"对[^，。；\n]{1,15}进行了[^，。；\n]{1,12}|"
        r"在[^，。；\n]{1,12}方面(?:上)?(?:具有|拥有)|"
        r"(?:通过|以)[^，。；\n]{1,12}的(?:方式|手段|形式)",
        3.5, "soft"),
    # --- 公文腔 ---
    "7_bureaucratic": (
        "公文腔",
        r"切实(?:加强|推进|做好|落实)|着力(?:打造|构建|推动|加强)|"
        r"持续(?:用力|发力|推进)|抓紧(?:抓实抓好)|深入(?:推进|落实|开展)|"
        r"严格按照|全面(?:落实|提升|加强)|扎实(?:推进|做好|开展)|"
        r"广泛(?:开展|动员)|形成(?:合力|共识|闭环)",
        3.0, "soft"),
    # --- 词汇：空洞抽象名词（中文特有）---
    # 「真实的场景的深刻洞察」——连续抽象名词 + 的，是中文 AI 腔的高频结构。
    # 实测密度：AI 文本 ≥4/千字，中文人写 ≤0.8/千字。
    # 「真实的场景的深刻洞察」——**连续两个「的」**的抽象名词链，是中文 AI 腔的
    # 高频结构。只要一个「的」（「学习能力」「团队的协作机制」）是日常话，
    # 曾经因此误报，故收紧。
    "15_abstract_noun_chain": (
        "空洞抽象名词",
        r"[一-鿿]{2,6}的[一-鿿]{2,6}的[一-鿿]{0,4}(?:洞察|能力|机制|价值|意识|思维|心态|认知|壁垒|体系)|"
        r"(?:深刻|全面|系统|独特)(?:的)?(?:洞察|理解|思考|把握)",
        2.5, "strong"),
    # --- 修辞：虚假权威引用 ---
    # 「正如某位资深产品经理所言」——无来源的权威背书。
    # 光秃秃的「正如」不算（「正如上次会议定的那样」是正常回指）。
    "16_false_authority": (
        "虚假权威引用",
        r"正如[^，。\n]{1,24}(?:所言|所说|所讲|指出的那样)|"
        r"有(?:专家|学者|大师|资深[^，。\n]{0,6})[^，。\n]{0,10}(?:指出|表示|认为|强调)",
        1.0, "strong"),
    # --- 结构：空洞动作短语 ---
    # 「反复验证」「不断打磨」「沉淀方法论」——只有姿态没有动作。
    # 「深度融合」已在 1_ai_bigwords，不重复。附近有数字的命中按具体陈述豁免。
    "17_hollow_action": (
        "空洞动作",
        r"反复验证|不断打磨|持续优化|持续打磨|打破壁垒|"
        r"沉淀(?:方法论|经验|能力|资产)|降低[^，。；\n]{0,8}成本|"
        r"(?:真正|切实|有效)(?:地)?(?:提升|增强|优化|改善)",
        3.0, "soft"),
    # --- 结构：连接词堆砌（枚举型，需上下文判定）---
    # 计数只走 _ENUM_CHAIN（首先…其次…最后成串）；正则在此仅作文档。
    "4_connector_stacking": (
        "连接词堆砌",
        r"首先|其次|再次|最后",
        3.0, "soft"),
    # --- 结构：套话连接词（单个即算，按密度）---
    # 原先列在 4_connector_stacking 里但从未被计数（死代码）。
    # 不收「需要注意的是」——在技术规格书里是正常的注意事项提示。
    "18_discourse_filler": (
        "套话连接词",
        r"值得(?:注意|一提|关注)的是|由此可见|不难(?:看出|发现)|"
        r"毋庸置疑|显而易见|不可否认|换言之|换句话说|更重要的是|"
        r"总而言之|综上所述|总的来说|总体而言",
        1.5, "strong"),
    # --- 修辞：否定式反转（上游 #17）---
    # 阈值与 5_formula_frame 对齐取 2.0：长文档里偶发一次「不在于…而在于…」
    # 是正常对比，短文里密集出现才是套路。
    "19_negation_reversal": (
        "否定式反转",
        r"真正的[^，。；！？\n]{1,12}(?:不是|不在于|从来不是)|"
        r"这不是[^，。；！？\n]{1,12}[，,]\s*(?:而是|是)|"
        r"不在于[^，。；！？\n]{1,16}[，,]\s*而在于|"
        r"与其说[^，。；！？\n]{1,16}[，,]\s*不如说",
        2.0, "strong"),
    # --- 词汇：近年大模型中文腔 ---
    # 「一句话总结：」带冒号是飞书文档常用字段标签，已用负向前瞻排除。
    "20_llm_buzz": (
        "大模型腔词汇",
        r"说白了|一句话(?:总结|概括)(?![：:])|核心在于|关键在于|破局|深耕|护城河|一站式|"
        r"降本增效|沉浸式|硬核|不止于|重塑|无缝(?:衔接|对接|集成)?|"
        r"飞轮|第一性原理|范式转移|降维打击",
        3.0, "soft"),
    # --- 结构：预告与路标（上游 #31）---
    "31_signposting": (
        "预告与路标",
        r"本文将(?:从|围绕|介绍|探讨|分析|详细)|"
        r"接下来[，,]?\s*(?:我们|我将|让我们)(?:来)?(?:看|介绍|详细|深入|聊)|"
        r"下面(?:我们|我将|就)(?:来)?(?:详细)?(?:介绍|看看|分析|展开)|"
        r"让我们(?:一起)?(?:来)?(?:深入)?(?:探讨|看看|了解|探索)|"
        r"(?:从以下|分以下)(?:几个|[一二三四五六七八九十\d]+个)(?:方面|维度|角度)",
        1.0, "strong"),
    # --- 结构：程式化结尾（上游 #38/#39）---
    "38_formula_closing": (
        "程式化结尾",
        r"希望本文(?:能|可以|对)|希望(?:以上|这些)(?:内容|建议|分享)|让我们携手|"
        r"携手共(?:进|创|建)|共同(?:开创|迎接|创造)(?:更加)?美好(?:的)?未来|未来可期",
        1.0, "strong"),
    # --- 语域：谄媚（上游 #48）---
    "48_sycophantic": (
        "谄媚语气",
        r"这是一个(?:非常|很|特别)?(?:好|棒|有意思)的问题|(?:好|问得)(?:非常|很)?好(?:的)?问题|"
        r"您说得(?:非常|很|完全)?(?:对|有道理)|您的(?:想法|观点)(?:非常|很)(?:棒|好|有见地)",
        1.0, "strong"),
}

# 枚举型连接词：必须成串出现才算数（孤立一个「最后」不算 AI 味）
_ENUM_CHAIN = re.compile(
    r"首先[^。！？\n]{0,80}?(?:其次|再次|然后|最后)[^。！？\n]{0,80}?"
    r"(?:再次|然后|最后)|"
    r"(?:首先|其次|再次|最后)[、，,]\s*"
    r"(?:其次|再次|然后|最后)")

# report-only：中文正常用法，不进分数
_ZH_REPORT_ONLY = {
    "13_em_dash": ("破折号", re.compile(r"——")),
    "14_paren_aside": ("括号插入语", re.compile(r"（[^）]{0,24}如[^）]{0,24}）")),
    "12_quote_coining": ("引号造词", re.compile(r"[“\"][^”\"]{1,8}(?:式|型|化|主义|论|理念|思维|范式|路径|策略)[”\"]")),
}

# 具体性豁免：命中片段前后 ±12 字内有数字/量词单位，说明句子托着具体事实
# （「降低了服务器成本，每月省 3000 元」「实现了 32% 的增长」），不计为空洞。
_SPECIFICITY_EXEMPT = {"6_translationese", "17_hollow_action", "2_universal_verbs"}
_SPECIFIC_RE = re.compile(r"\d|[%％‰]|[一二两三四五六七八九十百千万]+(?:个|次|项|人|天|周|月|年|倍|元|条|篇|款)")
_SPECIFIC_WINDOW = 12


def _is_specific(prose: str, m: re.Match) -> bool:
    s = max(0, m.start() - _SPECIFIC_WINDOW)
    e = min(len(prose), m.end() + _SPECIFIC_WINDOW)
    return bool(_SPECIFIC_RE.search(prose[s:e]))


# 对冲堆叠（上游 #54）：同一句里 ≥2 个**不同的**对冲词。
# 不收「通常/往往/一般」——那是频率陈述，不是对冲。
_HEDGE_RE = re.compile(
    r"可能|或许|也许|大概|大致|似乎|仿佛|一定程度上|某种程度上|某种意义上|"
    r"相对而言|相对来说|基本上|或多或少|多多少少|在某些情况下")

# 格式类：必须在剥 markdown **之前**扫（剥完就看不到了）。
_BOLD_RE = re.compile(r"\*\*[^*\n]{1,60}\*\*")
_INLINE_HEADER_RE = re.compile(
    r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)?\*\*[^*\n]{1,24}\*\*\s*[：:]", re.M)


def detect_hedge_stack(sents: list[str]) -> list[str]:
    out = []
    for s in sents:
        found = set(_HEDGE_RE.findall(s))
        if len(found) >= 2:
            out.append(s[:60])
    return out


def detect_dash_heavy_paragraphs(text: str) -> int:
    """单行 ≥3 处「——」的行数。成对插入语（2 处）是中文正常用法，不计；
    按单行而非 `\\n\\n` 切，避免 `.docx` 单换行拼接时把整篇算成一段。"""
    return sum(1 for ln in text.split("\n") if ln.count("——") >= 3)


def _strip_code(text: str) -> str:
    text = _strip_feishu_tables(text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", " ", text)


# ---------------------------------------------------------------- 扫描
def scan(raw_text: str) -> dict:
    prose = strip_markdown_furniture(strip_quoted_spans(raw_text))
    # 机器痕迹 / 格式类在「去掉代码块的原文」上扫：链接 URL、加粗、emoji
    # 在剥 markdown 之后就看不到了。
    raw_nocode = _strip_code(strip_quoted_spans(raw_text))
    chars = max(zh_count(prose), 1)
    per_k = chars / 1000.0

    result: dict = {}
    fired: dict[str, dict] = {}

    # --- 词表 pattern（密度判定）---
    for key, (label, pat, thresh, tier) in _ZH_PATTERNS.items():
        if key == "4_connector_stacking":
            hits = list(_ENUM_CHAIN.finditer(prose))
        else:
            rx = re.compile(pat)
            hits = list(rx.finditer(prose))
            if key in _SPECIFICITY_EXEMPT:
                hits = [m for m in hits if not _is_specific(prose, m)]
        count = len(hits)
        density = count / per_k
        if count == 0:
            continue
        # 判定只看密度。
        # 曾经有个「绝对次数 ≥3 也放行」的例外，那是个 bug：2000 字的周报规范
        # 里「闭环」出现 3 次（密度 1.5）就被判成 AI 味，而那本就是人写的规范
        # 文档，「闭环」在那里是术语。短文本已经由 count==0 之外的密度天然保护
        # ——200 字里 1 次就是密度 5，自然超阈值。
        if density < thresh:
            continue
        samples = []
        for m in hits[:3]:
            s = max(0, m.start() - 18)
            e = min(len(prose), m.end() + 18)
            samples.append(prose[s:e].replace("\n", " "))
        fired[key] = {"label": label, "count": count, "density": round(density, 2),
                      "tier": tier, "samples": samples}

    # --- report-only ---
    for key, (label, rx) in _ZH_REPORT_ONLY.items():
        n = len(rx.findall(prose))
        if n:
            fired[key] = {"label": label, "count": n, "density": round(n / per_k, 2),
                          "tier": "report", "samples": []}

    # --- 机器痕迹 ---
    for key, (rx, label, tier) in _DECISIVE_RE.items():
        if rx is None:
            continue
        ms = list(rx.finditer(raw_nocode))
        if ms:
            fired[key] = {"label": label, "count": len(ms), "density": round(len(ms) / per_k, 2),
                          "tier": tier,
                          "samples": [raw_nocode[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")
                                      for m in ms[:3]]}
    inv = detect_invisible(raw_nocode)
    if inv:
        # 飞书富文本导出器在加粗/括号边界（如 `**1.1 项目背景**\u200b：`）会常驻注入
        # U+200B，因此与占位符一样归入 defect：照常检测报告并由 --clean 清除，
        # 但不触发 decisive 抬分底线，避免把飞书导出文档误判为 AI 腔。
        fired["62_invisible_chars"] = {"label": "零宽字符", "count": inv,
                                       "density": round(inv / per_k, 2),
                                       "tier": "defect", "samples": []}
    if _UP:
        try:
            hom = _UP.detect_homoglyphs(prose)
            if hom:
                fired["66_homoglyphs"] = {"label": "同形异码字符", "count": len(hom),
                                           "density": round(len(hom) / per_k, 2),
                                           "tier": "decisive", "samples": hom[:3]}
        except Exception:
            pass

    # --- 统计类 ---
    sents = zh_split_sentences(prose)
    burst = burstiness(sents)
    if burst.get("monotonous"):
        fired["40_sentence_monotony"] = {"label": "句长单调", "count": 1,
                                          "density": 1.0, "tier": "rhythm",
                                          "samples": [f"cv={burst['cv']}"]}
    paras = zh_split_paragraphs(prose)
    pm = paragraph_monotony(paras)
    if pm.get("monotonous"):
        fired["44_uniform_paragraph"] = {"label": "段落长度一致", "count": 1,
                                          "density": 1.0, "tier": "rhythm",
                                          "samples": [f"pcv={pm['cv']}"]}
    for o in detect_opener_repetition(paras):
        fired.setdefault("42_opener_repetition", {
            "label": "段首重复", "count": o["count"], "density": round(o["count"] / max(len(paras), 1) * 1000, 2),
            "tier": "rhythm", "samples": [o["opener"]]})
    # --- 对冲堆叠（上游 #54，对齐上游归入 report-only，供改写时删减，不计分）---
    hedges = detect_hedge_stack(sents)
    if hedges and len(hedges) / per_k >= 1.0:
        fired["54_hedge_stack"] = {"label": "对冲堆叠", "count": len(hedges),
                                   "density": round(len(hedges) / per_k, 2),
                                   "tier": "report", "samples": hedges[:3]}

    # --- 破折号过多：单行 ≥3 处（只报告不计分，对齐 SKILL.md 破折号口径）---
    dash_lines = detect_dash_heavy_paragraphs(prose)
    if dash_lines:
        fired["58_em_dash_overuse"] = {"label": "破折号过多", "count": dash_lines,
                                       "density": round(dash_lines / per_k, 2),
                                       "tier": "report", "samples": []}

    # --- 格式类（上游 #33 / #56 / #57），在去表格/代码块的原文上扫 ---
    # 中文飞书业务文档（SOW、PRD）大量使用加粗字段名与手势/状态 emoji，
    # 故归入 report（报告供改写参考，不直接计入分数，避免误伤业务文档）。
    inline_hdr = _INLINE_HEADER_RE.findall(raw_nocode)
    if len(inline_hdr) >= 3:
        fired["33_inline_header_list"] = {"label": "加粗小标题列表", "count": len(inline_hdr),
                                          "density": round(len(inline_hdr) / per_k, 2),
                                          "tier": "report", "samples": [s.strip() for s in inline_hdr[:3]]}
    # 标题行里的加粗是飞书导出习惯（`# **产品名称**`），不算正文加粗。
    body = "\n".join(ln for ln in raw_nocode.split("\n") if not re.match(r"^\s{0,3}#", ln))
    bolds = _BOLD_RE.findall(body)
    if len(bolds) >= 4 and len(bolds) / per_k >= 8.0:
        fired["56_boldface"] = {"label": "加粗过多", "count": len(bolds),
                                "density": round(len(bolds) / per_k, 2),
                                "tier": "report", "samples": bolds[:3]}
    emojis = _EMOJI_RE.findall(raw_nocode)
    if len(emojis) >= 2:
        fired["57_emojis"] = {"label": "表情符号", "count": len(emojis),
                              "density": round(len(emojis) / per_k, 2),
                              "tier": "report", "samples": list(dict.fromkeys(emojis))[:3]}

    deg = detect_degenerate_repetition(prose)
    # 「同一片段反复出现」有两种：模型卡壳（同一句连续重复很多次）和
    # 正常文档里同一个标签被反复引用（每个片段只出现 2 次）。
    # 用 max_count 区分：真退化重复的片段会连续出现 4 次以上。
    if deg["ratio"] > 0.20 and deg.get("max_count", 0) >= 4:
        fired["71_degenerate_repetition"] = {"label": "退化重复", "count": deg["repeated"],
                                             "density": round(deg["ratio"], 3),
                                             "tier": "decisive", "samples": []}

    score = ai_tell_score(fired, chars, burst, pm)
    fired["_metrics"] = {
        "zh_chars": chars,
        "sentences": len(sents),
        "paragraphs": len(paras),
        "length_cv": burst.get("cv"),
        "paragraph_cv": pm.get("cv"),
        "ai_tell_score": score["score"],
        "ai_tell_band": score["band"],
        "patterns_flagged": len([k for k in fired if not k.startswith("_")]),
    }
    fired["_metrics"].update(score_confidence(chars, len([
        k for k in fired if not k.startswith("_") and fired[k].get("tier") not in ("report",)])))
    return fired


# ---------------------------------------------------------------- 打分
_TIER_WEIGHT = {"decisive": 3.0, "strong": 1.6, "soft": 1.0, "rhythm": 1.0, "defect": 0.0}
_TIER_DIVERSITY = {"decisive": 16.0, "strong": 10.0, "soft": 2.5, "rhythm": 5.0, "defect": 0.0}

_CONTENT_CAP = 48.0
_RHYTHM_CAP = 18.0
_VOCAB_CAP = 20.0
_SCORE_MIN_CHARS = 150


def _band(score: float) -> str:
    if score <= 20:
        return "clean"
    if score <= 40:
        return "light tells"
    if score <= 60:
        return "mixed"
    if score <= 80:
        return "heavy tells"
    return "pervasive tells"


def ai_tell_score(fired: dict, chars: int, burst: dict, pm: dict) -> dict:
    eff = max(chars, _SCORE_MIN_CHARS)
    diversity = 0.0
    content_load = 0.0
    families: set[str] = set()
    rhythm_flags = 0

    for key, val in fired.items():
        if key.startswith("_") or not isinstance(val, dict):
            continue
        tier = val.get("tier", "soft")
        if tier in ("report", "defect"):
            continue          # report 只提示；defect 是文档缺陷，不进分数
        family = key.split("_", 1)[0]
        if family in families:
            continue          # 每族只计一次（对齐上游 diversity 逻辑）
        families.add(family)
        if tier == "rhythm":
            rhythm_flags += 1
            continue
        count = min(val.get("count", 1), 5)
        content_load += _TIER_WEIGHT.get(tier, 1.0) * count
        diversity += _TIER_DIVERSITY.get(tier, 5.0)

    density_bonus = min(10.0, content_load / eff * 1000)
    content_term = min(_CONTENT_CAP, diversity + density_bonus)

    cv = burst.get("cv")
    burst_term = 0.0 if cv is None else max(0.0, min(1.0, (0.45 - cv) / 0.45)) * 10.0
    pcv = pm.get("cv")
    mono_term = 0.0 if pcv is None else max(0.0, min(1.0, (0.4 - pcv) / 0.4)) * 8.0
    rhythm_term = min(_RHYTHM_CAP, rhythm_flags * 4.0 + burst_term + mono_term)

    vocab_hit = fired.get("1_ai_bigwords", {})
    vocab_density = vocab_hit.get("density", 0.0)
    vocab_term = min(_VOCAB_CAP, max(0.0, vocab_density - 3.0) * 1.8)

    score = round(min(100.0, content_term + rhythm_term + vocab_term))

    decisive_fams = {k.split("_", 1)[0] for k, v in fired.items()
                     if not k.startswith("_") and v.get("tier") == "decisive"}
    if len(decisive_fams) >= 2:
        score = max(score, 65)
    elif len(decisive_fams) == 1:
        score = max(score, 45)

    if chars < _SCORE_MIN_CHARS:
        if len(families) >= 3:
            score = max(score, 45)
        elif len(families) >= 2:
            score = max(score, 25)
    elif chars >= 800:
        if len(families) >= 5:
            score = max(score, 45)
        elif len(families) >= 3:
            score = max(score, 25)
    return {"score": score, "band": _band(score)}


_CONF_FLOOR = 300
_CONF_SOLID = 800


def score_confidence(chars: int, families: int) -> dict:
    """分数可信度。中文阈值按字数折算（人写 300 字 = 英文 120 词量级）。"""
    if chars < 120:
        return {"confidence": "none",
                "confidence_reason": f"{chars} 字：太短，一个短语就能翻档，数字当噪声看。"}
    if chars < _CONF_FLOOR:
        return {"confidence": "low",
                "confidence_reason": f"{chars} 字：低于 { _CONF_FLOOR } 字地板，一个短语就能移动档位。"}
    if chars < _CONF_SOLID and families <= 1:
        return {"confidence": "low",
                "confidence_reason": f"{chars} 字且只有 {families} 类命中：证据不足。"}
    if chars < _CONF_SOLID:
        return {"confidence": "moderate",
                "confidence_reason": f"{chars} 字：可用，但到 { _CONF_SOLID } 字分数才稳。"}
    return {"confidence": "high", "confidence_reason": ""}


# ---------------------------------------------------------------- 格式提取
def extract_text(path: Path) -> tuple[str, str]:
    """返回 (文本, 语法)。优先用上游的 read_source（覆盖 docx/epub/odt/ipynb）。"""
    if _UP:
        try:
            text, newline = _UP.read_source([str(path)])
            return text, "markdown"
        except Exception:
            pass
    suffix = path.suffix.lower()
    if suffix == ".ipynb":
        try:
            import json as _json

            nb = _json.loads(path.read_text(errors="ignore"))
            cells = [c.get("source", "") for c in nb.get("cells", [])
                     if c.get("cell_type") == "markdown"]
            return "\n\n".join("".join(c) for c in cells), "markdown"
        except Exception:
            return path.read_text(errors="ignore"), "markdown"
    if suffix in (".docx", ".docm", ".pptx", ".pptm", ".xlsx",
                  ".xlsm", ".odt", ".odp", ".ods", ".epub"):
        try:
            return _extract_office(path), "markdown"
        except Exception:
            return "", "markdown"
    return path.read_text(errors="ignore"), "markdown"


def _extract_office(path: Path) -> str:
    members = {
        ".docx": ("word/document.xml",), ".docm": ("word/document.xml",),
        ".pptx": ("ppt/slides/slide",), ".pptm": ("ppt/slides/slide",),
        ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/sheet"),
        ".xlsm": ("xl/sharedStrings.xml", "xl/worksheets/sheet"),
        ".odt": ("content.xml",), ".odp": ("content.xml",), ".ods": ("content.xml",),
        ".epub": (".xhtml", ".html"),
    }[path.suffix.lower()]
    out = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if any(name.endswith(m) or m in name for m in members):
                xml = z.read(name).decode("utf-8", "ignore")
                xml = re.sub(r"</w:p>|</text:p>|<text:p\b[^>]*>|</text:p>", "\n", xml)
                txt = re.sub(r"<[^>]+>", "", xml)
                txt = (txt.replace("&amp;", "&").replace("&lt;", "<")
                          .replace("&gt;", ">").replace("&quot;", '"'))
                if txt.strip():
                    out.append(txt.strip())
    return "\n\n".join(out)


# ---------------------------------------------------------------- CLI
def main() -> int:
    args = sys.argv[1:]
    clean = "--clean" in args
    args = [a for a in args if a != "--clean"]
    if args:
        path = Path(args[0])
        text, _syn = extract_text(path)
    else:
        text = sys.stdin.buffer.read().decode("utf-8", "replace")
    if clean:
        sys.stdout.write(clean_text(text))
        return 0
    result = scan(text)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
