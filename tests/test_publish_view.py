"""publish_view CLI contracts: citation compression and residual-ID gate."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/publish_view.py'


class PublishViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write_index(self, day, entries):
        root = Path(self.tmp.name) / 'raw'
        (root / day).mkdir(parents=True, exist_ok=True)
        payload = dict(sources=dict(chat=dict(entries=[
            dict(id=cid, title=title) for cid, title in entries])))
        (root / day / 'context.index.json').write_text(
            json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return str(root)

    def run_view(self, text, expected=0, name='digest.md', root=None):
        digest = Path(self.tmp.name) / name
        digest.write_text(text, encoding='utf-8')
        args = [sys.executable, str(SCRIPT), str(digest)]
        if root:
            args += ['--root', root]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result.stdout

    def test_full_chat_citation_compressed(self):
        out = self.run_view(
            '- **10:52** 同事甲已沟通。（来源：2026-10-15 10:52:55 | peer 同事甲 | '
            'chat:cidSyntheticPeerA= msg:msgSynthetic001+/==）')
        self.assertIn('（来源：私聊·同事甲 10-15 10:52）', out)

    def test_multi_segment_chat_citation(self):
        out = self.run_view(
            '（来源：2026-10-15 14:44:42 | peer 同事甲 | chat:cidAAA msg:msgBBB；'
            '2026-10-15 14:45:09 | self 用户甲 | chat:cidAAA msg:msgCCC）')
        self.assertIn('（来源：私聊·同事甲 10-15 14:44；私聊·用户甲 10-15 14:45）', out)

    def test_inline_code_is_preserved_even_when_it_looks_like_a_citation(self):
        text = '`2026-10-06 21:48:23 | peer 测试发布助手 | chat:cidSyntheticBot= msg:msgSynthetic001==`(示例)'
        self.assertEqual(self.run_view(text), text)

    def test_bare_ids_in_table_cell(self):
        out = self.run_view(
            '| 事项 | minutes:76327569643438353434313635395f34323438323931325f39；'
            'chat:cidSyntheticPeerA= msg:msgSynthetic003+/== | 同事甲 |')
        self.assertIn('| 事项 | 听记；私聊 | 同事甲 |', out)

    def test_truncated_cid_todo_form(self):
        out = self.run_view(
            '| 同事丙 chat:cidSyntheticPeerC… msg:msgSynthetic004+/==(10-10 16:57);'
            '同事丁 chat:cidSyntheticPeerD… msg:msgSynthetic005+/==(10-09 10:37) |')
        self.assertIn('| 同事丙 私聊(10-10 16:57);同事丁 私聊(10-09 10:37) |', out)

    def test_slash_separated_extra_msg(self):
        out = self.run_view(
            '| chat:cidSyntheticCode= msg:msgSynthetic006+/== '
            '/ msg:msgSynthetic007+/== · [CR](https://code.example.test/codereview/1002) |')
        self.assertIn(
            '| 私聊 · [CR](https://code.example.test/codereview/1002) |', out)

    def test_repeated_mail_collapses_with_count(self):
        out = self.run_view('（来源：mail:DzzzzzzNMm6、mail:DzzzzzzNMm5、mail:DzzzzzzNMm4）')
        self.assertIn('（来源：邮件×3）', out)

    def test_dotted_and_dashed_mail_ids_swallowed(self):
        out = self.run_view('（来源：mail:DzzzzzzNMm.、mail:DzzzzzzNMm-）')
        self.assertIn('（来源：邮件×2）', out)

    def test_mail_run_with_trailing_range(self):
        out = self.run_view(
            '（来源：mail:DzzzzzzNMm-、mail:DzzzzzzNMlq、mail:DzzzzzzNMlr、mail:DzzzzzzNMlx、'
            'mail:DzzzzzzNMly（10-15 15:02–15:03））')
        self.assertIn('（来源：邮件×5（10-15 15:02–15:03））', out)

    def test_urls_and_facts_untouched(self):
        text = ('- [CR 1001](https://code.example.test/team/sample/codereview/1001) 合并。'
                '（来源：mail:DzzzzzzNMm7）\n'
                '数据源：raw/2026-10-15（1 天，缺失 0，不可读 0）\n'
                '快照：2026-10-15@2026-10-20 09:27')
        out = self.run_view(text)
        self.assertIn('[CR 1001](https://code.example.test/team/sample/codereview/1001)', out)
        self.assertIn('数据源：raw/2026-10-15（1 天，缺失 0，不可读 0）', out)
        self.assertIn('快照：2026-10-15@2026-10-20 09:27', out)

    def test_urls_and_code_with_id_like_text_untouched(self):
        text = ('[源](https://example.com/mail:abc)\n'
                '[源](https://example.com/a(b)/chat:cidABC)\n'
                '[ref]: https://example.com/minutes:abcdef\n'
                '[relative](./mail:abc)\n'
                '[email](mailto:mail:abc@example.com)\n'
                '    value = "mail:abc"\n'
                '裸链接 https://example.com/calendar:abc\n'
                '`mail:abc`\n'
                '```python\nvalue = "mail:abc"\n```\n'
                '~~~text\nchat:cidABC msg:msgABC\n~~~\n')
        self.assertEqual(self.run_view(text), text)

    def test_list_continuation_is_not_mistaken_for_code(self):
        text = '- 进展\n    （来源：mail:abc）\n'
        self.assertEqual(self.run_view(text), '- 进展\n    （来源：邮件）\n')

    def test_residual_msg_id_gates_exit_code(self):
        digest = Path(self.tmp.name) / 'digest.md'
        digest.write_text('（来源：msg:msgSynthetic008+/==）', encoding='utf-8')
        result = subprocess.run([sys.executable, str(SCRIPT), str(digest)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('残留机器 ID', result.stderr)

    def test_bare_chat_enriched_from_index(self):
        root = self.write_index('2026-10-15', [
            ('cidSyntheticPeerA=', '同事甲'),
            ('cidSyntheticPeerB=', '同事乙')])
        out = self.run_view(
            '| 听记·评审；chat:cidSyntheticPeerA= '
            'msg:msgSynthetic003+/== | 同事甲 |',
            name='2026-10-15.md', root=root)
        self.assertIn('| 听记·评审；私聊·同事甲 | 同事甲 |', out)

    def test_enrichment_skips_duplicate_name_prefix(self):
        root = self.write_index('2026-10-05', [
            ('cidSyntheticPeerCfull=', '同事丙')])
        out = self.run_view(
            '| 同事丙 chat:cidSyntheticPeerCfull= msg:msgSynthetic004+/==(10-10 16:57) |',
            name='2026-10-05.md', root=root)
        self.assertIn('| 同事丙 私聊(10-10 16:57) |', out)

    def test_truncated_cid_prefix_match(self):
        root = self.write_index('2026-10-08', [
            ('cidSyntheticPeerB=', '同事乙')])
        out = self.run_view(
            'chat:cidSyntheticPeerB=… '
            'msg:msgSynthetic009+/==(10-08 12:04)',
            name='2026-10-08.md', root=root)
        self.assertIn('私聊·同事乙(10-08 12:04)', out)

    def test_multi_day_window_loads_each_day_index(self):
        root = self.write_index('2026-10-06', [('cidOnlyInDay6', '同事戊')])
        out = self.run_view('chat:cidOnlyInDay6 msg:msgAny', name='2026-10-05_2026-10-07.md', root=root)
        self.assertIn('私聊·同事戊', out)

    def test_missing_index_falls_back_to_generic_label(self):
        out = self.run_view('chat:cidNotIndexed msg:msgAny', name='2026-10-15.md')
        self.assertIn('私聊', out)
        self.assertNotIn('私聊·', out)

    def test_readable_paren_ids_stripped(self):
        out = self.run_view(
            '（来源：邮件×3（mail:DzzzzzzNMm6、mail:DzzzzzzNMm5、mail:DzzzzzzNMm4））\n'
            '| 听记·示例项目 CLI 切换自研 MCP 接口；私聊·同事甲 10-15 14:44'
            '（minutes:76327569643438353837313836305f363733353539323039365f39；'
            'chat:cidSyntheticPeerA= msg:msgSynthetic003+/==） |')
        self.assertIn('（来源：邮件×3）', out)
        self.assertIn('| 听记·示例项目 CLI 切换自研 MCP 接口；私聊·同事甲 10-15 14:44 |', out)

    def test_time_annotation_paren_kept(self):
        out = self.run_view('同事丙 私聊(10-10 16:57)（首次失败）')
        self.assertIn('同事丙 私聊(10-10 16:57)（首次失败）', out)

    def test_evidence_appendix_is_dropped_from_view(self):
        text = ('## 推进\n\n- 事项已跟进。（来源：2026-10-15 10:52:55 | peer 同事甲 | '
                'chat:cidSyntheticPeerA= msg:msgSynthetic001+/==）\n\n'
                '## 证据附录\n\n- 事项：2026-10-15 10:52:55 | peer 同事甲 | '
                'chat:cidSyntheticPeerA= msg:msgSynthetic001+/==\n')
        out = self.run_view(text)
        self.assertNotIn('证据附录', out)
        self.assertNotIn('chat:cidSyntheticPeerA=', out)
        self.assertIn('私聊·同事甲 10-15 10:52', out)

    def test_appendix_stops_at_next_section(self):
        text = ('## 证据附录\n\n- 事项：chat:cidSyntheticPeerA= msg:msgSynthetic001+/==\n\n'
                '## 覆盖口径\n\n数据源：raw/2026-10-15（1 天，缺失 0，不可读 0）\n')
        out = self.run_view(text)
        self.assertNotIn('证据附录', out)
        self.assertNotIn('chat:cidSyntheticPeerA=', out)
        self.assertIn('数据源：raw/2026-10-15', out)

    def test_appendix_heading_inside_code_is_not_stripped(self):
        text = ('```markdown\n## 证据附录\nchat:cidSyntheticPeerA= msg:msgSynthetic001+/==\n```\n')
        self.assertEqual(self.run_view(text), text)


if __name__ == '__main__':
    unittest.main()
