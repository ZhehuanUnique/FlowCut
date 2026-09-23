"""Static package invariants for naming, routing, and public portability."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "flowcut"


class SkillMetadataTests(unittest.TestCase):
    def test_canonical_name_and_alias_discovery(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = re.match(r"^---\n(.*?)\n---", skill, re.DOTALL)
        self.assertIsNotNone(frontmatter)
        header = frontmatter.group(1)
        self.assertRegex(header, r"(?m)^name:\s*flowcut\s*$")
        for label in ("FlowCut", "口播Skill", "koubo-skill"):
            self.assertIn(label, header)
        readme = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn("正式技能标识仍是 `flowcut`", readme)
        self.assertIn("显式调用写作 `$flowcut`", readme)

    def test_default_prompt_uses_canonical_invocation(self):
        metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("FlowCut · 口播Skill", metadata)
        self.assertIn("$flowcut", metadata)
        self.assertNotIn("$koubo-skill", metadata)

    def test_speed_is_not_a_default_editing_rule(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("默认保持素材原速", skill)
        self.assertIn("只有用户对当前任务明确提出变速时", skill)
        self.assertNotRegex(skill, r"1[.]5\s*(?:倍|x)")

    def test_public_package_has_no_absolute_windows_path(self):
        text_files = list(SKILL_ROOT.rglob("*.md")) + list(SKILL_ROOT.rglob("*.py")) + list(SKILL_ROOT.rglob("*.yaml"))
        combined = "\n".join(path.read_text(encoding="utf-8") for path in text_files)
        self.assertIsNone(re.search(r"(?i)\b[a-z]:[\\/]", combined))


if __name__ == "__main__":
    unittest.main(verbosity=2)
