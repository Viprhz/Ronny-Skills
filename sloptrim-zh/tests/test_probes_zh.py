#!/usr/bin/env python3
"""探针回归测试：每条对应一次实测发现的 bug 或覆盖缺口（2026-10-22 评审）。

跑法（零依赖，也兼容 pytest）：
    python3 test_probes_zh.py        # 逐条打印 PASS/FAIL，有失败退出码 1
    pytest test_probes_zh.py

每条探针只断言**它要守的那一件事**（某个 key 命中 / 不命中、分数跨没跨档），
不锁死具体分数——阈值重校准时不该让这些用例无谓地变红。
改检测器后与 validation_corpus_zh.py 一起跑：那个守「分离」，这个守「具体行为」。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "detect_zh.py"
_spec = importlib.util.spec_from_file_location("detect_zh", _SCRIPT)
dz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dz)

# 人写基线：真实口吻、无 AI 腔、不重复（约 600 字），用来托住单个探针短语，
# 让密度落在真实文档的量级，而不是 30 字里一次就爆表。
BASE = """上周三下午，产品组六个人挤在会议室里吵了两个钟头。会议室订的是三楼那间，投影仪线又短，只有一半人能看见屏幕。

张伟把打印出来的流程图贴在白板上，用红笔圈了三个地方。他说这三个地方要改，但没说改哪三个，得先问客户端。

午饭的时候没人去食堂。老陈点了两份外卖，一份放在张伟桌上，自己端着另一份蹲在走廊尽头吃。他说这种事急不来，改错一个字段，客户端那边要重刷一遍。

弱电箱在客厅，箱内是光纤入户点。书房的网线从弱电箱直连，沿墙走地面再上墙，中途经过餐厅。实测速率 940 Mbps，跑满 5 分钟不掉线。主卧接的是面板 AP，客卧没有网口，只能用 Mesh 子节点凑合。

我不喜欢写周报。写完交上去，然后就忘了。后来发现周报其实是给自己看的：过三个月回头翻，能想起来当时在忙什么。真要改，只改一件事——先写做了什么，再写想做什么。

