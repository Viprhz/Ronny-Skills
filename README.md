# Ronny-Skills

个人 Agent Skills 仓库。

## 目录

- [`sloptrim-zh`](./sloptrim-zh/)：中文去 AI 腔编辑辅助，保留事实与义务。包含固定版本的上游字符/格式组件、代码与引文保护、表格文案检查、抽取错误状态、保全辅助检查和行为评估；不提供作者身份鉴定。

## 验证

```bash
python3 -m unittest discover -s sloptrim-zh/tests -p 'test_*.py'
python3 sloptrim-zh/tests/test_probes_zh.py
python3 sloptrim-zh/tests/validation_corpus_zh.py
python3 sloptrim-zh/scripts/evaluate_corpus.py --corpus sloptrim-zh/tests/evaluation_samples.jsonl
```

运行时只需要 Python 3.9+ 标准库。来源与许可见 [`NOTICE`](./sloptrim-zh/NOTICE)。历史校准分布不代表独立准确率，具体边界见 skill 与评估说明。
