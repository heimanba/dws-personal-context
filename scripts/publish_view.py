#!/usr/bin/env python3
"""digest 发布视图：把机器 ID 来源引用压缩成读者友好的标签。

本地 digest 是完整取证版（cite 原文照抄），不因发布改写；发布前用本脚本生成
发布视图，只压缩来源引用，事实内容与 URL 一律不动：

- `YYYY-MM-DD HH:MM:SS | peer/self 名字 | chat:cid msg:msg` → `私聊·名字 MM-DD HH:MM`
- 裸 `chat:cid msg:msg`（表格/TODO 形态）→ `私聊·人名`：按 digest 文件名对应的
  `raw/<日>/context.index.json` 会话标题解析（`--root` 可覆盖）；人名已在引用前缀时
  不重复；cid 尾部 `…` 截断时按唯一前缀匹配；查不到回退 私聊
- 裸 `mail:x` / `minutes:x` / `calendar:x` → 邮件 / 听记 / 日程
- digest 可读性格式 `渠道·名称（IDs）` → 剥离 ID 括号，保留标签
- 同一来源多个 msg（`chat:cid msg:a / msg:b`）折叠；连续相同标签折叠为 `标签×N`

转换后仍残留已知形态机器 ID 时以非零码退出（发布前硬门禁），宁可不发布也不带 ID 出门。
"""
import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

CID = r'chat:(?P<cid>[A-Za-z0-9+/=]+(?:…|\.{3})?)'
MSG = r'msg:[A-Za-z0-9+/=]+'

FULL_CHAT = re.compile(
    r'(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):\d{2} \| (?:peer|self) (.+?) \| '
    + CID + ' ' + MSG)

BARE_CHAT = re.compile(
    r'(?:(?P<pre>[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9]{0,15}) )?' + CID + r'(?: ' + MSG + r')?')

BARE_IDS = (
    (re.compile(r'\bmail:[A-Za-z0-9.-]+'), '邮件'),
    (re.compile(r'\bminutes:[0-9a-fA-F]+'), '听记'),
    (re.compile(r'\bcalendar:[A-Za-z0-9+/=]+'), '日程'),
)

MSG_SLASH = re.compile(r' ?/ ?' + MSG)

PAREN = re.compile(r'[（(]([^（）()]*)[）)]')
ID_ONLY = re.compile(r'^(?:(?:chat|msg|mail|minutes|calendar):[A-Za-z0-9+/=.\-…]+[\s;；、]*)+$')

RESIDUAL = re.compile(r'\b(?:chat|msg|mail|minutes|calendar):[A-Za-z0-9+/=-]')


def _full_chat_sub(match):
    month, day, hour, minute, name = (match.group(i) for i in range(2, 7))
    return '私聊·%s %s-%s %s:%s' % (name.strip(), month, day, hour, minute)


def _strip_id_parens(text):
    def repl(match):
        inner = match.group(1).strip()
        return '' if (inner and ID_ONLY.fullmatch(inner)) else match.group(0)
    return PAREN.sub(repl, text)


def _collapse_runs(text):
    labels = []
    for _, label in BARE_IDS:
        if label not in labels:
            labels.append(label)
    labels.append('私聊')
    for label in labels:
        pattern = re.compile('(%s)(?:[、；]\\1)+' % re.escape(label))
        text = pattern.sub(
            lambda m: '%s×%d' % (m.group(1), m.group(0).count('、') + m.group(0).count('；') + 1),
            text)
    return text


def _window_days(stem):
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:_(\d{4}-\d{2}-\d{2}))?', stem)
    if not match:
        return []
    start = date.fromisoformat(match.group(1))
    end = date.fromisoformat(match.group(2) or match.group(1))
    days, current = [], start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def load_chat_titles(root, days):
    titles = {}
    for day in days:
        path = Path(root) / day / 'context.index.json'
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        entries = ((data.get('sources') or {}).get('chat') or {}).get('entries') or []
        for entry in entries:
            cid, title = entry.get('id'), entry.get('title')
            if cid and title:
                titles[cid] = str(title).replace('|', '·').strip()
    return titles


