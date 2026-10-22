import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
REFERENCES = ROOT / "references"

LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _split_frontmatter(text):
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end], text[end + 5:]


def _slug(title):
    lowered = title.strip().lower()
    stripped = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", lowered)
    return re.sub(r"\s+", "-", stripped)


def _anchors(path):
    return {_slug(m.group(1)) for m in HEADING_PATTERN.finditer(path.read_text(encoding="utf-8"))}


class SkillDocsTest(unittest.TestCase):
    def test_frontmatter_name_matches_directory(self):
        text = SKILL.read_text(encoding="utf-8")
        frontmatter, _ = _split_frontmatter(text)
        self.assertIsNotNone(frontmatter, "SKILL.md 缺少 frontmatter")
        name = re.search(r"^name:\s*(\S+)\s*$", frontmatter, re.MULTILINE)
        self.assertIsNotNone(name, "frontmatter 缺少 name")
        self.assertEqual(name.group(1), ROOT.name)
        self.assertEqual(name.group(1), "dws-personal-context")
        self.assertRegex(text, r"(?m)^# 钉钉个人上下文与日报$")
        self.assertRegex(name.group(1), r"^[a-z0-9-]+$")

    def test_description_is_single_line_within_limit(self):
        text = SKILL.read_text(encoding="utf-8")
        frontmatter, _ = _split_frontmatter(text)
        self.assertIsNotNone(frontmatter)
        line = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)
        self.assertIsNotNone(line, "frontmatter 缺少 description")
        description = line.group(1).strip().strip('"')
        self.assertNotIn("\n", description)
        self.assertLessEqual(len(description), 1024)
        for keyword in ("上下文", "digest", "写回钉钉文档", "群聊数据不收集"):
            self.assertIn(keyword, description)

    def test_skill_body_stays_small(self):
        _, body = _split_frontmatter(SKILL.read_text(encoding="utf-8"))
        self.assertLessEqual(len(body), 7000, "SKILL.md 正文超过 7000 字符，细节应下沉到 references/")
        self.assertLessEqual(len(body.splitlines()), 110, "SKILL.md 正文超过 110 行")

    def test_required_safety_invariants_present(self):
        text = SKILL.read_text(encoding="utf-8")
        for phrase in (
            "群聊数据不收集",
            "唯一写操作",
            "写回钉钉文档",
            "不发送消息",
            "目录全集未获证明",
            "三态标注",
        ):
            self.assertIn(phrase, text)

    def test_markdown_links_resolve(self):
        docs = [SKILL, *sorted(REFERENCES.glob("*.md"))]
        for doc in docs:
            text = doc.read_text(encoding="utf-8")
            self.assertNotIn("skills/" + "ctxring", text, f"{doc.name} 含旧技能路径")
            for target in LINK_PATTERN.findall(text):
                if re.match(r"^[a-z]+://", target) or target.startswith("mailto:"):
                    continue
                path_part, _, anchor = target.partition("#")
                if not path_part:
                    resolved = doc
                else:
                    resolved = (doc.parent / path_part).resolve()
                self.assertTrue(resolved.exists(), f"{doc.name} 链接不存在：{target}")
                if anchor:
                    self.assertIn(anchor, _anchors(resolved), f"{doc.name} 锚点不存在：{target}")

    def test_no_unresolved_placeholders(self):
        for doc in [SKILL, *sorted(REFERENCES.glob("*.md"))]:
            self.assertNotIn("TODO:", doc.read_text(encoding="utf-8"), f"{doc.name} 含未完成占位符")


if __name__ == "__main__":
    unittest.main()
