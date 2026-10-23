#!/usr/bin/env python3
"""离线评估编辑需求标签；不评估作者身份。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from collections import defaultdict
from detect_zh import scan


def evaluate(rows, threshold=40):
    metrics = defaultdict(lambda: dict(tp=0, tn=0, fp=0, fn=0, unassessed=0, manual_only=0))
    details = []
    ids = set()
    for row in rows:
        for key in ('id', 'text', 'genre', 'source', 'origin', 'split', 'needs_edit', 'labeler'):
            if key not in row:
                raise ValueError(f'缺少字段：{key}')
        if not all(isinstance(row[k], str) and row[k].strip() for k in ('id', 'text', 'genre', 'source', 'labeler')):
            raise ValueError('id/text/genre/source/labeler 必须为非空字符串')
        if row['id'] in ids:
            raise ValueError('样本 id 重复')
        ids.add(row['id'])
        if type(row['needs_edit']) is not bool:
            raise ValueError('needs_edit 必须是复核后的布尔标签，labeler 记录评估者')
        if row['split'] != 'holdout' or row['origin'] not in ('human', 'model', 'unknown'):
            raise ValueError('独立评估只接受 holdout，origin 必须 human/model/unknown')
        m = scan(row['text'])['_metrics']
        size = 'short' if m['zh_chars'] < 300 else 'medium' if m['zh_chars'] < 800 else 'long'
        groups = ['all', 'genre:'+row['genre'], 'length:'+size, 'origin:'+row['origin']]
        valid = m['assessment_status'] == 'ok'
        low = m['confidence'] in ('none', 'low')
        decision = ('tp' if row['needs_edit'] else 'fp') if valid and m['ai_tell_score'] >= threshold else ('fn' if row['needs_edit'] else 'tn')
        for group in groups:
            if valid:
                metrics['diagnostic:'+group][decision] += 1
                if low:
                    metrics['automatic:'+group]['manual_only'] += 1
                else:
                    metrics['automatic:'+group][decision] += 1
            else:
                metrics['diagnostic:'+group]['unassessed'] += 1
                metrics['automatic:'+group]['unassessed'] += 1
        details.append({'id':row['id'], 'genre':row['genre'], 'length':size, 'source':row['source'],
                        'origin':row['origin'], 'labeler':row['labeler'], 'needs_edit':row['needs_edit'], 'metrics':m})
    return {'threshold':threshold, 'samples':len(details), 'groups':dict(metrics), 'details':details,
            'limitation':'小样本只描述本次结果；自动矩阵排除低可信度，全体诊断矩阵保留这些样本。'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--corpus', required=True, type=Path)
    p.add_argument('--threshold', type=int, default=40)
    a = p.parse_args()
    try:
        if not 0 <= a.threshold <= 100:
            raise ValueError('threshold 必须在 0–100 之间')
        rows = [json.loads(line) for line in a.corpus.read_text(encoding='utf-8').splitlines() if line.strip()]
        if not rows:
            raise ValueError('语料为空')
        print(json.dumps(evaluate(rows,a.threshold),ensure_ascii=False,indent=2))
    except (OSError, ValueError, TypeError) as exc:
        p.exit(2, f'语料错误：{exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
