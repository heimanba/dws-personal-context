"""Conservative Markdown equivalence for full publication readback.

Exact equality needs no dependency. Format-only recovery uses markdown-it-py;
if unavailable, callers retain partial-success status rather than guess.
"""
import re

# Check literal Markdown link destinations as well as parsed link attributes.
LINK = re.compile(r'\]\(([^)]*)\)')


def equivalence(expected, actual):
    if expected == actual:
        return 'exact'
    if '\x00' in expected or '\x00' in actual:
        return None
    if LINK.findall(expected) != LINK.findall(actual):
        return None
    try:
        from markdown_it import MarkdownIt
        from markdown_it.rules_block.table import escapedSplit
    except ImportError:
        return None
    parser = MarkdownIt('commonmark').enable('table')

    def tokens(items):
        return [(
            item.type, item.tag, item.nesting, item.attrs,
            item.content if item.type != 'inline' else '',
            item.info if item.type in ('fence', 'code_block') else '',
            tokens(item.children) if item.children else [],
        ) for item in items]

    def parse_losslessly(text):
        env = {}
        parsed = parser.parse(text, env)
        lines = text.splitlines()
        definitions = []
        for label, ref in env.get('references', {}).items():
            definitions.append((label, tuple(line.strip() for line in lines[slice(*ref['map'])])))
        for ref in env.get('duplicate_refs', []):
            definitions.append((ref['label'], tuple(line.strip() for line in lines[slice(*ref['map'])])))
        for token in parsed:
            if token.type != 'table_open':
                continue
            start, end = token.map
            # The table rule discards overflow cells and pads missing cells.
            # Reject ragged rows rather than trusting that lossy projection.
            widths = []
            for line in lines[start:end]:
                row = line.strip()
                if row.startswith('|'):
                    row = row[1:]
                if row.endswith('|') and not row.endswith(r'\|'):
                    row = row[:-1]
                widths.append(len(escapedSplit(row)))
            if len(set(widths)) != 1:
                return None
        return tokens(parsed), sorted(definitions)

    # Ignore only source positions, delimiter spelling and loose-list wrappers.
    # Text spacing, code, links, headings, lists and table cell structure remain.
    left, right = parse_losslessly(expected), parse_losslessly(actual)
    if left is not None and right is not None and left == right:
        return 'normalized'
    return None