def _resolve_title(titles, cid_text):
    if cid_text.endswith('…'):
        core, truncated = cid_text[:-1], True
    elif cid_text.endswith('...'):
        core, truncated = cid_text[:-3], True
    else:
        core, truncated = cid_text, False
    if core in titles:
        return titles[core]
    if truncated:
        matches = [cid for cid in titles if cid.startswith(core)]
        if len(matches) == 1:
            return titles[matches[0]]
    return None


def _chat_substitute(titles):
    def substitute(match):
        prefix, cid_text = match.group('pre'), match.group('cid')
        title = _resolve_title(titles, cid_text)
        if prefix:
            if title and title != prefix:
                return '%s 私聊·%s' % (prefix, title)
            return '%s 私聊' % prefix
        return '私聊·%s' % title if title else '私聊'
    return substitute


# Protect fenced code, inline code and URL targets before rewriting citations.
PROTECTED = re.compile(
    r'(?P<fenced>^[ ]{0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n'
    r'.*?(?:^[ ]{0,3}(?P=fence)[`~]*[ \t]*(?:\n|$)|\Z))'
    r'|(?P<indented>^(?: {4}|\t)[^\n]*(?:\n|$))'
    r'|(?P<destination>\]\((?:\\.|[^()\\]|\((?:\\.|[^()\\])*\))*\))'
    r'|(?P<reference>^[ ]{0,3}\[[^]\n]+\]:[^\n]*)'
    r'|(?P<inline>(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`))'
    r'|(?P<url>https?://[^\s<>`"\u3000-\u303f\uff00-\uffef]+)',
    re.MULTILINE | re.DOTALL,
)


def _list_continuation(text, position, part):
    """Four spaces may continue a list item rather than start a code block."""
    indent = len(part.expandtabs(4)) - len(part.expandtabs(4).lstrip(' '))
    for line in reversed(text[:position].splitlines()):
        if not line.strip():
            continue
        expanded = line.expandtabs(4)
        marker = re.match(r'^( *)(?:[-+*]|[0-9]+[.)]) +', expanded)
        if marker and len(marker.group(1)) < indent:
            return marker.end() <= indent < marker.end() + 4
        if expanded and not expanded.startswith(' '):
            return False
    return False


def _segments(text):
    start = 0
    for match in PROTECTED.finditer(text):
        yield False, text[start:match.start()]
        part = match.group(0)
        continuation = (match.group('indented') is not None
                        and _list_continuation(text, match.start(), part))
        yield not continuation, part
        start = match.end()
    yield False, text[start:]


def citation_text(text):
    """Text scanned by the residual-ID gate; URLs and code are not citations."""
    return ''.join(re.sub(r'[^\n]', ' ', part) if protected else part
                   for protected, part in _segments(text))


def _transform_citations(text, titles=None):
    titles = titles or {}
    text = FULL_CHAT.sub(_full_chat_sub, text)
    text = _strip_id_parens(text)
    text = BARE_CHAT.sub(_chat_substitute(titles), text)
    for pattern, label in BARE_IDS:
        text = pattern.sub(label, text)
    return _collapse_runs(MSG_SLASH.sub('', text))


def transform(text, titles=None):
    return ''.join(part if protected else _transform_citations(part, titles)
                   for protected, part in _segments(text))


def find_residual(text):
    return [line for line in citation_text(text).splitlines() if RESIDUAL.search(line)]


def main(argv=None):
    parser = argparse.ArgumentParser(description='生成 digest 的发布视图（压缩机器 ID 来源引用）')
    parser.add_argument('digest', help='本地 digest .md 文件路径')
    parser.add_argument('--root', default=str(Path.home() / '.qoder/dws-personal-context/raw'),
                        help='raw 数据根目录；按 digest 文件名定位各日 context.index.json 做人名富化')
    args = parser.parse_args(argv)
    digest_path = Path(args.digest)
    with open(digest_path, encoding='utf-8') as handle:
        text = handle.read()
    titles = load_chat_titles(args.root, _window_days(digest_path.stem))
    view = transform(text, titles)
    residual = find_residual(view)
    if residual:
        for line in residual:
            print('残留机器 ID：%s' % line, file=sys.stderr)
        return 1
    sys.stdout.write(view)
    return 0


if __name__ == '__main__':
    sys.exit(main())
