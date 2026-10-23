"""评审发现的可观察边界：不依赖锁死分数。"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import detect_zh as dz
from check_preservation import check_pair
from evaluate_corpus import evaluate

SLOP = '在当今这个信息爆炸的时代，赋能业务、形成闭环至关重要。本文将从多个维度探讨这一问题。综上所述，未来可期。'


class ScopeRegressionTests(unittest.TestCase):
    def test_code_not_scored(self):
        for text in ['```text\n'+SLOP+'\n```', '`'+SLOP+'`', '    '+SLOP, '``'+SLOP+'``']:
            with self.subTest(text=text[:15]):
                m=dz.scan(text)['_metrics']
                self.assertEqual(m['assessment_status'],'no_analyzable_text')
                self.assertIsNone(m['ai_tell_score'])
                self.assertEqual(m['zh_chars'],0)

    def test_clean_verbatim_and_body(self):
        protected = ['`中文\u200b值`', '```text\n中文\u200b值  \n```', '> 中文\u200b值  ', '“中文\u200b值”', '「中文\u200b值」', '> 原文 `中文\u200b值`', '“保留 `参数` 原样。”', '「保留 ``参数`` 原样。」']
        for text in protected:
            with self.subTest(text=text[:10]):
                out=dz.clean_text('正文\u200b残渣。\n\n'+text)
                self.assertIn(text,out)
                self.assertIn('正文残渣。',out)

    def test_without_external_backend_has_same_behavior(self):
        t='中文\u200b值\u00a0已记录。👨\u200d💻\n\n```txt\n中文\u200b值\n```\n\n> 原文\u200b内容'
        expected=dz.clean_text(t)
        old=dz._UP
        try:
            dz._UP=None
            self.assertEqual(dz.clean_text(t),expected)
            self.assertIn('👨\u200d💻',dz.clean_text(t))
        finally:
            dz._UP=old

    def test_tables_keep_words_exclude_rhythm(self):
        tables=['|说明|\n|---|\n|'+SLOP+'|', '说明 | 内容\n--- | ---\n介绍 | '+SLOP,
                '<lark-table><lark-tr><lark-td>'+SLOP+'</lark-td></lark-tr></lark-table>',
                '<table><tr><td>'+SLOP+'</td></tr></table>']
        for t in tables:
            with self.subTest(text=t[:10]):
                r=dz.scan(t)
                self.assertIn('1_ai_bigwords',r)
                self.assertGreater(r['_metrics']['coverage']['table_units'],20)
                self.assertEqual(r['_metrics']['coverage']['rhythm_units'],0)
                self.assertNotIn('40_sentence_monotony',r)
                self.assertNotIn('71_degenerate_repetition',r)

    def test_lists_excluded_from_rhythm(self):
        text='\n'.join('- '+x for x in ['请先检查设备电源。','然后检查网线连接。','接着检查端口指示。','最后检查服务日志。','随后重新启动设备。'])
        r=dz.scan(text)
        self.assertNotIn('40_sentence_monotony',r)
        self.assertGreater(r['_metrics']['zh_chars'],0)

    def test_empty_is_unassessed(self):
        r=dz.scan('')['_metrics']
        self.assertIsNone(r['ai_tell_score'])
        self.assertEqual(r['zh_chars'],0)
        self.assertEqual(r['ai_tell_band'],'unassessed')

    def test_bounds_not_html(self):
        r=dz.scan('范围为 100<d≤400cm；另有 a>b。')['_metrics']
        self.assertGreater(r['zh_chars'],5)
        from text_scopes import prepare_scopes
        self.assertIn('100<d≤400cm',prepare_scopes('范围为 100<d≤400cm；另有 a>b。',dz._backend())[0])

    def test_digits_do_not_exempt_vague_actions(self):
        for text in ['持续优化整体体验，推进平台化建设。','2026年持续优化整体体验，推进平台化建设。',
                     'v2.1持续优化整体体验，推进平台化建设。']:
            r=dz.scan(text)
            self.assertIn('17_hollow_action',r)
            self.assertIn('2_universal_verbs',r)

    def test_concrete_metric_exempts_action(self):
        r=dz.scan('把缓存换成本地内存，降低了服务器成本，每月省 3000 元。')
        self.assertNotIn('17_hollow_action',r)

    def test_source_flags_not_fake_fact(self):
        for text,key in [('相关研究表明，该方法可能有效。[1]','11_unsourced_evidence'),
                         ('正如某位作者所言，这个方法适用。','16_false_authority')]:
            r=dz.scan(text)
            self.assertEqual(r[key]['tier'],'report')
            self.assertNotIn('虚假',r[key]['label'])
            self.assertNotIn('无来源',r[key]['label'])


class CliRegressionTests(unittest.TestCase):
    def run_cli(self,*args,input=None):
        return subprocess.run([sys.executable,str(SCRIPTS/'detect_zh.py'),*map(str,args)],input=input,capture_output=True,text=True)

    def test_help(self):
        p=self.run_cli('--help');self.assertEqual(p.returncode,0);self.assertIn('--clean',p.stdout)

    def test_unknown_flag_rejected(self):
        self.assertEqual(self.run_cli('--typo').returncode,2)

    def test_pattern_catalog_matches_code(self):
        p=self.run_cli('--list-patterns');j=json.loads(p.stdout)
        self.assertEqual(j['count'],len(dz._ZH_PATTERNS))
        self.assertEqual(len(j['patterns']),j['count'])

    def test_bad_docx_and_empty_fail(self):
        with tempfile.TemporaryDirectory() as td:
            for name,data in [('bad.docx',b'not-a-zip'),('empty.txt',b''),('bad.txt',b'\xff')]:
                path=Path(td)/name;path.write_bytes(data)
                p=self.run_cli(path);j=json.loads(p.stdout)
                self.assertEqual(p.returncode,2)
                self.assertEqual(j['_metrics']['extraction_status'],'error')
                self.assertIsNone(j['_metrics']['ai_tell_score'])

    def test_clean_error_does_not_emit_document(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'bad.docx';path.write_bytes(b'bad')
            p=self.run_cli('--clean',path)
            self.assertEqual(p.returncode,2);self.assertEqual(p.stdout,'');self.assertIn('error',p.stderr)

    def test_docx_extract_without_modifying_package(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'good.docx'
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>乙方应提供 SDK，不得改变范围。</w:t></w:r></w:p></w:body></w:document>')
            before=path.read_bytes();p=self.run_cli(path)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            self.assertEqual(path.read_bytes(),before)
            self.assertGreater(json.loads(p.stdout)['_metrics']['zh_chars'],0)

    def test_no_prose_stdin_has_no_clean_verdict(self):
        p=self.run_cli(input='```text\n示例\n```')
        self.assertEqual(p.returncode,2)
        self.assertEqual(json.loads(p.stdout)['_metrics']['ai_tell_band'],'unassessed')

    def test_partial_epub_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'partial.epub'
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('OEBPS/good.xhtml','<html><body><p>完整章节。</p></body></html>')
                z.writestr('OEBPS/bad.xhtml','<html><body><p>损坏章节')
            p=self.run_cli(path)
            self.assertEqual(p.returncode,2)
            self.assertEqual(json.loads(p.stdout)['_metrics']['extraction_status'],'error')

    def test_office_tables_keep_structure(self):
        lines=['请先检查设备电源。','然后检查网线连接。','接着检查端口指示。','最后检查服务日志。','随后重新启动设备。']
        word='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl>'+''.join('<w:tr><w:tc><w:p><w:r><w:t>'+t+'</w:t></w:r></w:p></w:tc></w:tr>' for t in lines)+'</w:tbl></w:body></w:document>'
        sheet='<worksheet><sheetData>'+''.join('<row><c t="inlineStr"><is><t>'+t+'</t></is></c></row>' for t in lines)+'</sheetData></worksheet>'
        with tempfile.TemporaryDirectory() as td:
            for name,member,xml in [('table.docx','word/document.xml',word),('table.xlsx','xl/worksheets/sheet1.xml',sheet)]:
                path=Path(td)/name
                with zipfile.ZipFile(path,'w') as z:z.writestr(member,xml)
                r=dz.scan(dz.extract_text(path)[0])
                self.assertEqual(r['_metrics']['coverage']['table_cells'],5)
                self.assertEqual(r['_metrics']['coverage']['rhythm_units'],0)
                self.assertNotIn('40_sentence_monotony',r)

    def test_epub_visible_containers_and_tails_not_dropped(self):
        xml='<html xmlns="http://www.w3.org/1999/xhtml"><body>开头。<p>普通引言。</p>尾文。<div>赋能业务，形成闭环。</div><section>重要正文<span>中间内容</span>不能丢失。</section><script>隐藏脚本</script></body></html>'
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'containers.epub'
            with zipfile.ZipFile(path,'w') as z:z.writestr('OEBPS/chapter.xhtml',xml)
            text=dz.extract_text(path)[0]
            for part in ['开头。','普通引言。','尾文。','赋能业务，形成闭环。','重要正文中间内容不能丢失。']:
                self.assertIn(part,text)
            self.assertNotIn('隐藏脚本',text)

    def test_word_inline_code_does_not_hide_paragraph(self):
        xml='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>在当今这个信息爆炸的时代，赋能业务、形成闭环至关重要。</w:t></w:r><w:r><w:rPr><w:rStyle w:val="CodeChar"/></w:rPr><w:t>fetchData()</w:t></w:r><w:r><w:t>正文仍需分析。</w:t></w:r></w:p></w:body></w:document>'
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'inline.docx'
            with zipfile.ZipFile(path,'w') as z:z.writestr('word/document.xml',xml)
            text=dz.extract_text(path)[0]
            self.assertIn('`fetchData()`',text)
            r=dz.scan(text)
            self.assertIn('1_ai_bigwords',r)
            self.assertIn('9_formula_opener',r)

    def test_external_backend_is_explicit(self):
        self.assertEqual(dz.scan('正常正文。')['_metrics']['backend'],'bundled-0.9.6')


class PreservationTests(unittest.TestCase):
    def test_same_semantics_with_markers(self):
        a='乙方应在周五前提供 SDK，范围为 100<d≤400cm；甲方可以选择【待补】。'
        b='范围为 100<d≤400cm。乙方应在周五前提供 SDK；甲方可以选择【待补】。'
        self.assertTrue(check_pair(a,b)['passed'])

    def test_obligations_negation_placeholders_boundaries(self):
        pairs=[('甲方可以选择。','甲方必须选择。'),('乙方不得上线。','乙方可以上线。'),
               ('范围 100<d≤400cm。','范围 100≤d≤400cm。'),('负责人：【待补】','负责人：张伟'),
               ('可能提升 30%','提升 30%'),('两项工作同时执行。','两项工作先后执行。')]
        for a,b in pairs:
            self.assertFalse(check_pair(a,b)['passed'],(a,b))

    def test_verbatim_and_entities(self):
        for a,b in [('`中文\u200b值`','`中文值`'),('``中文 `x` 原值``','``中文 `x` 新值``'),('『逐字原文』','『逐字改文』'),('    原文\u200b代码','    原文代码')]:
            self.assertFalse(check_pair(a,b)['passed'])
        self.assertFalse(check_pair('张伟检查召回率。','李明检查回收率。',entities=['张伟'],terms=['召回率'])['passed'])

    def test_numbers_on_wrong_noun_need_semantic_review(self):
        r=check_pair('续航 30 小时，降噪 20 小时。','续航 20 小时，降噪 30 小时。')
        self.assertIn('人工复核',r['limitation'])


class EvaluationTests(unittest.TestCase):
    def row(self,**kwargs):
        return dict(id='x',text='正常正文。',genre='技术说明',source='独立测试样例',origin='unknown',split='holdout',needs_edit=False,labeler='合成测试标签',**kwargs)

    def test_low_confidence_is_reported_separately(self):
        r=evaluate([self.row()]);self.assertEqual(r['groups']['automatic:all']['manual_only'],1)
        self.assertEqual(sum(r['groups']['diagnostic:all'][k] for k in ('tp','tn','fp','fn')),1)

    def test_invalid_origin_or_label_rejected(self):
        for field,value in [('origin','AI-proof'),('needs_edit','false'),('split','train')]:
            row=self.row();row[field]=value
            with self.assertRaises(ValueError):evaluate([row])

    def test_unassessed_not_counted_as_clean(self):
        row=self.row();row['text']='`示例`'
        r=evaluate([row]);self.assertEqual(r['groups']['diagnostic:all']['unassessed'],1)
        self.assertEqual(r['groups']['diagnostic:all']['tn'],0)


if __name__=='__main__':unittest.main()
