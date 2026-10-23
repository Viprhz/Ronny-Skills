#!/usr/bin/env python3
"""改写保全辅助：发现标记变化，不证明语义等价。"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from detect_zh import _backend
from text_scopes import protected_spans

_NUMBER = re.compile(r'(?<![A-Za-z])\d+(?:\.\d+)*(?:\s*(?:%|％|‰|cm|mm|kg|lb|ms|GB|MB|小时|毫秒|秒|元|天|周|年))?')
_PLACEHOLDER = re.compile(r'\{\{[^}\n]+\}\}|【[^】\n]*(?:待补|待定|TBD)[^】\n]*】|\[(?:姓名|公司名|待补|TBD)\]')
_FORCE = re.compile(r'不得|不能|不可|不超过|不低于|不少于|不大于|必须|应当|应(?=在|提供|完成|提交|负责|支持)|'
                    r'可(?=在|提供|选择|使用|增加|调整)|可以|可选|可能|或许|大致|大约|仅限|仅(?=在|支持|提供)|'
                    r'至少|至多|全部|部分|同时|先后|尚未|不在|并非|不是')
_BOUNDARY = re.compile(r'(?:\d+(?:\.\d+)?\s*[<≤>≥]\s*)?[A-Za-z]\s*[<≤>≥]\s*\d+(?:\.\d+)?(?:cm|mm|m)?')



def _counter(rx, text):
    return Counter(re.sub(r'\s+', '', m.group()) for m in rx.finditer(text))


def check_pair(before, after, entities=(), terms=()):
    issues = []
    for name, rx in [('numbers_units', _NUMBER), ('placeholders', _PLACEHOLDER),
                     ('obligations_qualifiers', _FORCE), ('boundaries', _BOUNDARY)]:
        a, b = _counter(rx, before), _counter(rx, after)
        if a != b:
            issues.append({'kind': name, 'missing': list((a-b).elements()),
                           'added': list((b-a).elements())})
    # 逐字区域不去空白；其中一个零宽字符变化也必须发现。
    a, b = Counter(protected_spans(before, _backend())), Counter(protected_spans(after, _backend()))
    if a != b:
        issues.append({'kind': 'verbatim', 'missing': list((a-b).elements()), 'added': list((b-a).elements())})
    for token in list(entities) + list(terms):
        if token in before and token not in after:
            issues.append({'kind': 'protected_term', 'missing': [token], 'added': []})
    return {'passed': not issues, 'issues': issues,
            'limitation': '仅核对部分标记；须人工复核数字归属、条件、因果、新增事实与语义等价。'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('before', type=Path); p.add_argument('after', type=Path)
    p.add_argument('--entity', action='append', default=[])
    p.add_argument('--term', action='append', default=[])
    a = p.parse_args()
    report = check_pair(a.before.read_text(encoding='utf-8'), a.after.read_text(encoding='utf-8'), a.entity, a.term)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
