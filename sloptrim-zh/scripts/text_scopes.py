"""共享检测/清洗边界。只处理正文，代码与逐字引文原样恢复。"""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser


_QUOTE = re.compile(r'“[^”\n]*”|「[^」\n]*」|『[^』\n]*』')
_ZERO_HAN = re.compile(
    r'(?<=[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef])[\u200b\u2060\ufeff]+'
    r'|[\u200b\u2060\ufeff]+(?=[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef])')


def _map_protected(text, backend, replace):
    # 先保护代码，避免代码中的 > 或引号被重复保护。
    held = []
    prefix = 'SLOPTRIMZHKEEP'
    while prefix in text:
        prefix += 'X'

    token = re.compile(re.escape(prefix) + r'(\d+)END')

    def hold(span):
        # 外层引文可能包含先保护的代码，存入前展开子标记。
        # 每个 held 都是真正原文，恢复一轮即可，标记绝不泄漏。
        span = token.sub(lambda m: held[int(m[1])], span)
        held.append(span)
        return f'{prefix}{len(held)-1}END'

    masked = backend._map_code(text, hold)
    masked = re.sub(r'^[ \t]{0,3}>[^\n]*$', lambda m: hold(m.group()), masked, flags=re.M)
    masked = _QUOTE.sub(lambda m: hold(m.group()), masked)
    # 一次恢复，避免已恢复片段又被下一个 token 的替换触及。
    return masked, lambda value: token.sub(lambda m: replace(held[int(m[1])]), value)


def protected_spans(text, backend):
    spans = []
    masked, restore = _map_protected(text, backend, lambda s: spans.append(s) or s)
    restore(masked)
    return spans


def strip_protected(text, backend):
    masked, restore = _map_protected(text, backend,
                                     lambda s: re.sub(r'[^\n]', ' ', s))
    return restore(masked)


def clean_protected(text, backend):
    masked, restore = _map_protected(text, backend, lambda s: s)
    # 保护标记不含空白或汉字，所有清洗都必须发生在恢复之前。
    cleaned = backend.clean_text(masked, syntax='markdown')
    cleaned = _ZERO_HAN.sub('', cleaned)
    return restore(cleaned)


class _TableParser(HTMLParser):
    """从 HTML/飞书表格中提取单元格，保留行列边界。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag in ('td', 'th', 'lark-td', 'lark-th'):
            self.current = []
        elif self.current is not None and tag in ('br', 'p', 'div'):
            self.current.append('\n')

    def handle_endtag(self, tag):
        if tag in ('td', 'th', 'lark-td', 'lark-th') and self.current is not None:
            self.cells.append(''.join(self.current).strip())
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)


_TABLE_BLOCK = re.compile(r'<(lark-table|table)\b[^>]*>.*?</\1\s*>', re.I | re.S)
_TAG = re.compile(r'</?[A-Za-z][A-Za-z0-9-]*(?:\s[^>]{0,200})?/?>')
_ATTR = re.compile(r'\{[A-Za-z_]+="[^"]*"\}')
_LIST = re.compile(r'^\s*(?:[-*+]\s+|\d+[.)、]\s*)')
_HEADING = re.compile(r'^\s{0,3}#{1,6}\s+')


def _cells(line):
    return re.split(r'(?<!\\)\|', line.strip().strip('|'))


def _table_separator(line):
    cells = _cells(line)
    return '|' in line and all(re.fullmatch(r'\s*:?-{3,}:?\s*', c) for c in cells)


def _plain(text):
    text = _TAG.sub(' ', text)
    text = _ATTR.sub(' ', text)
    text = re.sub(r'!?\[([^\]\n]*)\]\([^\n)]*\)', r'\1', text)
    text = re.sub(r'\*\*|__|~~', '', text)
    return unescape(text)


def _units(text):
    return (len(re.findall(r'[\u3400-\u9fff\uf900-\ufaff]', text))
            + len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)*", text))
            + len(re.findall(r'\d+(?:\.\d+)?', text)))


def prepare_scopes(raw, backend):
    unprotected = strip_protected(raw, backend)
    protected_units = max(0, _units(raw) - _units(unprotected))
    # YAML frontmatter 是元数据，不是文案。
    work = re.sub(r'\A---[ \t]*\n.*?\n(?:---|\.\.\.)[ \t]*(?:\n|$)', '\n', unprotected, flags=re.S)
    table_cells = []

    def table(m):
        parser = _TableParser()
        parser.feed(m.group())
        table_cells.extend(parser.cells)
        return '\n'

    work = _TABLE_BLOCK.sub(table, work)
    lines = work.split('\n')
    body = []
    i = 0
    while i < len(lines):
        # 需要分隔行才识别无外框 Markdown 表格，避免把普通 | 误当表格。
        if i + 1 < len(lines) and '|' in lines[i] and _table_separator(lines[i + 1]):
            table_cells.extend(_cells(lines[i]))
            i += 2
            while i < len(lines) and '|' in lines[i] and lines[i].strip():
                table_cells.extend(_cells(lines[i]))
                i += 1
            body.append('')
            continue
        body.append(lines[i])
        i += 1
    prose_body = '\n'.join(body)
    # 除去格式符，但保留标题/列表中的词汇。统计范围则排除这些结构化行。
    lexical_lines = []
    rhythm_lines = []
    in_list = False
    for line in body:
        structured = bool(_HEADING.match(line) or _LIST.match(line))
        continuation = in_list and bool(re.match(r'^\s{2,}\S', line))
        if line.strip():
            in_list = bool(_LIST.match(line) or continuation)
        lexical_lines.append(_LIST.sub('', _HEADING.sub('', line)))
        rhythm_lines.append('' if structured or continuation else line)
    body_plain = _plain('\n'.join(lexical_lines))
    cells_plain = [_plain(c).strip() for c in table_cells if c.strip()]
    lexical = body_plain + ('\n\n' + '\n\n'.join(cells_plain) if cells_plain else '')
    rhythm = _plain('\n'.join(rhythm_lines))
    # 格式/URL 线索保留原始标记，但也包含表格单元格正文。
    raw_nocode = prose_body + ('\n\n' + '\n\n'.join(table_cells) if table_cells else '')
    analyzed = _units(lexical)
    input_units = _units(raw)
    coverage = {
        'input_units': input_units, 'analyzed_units': analyzed,
        'prose_units': _units(body_plain), 'table_units': _units('\n'.join(cells_plain)),
        'table_cells': len(cells_plain), 'protected_units': protected_units,
        'rhythm_units': _units(rhythm),
        'analysis_coverage': round(min(1.0, analyzed / input_units), 3) if input_units else 0.0,
    }
    return lexical, rhythm, raw_nocode, coverage
