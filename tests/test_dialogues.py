"""Current dialogue encoder and collection/update/merge behavior."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import chats
from export_dialogues import export


class DialogueExportTests(unittest.TestCase):
    def fixture(self):
        def message(mid, text, sender='me', time='09:00:00'):
            return dict(msgId=mid, createTime='2026-10-13 '+time,
                        senderId=sender, sender='本人' if sender=='me' else '同事',
                        role='peer' if sender=='me' else 'self', text=text)
        return dict(meta=dict(self=dict(senderIds=['me']),
                             window=dict(start='2026-10-13 00:00:00', end='2026-10-14 00:00:00'),
                             complete=False, knownConversationsComplete=False,
                             stopReason='partial_read_failures',
                             reads=[dict(cid='waiting', start='2026-10-13 00:00:00',
                                         end='2026-10-14 00:00:00', complete=False,
                                         stopReason='read_failure')]),
                    conversations=[
                        dict(cid='waiting', title='没有回复的对方', singleChat=True, msgs=[
                            message('request', '试用期多久？', time='10:00:00')]),
                        dict(cid='notes', title='本人备忘', singleChat=True, msgs=[message('note', '记录一下')]),
                        dict(cid='work', title='同事', singleChat=True, msgs=[
                            message('reply', '我研究下哈', time='11:00:00'),
                            message('media', '[图片消息](mediaId=opaque)请看这里 [视频通话] 通话时长 3:31', 'other'),
                            message('file', '[文件消息](mediaId=file-id)说明 [语音消息](mediaId=audio-id)', 'other'),
                            message('calendar', '日程：讨论\n时间：2026-10-14 10:00\n日程ID：calendar-id', 'other')]),
                        dict(cid='empty', title='空会话', singleChat=True, msgs=[])])

    def test_export_preserves_evidence_boundaries_and_references(self):
        data = self.fixture()
        out = export(data, 'Asia/Shanghai', {'notes'})
        cs = {c['cid']:c for c in out['conversations']}
        self.assertEqual(set(cs), {'waiting','notes','work'})
        self.assertEqual(cs['waiting']['kind'], 'private_chat')
        self.assertEqual(cs['waiting']['direction'], 'self_only')
        self.assertEqual(cs['notes']['kind'], 'self_notes')
        self.assertEqual(cs['work']['direction'], 'bidirectional')
        self.assertFalse(out['meta']['coverage']['complete'])
        self.assertEqual(out['meta']['coverage']['failedReads'][0]['cid'], 'waiting')
        self.assertEqual(out['meta']['window']['timezone'], 'Asia/Shanghai')
        refs = {(c['cid'],m['msgId']):m for c in data['conversations'] for m in c['msgs']}
        for cid,c in cs.items():
            times = [m['createTime'] for m in c['messages']]
            self.assertEqual(times, sorted(times))
            for m in c['messages']:
                source = refs.pop((cid,m['msgId']))
                self.assertEqual(m['createTime'],source['createTime'])
                self.assertEqual(m['role'],'self' if source['senderId']=='me' else 'peer')
        self.assertFalse(refs)
        by_id = {m['msgId']:m for m in cs['work']['messages']}
        self.assertEqual(set(by_id['media']['contentFlags']), {'image_not_read','call_record_only'})
        self.assertNotIn('mediaId', by_id['media']['text'])
        self.assertIn('请看这里',by_id['media']['text'])
        self.assertEqual(set(by_id['file']['contentFlags']), {'file_not_read','audio_not_read'})
        self.assertIn('说明',by_id['file']['text'])
        self.assertNotIn('mediaId',by_id['file']['text'])
        self.assertEqual(by_id['calendar']['contentFlags'],['calendar_card_acceptance_unknown'])
        self.assertEqual(by_id['reply']['text'],'我研究下哈')

    def test_export_redacts_credentials_without_rewriting_normal_dialogue(self):
        data = self.fixture()
        secrets = ['sk-'+ 'Z'*40,'bearer-secret-0123456789','query-secret-0123456789',
                   'assigned-secret-0123456789','private-key-payload']
        cases = [secrets[0], 'Authorization: Bearer '+secrets[1],
                 'https://example.com/a?access_token='+secrets[2]+'&prompt=hello',
                 '{"client_secret": "'+secrets[3]+'"}',
                 '-----BEGIN PRIVATE KEY-----\n'+secrets[4]+'\n-----END PRIVATE KEY-----',
                 '发布 0.0.58；我研究下哈']
        original = data['conversations'][0]['msgs'][0]
        data['conversations'][0]['msgs'] = [dict(original, msgId='case-'+str(i), text=t) for i,t in enumerate(cases)]
        out = export(data, 'Asia/Shanghai', set())
        text=json.dumps(out, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret,text)
        msgs=out['conversations'][0]['messages']
        self.assertEqual(msgs[-1]['text'],cases[-1])
        self.assertEqual(out['meta']['stats']['redactedMessages'],5)
        self.assertIn('&prompt=hello',msgs[2]['text'])
        self.assertIn('linked_content_not_read',msgs[2]['contentFlags'])

    def test_repeated_encoding_preserves_redacted_text_and_flags(self):
        data = self.fixture()
        data['conversations'][0]['msgs'][0]['text'] = (
            'password=assigned-secret-0123456789 https://example.test/?token=query-secret-0123456789')
        first = export(data, 'Asia/Shanghai', set())
        second = export(chats.decode_dataset(first), 'Asia/Shanghai', set())
        self.assertEqual(first['conversations'], second['conversations'])
        self.assertEqual(first['meta']['stats'], second['meta']['stats'])

    def test_auto_reply_notices_are_flagged_as_suspected(self):
        data = self.fixture()
        original = data['conversations'][0]['msgs'][0]
        data['conversations'][0]['msgs'] = [
            dict(original, msgId='away', text='您好，我现在休假中，消息回复不及时，紧急请电话联系'),
            dict(original, msgId='normal', text='收到，我看下')]
        out = export(data, 'Asia/Shanghai', set())
        msgs = {m['msgId']: m for m in out['conversations'][0]['messages']}
        self.assertEqual(msgs['away']['contentFlags'], ['auto_reply_suspected'])
        self.assertNotIn('contentFlags', msgs['normal'])
        self.assertEqual(out['meta']['stats']['autoReplyMessages'], 1)

    def test_encoder_rejects_unreliable_evidence(self):
        cases = []
        d = self.fixture(); d['conversations'][0]['singleChat'] = False; cases.append((d, set()))
        d = self.fixture(); d['conversations'][0]['msgs'][0]['msgId'] = ''; cases.append((d, set()))
        d = self.fixture(); d['conversations'][0]['msgs'][0]['createTime'] = '2026-10-14 00:00:00'; cases.append((d, set()))
        d = self.fixture(); d['meta']['self'] = {}; cases.append((d, set()))
        cases.append((self.fixture(), {'work'}))
        for data, notes in cases:
            with self.subTest(notes=notes), self.assertRaises(ValueError):
                export(data, 'Asia/Shanghai', notes)

    def test_direct_collection_failure_recovery_update_and_merge_use_agent_baselines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cli=root/'dws'
            cli.write_text('''#!/usr/bin/env python3
import json,sys,os
a=sys.argv;phase=os.environ['PHASE']
if a[2]=='+chat-list':
    assert phase=='collect'
    result={'chats':[{'openConversationId':c,'name':c,'conversationType':'direct','chatMode':'p2p'} for c in ['a','b']], 'complete':False,'paginationKnown':False}
else:
    assert a[2]=='+chat-messages'
    cid=a[a.index('--open-conversation-id')+1]
    start=a[a.index('--start')+1]
    assert cid in ('a','b')
    if phase=='collect' and cid=='b':
        print(json.dumps({'messages':[],'complete':False,'stopReason':'read_failure'}));sys.exit(1)
    if phase=='recover':
        assert cid=='b' and start=='2026-10-13 00:00:00'
    if phase=='update':
        assert start=='2026-10-14 00:00:00'
    date='2026-10-14' if phase=='update' else '2026-10-13'
    text='sk-'+('X'*32)+' [图片消息](mediaId=opaque)' if cid=='a' and phase=='collect' else '完成反馈'
    result={'messages':[{'conversationId':cid,'messageId':cid+'-'+date,'createTime':date+' 01:00:00','senderId':'self' if cid=='a' else 'peer','text':text}], 'complete':True}
print(json.dumps(result))
''')
            cli.chmod(0o700)
            entry=str(ROOT/'scripts/export-dialogues.sh')
            home=root/'home';home.mkdir()
            def run(args,phase):
                proc=subprocess.run(['bash',entry,*args],cwd=root,
                                    env=dict(os.environ,DWS_BIN=str(cli),PHASE=phase,HOME=str(home)),capture_output=True,text=True)
                self.assertEqual(proc.returncode,2,proc.stderr)
            run(['--self-id','self','--start','2026-10-13 00:00:00','--end','2026-10-14 00:00:00',
                 '--retries','1','--no-directory-supplement'],'collect')
            first=home/'.qoder/dws-personal-context/raw/2026-10-13/dialogues.json';data=json.loads(first.read_text())
            self.assertFalse((root/'ctxring-dialogues.json').exists())
            self.assertEqual(data['meta']['format'],'ctxring-dialogues')
            self.assertEqual(data['meta']['schemaVersion'],2)
            self.assertEqual(data['meta']['state']['emptyConversations'][0]['cid'],'b')
            self.assertEqual(data['meta']['coverage']['failedReads'][0]['cid'],'b')
            recovered=root/'recovered.json'
            run(['update','--base',str(first),'--only-failed','--self-id','self',
                 '--end','2026-10-14 00:00:00','--out',str(recovered)],'recover')
            d=json.loads(recovered.read_text())
            self.assertTrue(d['meta']['coverage']['knownConversationsComplete'])
            self.assertEqual(d['meta']['state']['emptyConversations'],[])
            self.assertEqual(d['meta']['coverage']['failedReads'],[])
            updated=root/'updated.json'
            run(['update','--base',str(recovered),'--no-universe','--self-id','self',
                 '--end','2026-10-15 00:00:00','--out',str(updated)],'update')
            merged=root/'merged.json'
            run(['merge','--out',str(merged),str(first),str(recovered),str(updated)],'merge')
            final=json.loads(merged.read_text())
            messages={(c['cid'],m['msgId']):m for c in final['conversations'] for m in c['messages']}
            self.assertEqual(len(messages),4)
            self.assertEqual(final['meta']['stats']['messages'],4)
            original=messages[('a','a-2026-10-13')]
            self.assertEqual(set(original['contentFlags']),{'redacted_credential','image_not_read'})
            self.assertNotIn('X'*32,merged.read_text())
            self.assertEqual(original['role'],'self')
            self.assertEqual(messages[('b','b-2026-10-13')]['role'],'peer')
            self.assertFalse(final['meta']['coverage']['complete'])