下午三点多结论出来了：三个地方都改，但分两次上线，先改第一和第三个。没人问为什么。散会的时候老陈还在看那张图，手里的笔一直没盖上。"""


def scan(text: str) -> dict:
    return dz.scan(text)


def keys(r: dict) -> set[str]:
    return {k for k in r if not k.startswith("_")}


def score(r: dict) -> int:
    return r["_metrics"]["ai_tell_score"]


# ---------------------------------------------------------------- 基线
def test_p00_baseline_human_is_clean():
    r = scan(BASE)
    assert r["_metrics"]["ai_tell_band"] == "clean", r


# ---------------------------------------------------------------- P0 bug
def test_p01_zero_width_detected():
    r = scan(BASE.replace("会议室", "会议\u200b室", 2).replace("周报", "周\u200d报", 1))
    assert "62_invisible_chars" in keys(r)
    assert r["62_invisible_chars"]["count"] >= 3


def test_p01b_emoji_zwj_sequence_not_invisible():
    # 👨‍💻 里的 ZWJ 是功能性的，不算零宽字符
    r = scan(BASE + "\n\n收工 👨\u200d💻。")
    assert "62_invisible_chars" not in keys(r)


def test_p02_bare_zhengru_not_false_authority():
    r = scan("正如上次会议定的那样，下周二上线。\n\n" + BASE)
    assert "16_false_authority" not in keys(r)


def test_p02b_real_false_authority_still_fires():
    r = scan("正如某位资深产品经理所言：好的产品是长出来的。\n\n" + BASE)
    assert "16_false_authority" in keys(r)


def test_p03_ordinary_utm_not_decisive():
    base = score(scan(BASE))
    r = scan("活动页链接 https://a.com/x?utm_source=wechat&utm_medium=group 已发群里。\n\n" + BASE)
    assert "65_ai_tracking_params" not in keys(r)
    assert score(r) <= base + 5


def test_p03b_ai_utm_fires():
    r = scan("参考 [这篇](https://example.com/a?utm_source=chatgpt.com) 的做法。\n\n" + BASE)
    assert "65_ai_tracking_params" in keys(r)


def test_p04_discourse_filler_counted():
    t = ("值得注意的是，这个方案很好。毋庸置疑，它能解决问题。综上所述，我们应该采用它。"
         "不难看出，团队很支持。显而易见，效果会很好。")
    r = scan(t + "\n\n" + BASE)
    assert "18_discourse_filler" in keys(r)


def test_p05_chatbot_residue_and_formatting():
    t = ("好的！以下是为您整理的周报内容。\n\n"
         "- **本周进展**：完成了接口联调。\n"
         "- **下周计划**：上线灰度。\n"
         "- **风险**：后端人手不足。\n\n"
         "🚀 进度正常 ✅ 风险可控\n\n"
         "希望这对您有帮助！如果您还有其他问题，欢迎随时告诉我。")
    r = scan(t)
    k = keys(r)
    assert "47_chatbot_artifacts" in k
    assert "33_inline_header_list" in k
    assert "57_emojis" in k
    assert score(r) >= 45


def test_p06_concrete_cost_cut_not_hollow():
    r = scan("把缓存换成本地内存，降低了服务器成本，每月省 3000 元。\n\n" + BASE)
    assert "17_hollow_action" not in keys(r)


def test_p06b_hollow_action_without_specifics_fires():
    t = "这种洞察需要在大量实践中反复验证、不断打磨，持续优化，沉淀方法论。"
    r = scan(t * 2 + "\n\n" + BASE[:200])
    assert "17_hollow_action" in keys(r)


def test_p07_modern_llm_slop_not_clean():
    t = ("说白了，这件事的本质是信任。真正的问题不在于技术，而在于组织。"
         "一句话总结就是先跑通再优化。核心在于，我们要深耕用户痛点，构建护城河，"
         "打造一站式解决方案，实现降本增效。这不是终点，而是起点。")
    r = scan(t)
    assert r["_metrics"]["ai_tell_band"] != "clean", r
    assert {"19_negation_reversal", "20_llm_buzz"} <= keys(r)


def test_p08_concrete_growth_not_translationese():
    r = scan("三季度实现了 32% 的增长，主要靠海外渠道。我们对登录接口进行了压测，QPS 到 1200 才开始掉。\n\n" + BASE)
    assert "6_translationese" not in keys(r)


def test_p09_single_de_not_abstract_chain():
    r = scan("这个人的学习能力很强，团队的协作机制也比较顺。\n\n" + BASE)
    assert "15_abstract_noun_chain" not in keys(r)


def test_p09b_abstract_chain_still_fires():
    r = scan("产品的核心竞争力，来源于对用户真实场景的深刻洞察。\n\n" + BASE[:150])
    assert "15_abstract_noun_chain" in keys(r)


def test_p10_hedge_stack():
    r = scan("这可能在一定程度上或许会大致影响进度。\n\n" + BASE)
    assert "54_hedge_stack" in keys(r)


def test_p10b_single_hedge_ok():
    r = scan("这可能会影响进度。通常周三发版。\n\n" + BASE)
    assert "54_hedge_stack" not in keys(r)


def test_p11_clean_keeps_chinese_punctuation():
    s = "他说：“这个——不行。”然后走了…… 「好」（真的）"
    assert dz.clean_text(s) == s


def test_p11b_clean_strips_zwsp_between_han():
    # 上游把汉字旁的 ZWSP 当断行提示保留；中文不需要，必须删掉
    assert dz.clean_text("会议\u200b室\u200d里") == "会议室里"


def test_p12_chat_ref_markup():
    r = scan("模型参数量约 7B【3†source】，训练用了两周。\n\n" + BASE)
    assert "64_chatbot_ref_markup" in keys(r)


def test_p12b_human_citation_label_not_markup():
    r = scan(BASE + "\n\n资料来源：国家统计局 2025 年统计公报\n\n[1] 来源：公司年报")
    assert "64_chatbot_ref_markup" not in keys(r)


def test_p13_cutoff_disclaimer():
    r = scan("截至我的知识截止日期，该产品尚未发布。\n\n" + BASE)
    assert "50_cutoff_disclaimer" in keys(r)


def test_p14_dash_overuse_vs_paired_aside():
    paired = scan("这个方案——说到底——是在赌时间。\n\n" + BASE)
    assert "58_em_dash_overuse" not in keys(paired)
    heavy = scan("这个方案——说到底——是在赌时间，真正的护城河不是技术——而是组织——以及人。\n\n" + BASE)
    assert "58_em_dash_overuse" in keys(heavy)


def test_p15_stdin_invalid_utf8_does_not_crash():
    data = "人写的一段话。".encode() + b"\xe5\x8f"  # 末尾截断半个汉字
    p = subprocess.run([sys.executable, str(_SCRIPT)], input=data, capture_output=True)
    assert p.returncode == 0, p.stderr.decode(errors="replace")


def test_p16_signposting_closing_sycophancy():
    t = ("这是一个非常好的问题。本文将从三个方面展开。让我们一起来探讨。"
         "希望本文能对你有所启发，让我们携手共创美好未来。")
    k = keys(scan(t + "\n\n" + BASE[:150]))
    assert {"48_sycophantic", "31_signposting", "38_formula_closing"} <= k


def test_p17_heading_bold_not_boldface():
    t = "\n".join(f"## **第{i}节**\n这一节写的是第 {i} 次迭代的改动，没有加粗。" for i in range(1, 8))
    assert "56_boldface" not in keys(scan(t))


def test_p18_no_cross_family_duplicates():
    # 同一个词只属于一族：「赋能」「全方位」「深度融合」「底层逻辑」都归 1_ai_bigwords
    import re
    P = dz._ZH_PATTERNS
    for word in ("赋能", "全方位", "深度融合", "底层逻辑"):
        owners = [k for k, v in P.items() if re.fullmatch(f"(?:{v[1]})", word)]
        assert owners == ["1_ai_bigwords"], f"{word} 属于 {owners}"


def test_p19_placeholder_is_defect_not_scored():
    base = score(scan(BASE + "\n\n负责人：张伟"))   # 同结构对照，只差占位符
    r = scan(BASE + "\n\n负责人：【待补】")
    assert r["63_placeholder_text"]["tier"] == "defect"
    assert score(r) == base


# ---------------------------------------------------------------- runner
if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}  {str(e)[:300]}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}  {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
