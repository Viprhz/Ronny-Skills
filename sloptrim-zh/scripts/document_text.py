"""只提取供分析的文字与结构，不重建或覆盖源文档。"""
from __future__ import annotations

from html import escape
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

_MEMBER_CAP = 32 * 1024 * 1024
_TOTAL_CAP = 64 * 1024 * 1024
_SKIP = {'head', 'script', 'style', 'nav', 'svg', 'template', 'noscript'}
_CODE_STYLES = {'code', 'sourcecode', 'codeblock', 'codechar', 'htmlpreformatted'}
_WORD_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def local(tag):
    return tag.rsplit('}',1)[-1]


def node_text(node):
    if local(node.tag) in _SKIP:
        return ''
    text = node.text or ''
    for child in node:
        if local(child.tag) == 's':
            count=next((v for k,v in child.attrib.items() if local(k)=='c'),'1')
            text += ' ' * min(max(int(count),1),_MEMBER_CAP)
        elif local(child.tag) in {'br', 'tab', 'line-break'}:
            text += '\n' if local(child.tag) != 'tab' else '\t'
        else:
            text += node_text(child)
        text += child.tail or ''
    return text


def word_paragraph_text(node):
    # 字符代码样式仅保护所属 run，不能吞掉整段自然语言。
    if node.tag == _WORD_NS+'r':
        props=node.find(_WORD_NS+'rPr')
        style=props.find(_WORD_NS+'rStyle') if props is not None else None
        value=style.get(_WORD_NS+'val','').lower().replace(' ','') if style is not None else ''
        text=node_text(node)
        if value in _CODE_STYLES:
            fence='`' * max(1,max((len(m.group())+1 for m in re.finditer(r'`+',text)),default=1))
            return fence+text+fence
        return text
    return (node.text or '')+''.join(word_paragraph_text(c)+(c.tail or '') for c in node)


def render(node):
    tag = local(node.tag)
    if tag in _SKIP:
        return ''
    if tag in {'tbl', 'table'}:
        rows = []
        row_nodes = [c for c in node if local(c.tag) in {'tr','table-row'}]
        if not row_nodes:
            row_nodes = [r for wrapper in node if local(wrapper.tag) in {'tbody','thead','tfoot'}
                         for r in wrapper if local(r.tag)=='tr']
        for row in row_nodes:
            cells = ['<td>'+escape(node_text(c))+'</td>' for c in row
                     if local(c.tag) in {'tc','td','th','table-cell'}]
            rows.append('<tr>'+''.join(cells)+'</tr>')
        return '<table>'+''.join(rows)+'</table>\n\n'
    if tag in {'p','h','h1','h2','h3','h4','h5','h6','li','blockquote'}:
        text = word_paragraph_text(node) if node.tag == _WORD_NS+'p' else node_text(node)
        if not text.strip():
            return ''
        styles = [el.get(_WORD_NS+'val','').lower().replace(' ','') for el in node.iter()
                  if local(el.tag) == 'pStyle']
        if any(s in _CODE_STYLES for s in styles):
            fence = '`' * max(3,max((len(m.group())+1 for m in re.finditer(r'`+',text)),default=3))
            return fence+'text\n'+text+'\n'+fence+'\n\n'
        if tag=='blockquote' or any(s in {'quote','intensequote'} for s in styles):
            return '\n'.join('> '+line for line in text.splitlines())+'\n\n'
        if tag=='li' or any(local(el.tag)=='numPr' for el in node.iter()):
            return '- '+text+'\n\n'
        if tag in {'h','h1','h2','h3','h4','h5','h6'} or any(s.startswith('heading') for s in styles):
            return '# '+text+'\n\n'
        return text+'\n\n'
    return (node.text or '')+''.join(render(c)+(c.tail or '') for c in node)


def extract_office(path: Path, backend):
    suffix=path.suffix.lower()
    wanted=backend._ZIP_DOC[suffix]
    with zipfile.ZipFile(path) as archive:
        names=sorted((n for n in archive.namelist() if any(w in n for w in wanted)),key=backend._reading_order)
        if not names:
            raise ValueError('未找到支持的正文成员')
        infos=[archive.getinfo(n) for n in names]
        if any(i.file_size>_MEMBER_CAP for i in infos) or sum(i.file_size for i in infos)>_TOTAL_CAP:
            raise ValueError('正文解压大小超限，拒绝部分分析')
        # 所有正文成员都必须成功解析，不能跳过坏章节后返回成功。
        roots=[(n,ET.fromstring(backend._numeric_entities(archive.read(n)))) for n in names]
    if suffix in {'.xlsx','.xlsm'}:
        shared=[]
        for name,root in roots:
            if name=='xl/sharedStrings.xml':
                shared=[''.join(el.text or '' for el in si.iter() if local(el.tag)=='t')
                        for si in root if local(si.tag)=='si']
        sheets=[]
        for name,root in roots:
            if name=='xl/sharedStrings.xml':
                continue
            rows=[]
            for row in root.iter():
                if local(row.tag)!='row':
                    continue
                cells=[]
                for cell in row:
                    if local(cell.tag)!='c':
                        continue
                    kind=cell.get('t')
                    value=next((el.text or '' for el in cell if local(el.tag)=='v'),'')
                    if kind=='s':
                        index=int(value)
                        if not 0<=index<len(shared):
                            raise ValueError('共享字符串索引越界')
                        value=shared[index]
                    elif kind=='inlineStr':
                        value=''.join(el.text or '' for el in cell.iter() if local(el.tag)=='t')
                    cells.append('<td>'+escape(value)+'</td>')
                rows.append('<tr>'+''.join(cells)+'</tr>')
            sheets.append('<table>'+''.join(rows)+'</table>')
        return '\n\n'.join(sheets)
    return '\n\n'.join(render(root) for _,root in roots).strip()
