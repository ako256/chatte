"""On-demand reusable Markdown Skills and Structured Prompts manager."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
import yaml

from chatte.storage.database import get_data_dir


@dataclass
class Skill:
    slug: str
    name: str
    description: str
    icon: str
    instructions: str
    file_path: Path


@dataclass
class StructuredPrompt:
    id: str
    title: str
    description: str
    icon: str
    template: str


STRUCTURED_PROMPTS: list[StructuredPrompt] = [
    StructuredPrompt(
        id="code-review",
        title="Rigorous Code Review",
        description="Analyze correctness, edge cases, security, and idiomatic style",
        icon=" utilities-terminal-symbolic",
        template=(
            "### Structured Code Review Request\n"
            "**Language / Stack:** [e.g., Python 3.12 / GTK4]\n"
            "**Goal of the Code:** [Describe what this code does]\n\n"
            "Please evaluate the following code across:\n"
            "1. **Correctness & Edge Cases**\n"
            "2. **Security & Memory/Resource Safety**\n"
            "3. **Performance & Algorithmic Complexity**\n"
            "4. **Refactored Implementation**\n\n"
            "```\n[Paste code here]\n```"
        ),
    ),
    StructuredPrompt(
        id="math-derivation",
        title="Step-by-Step Mathematical Proof",
        description="Derive equations clearly using LaTeX display math",
        icon="accessories-calculator-symbolic",
        template=(
            "### Mathematical Derivation & Proof\n"
            "**Problem Statement:**\n"
            "$$\n[Write equation or theorem here]\n$$\n\n"
            "**Requirements:**\n"
            "- State all assumptions and definitions clearly.\n"
            "- Show every algebraic / calculus step in display LaTeX (`$$...$$`).\n"
            "- Provide intuition and verify boundary conditions."
        ),
    ),
    StructuredPrompt(
        id="research-synthesis",
        title="Deep Research with Citations",
        description="Synthesize verified facts and cite sources inline",
        icon="system-search-symbolic",
        template=(
            "### Deep Research Synthesis\n"
            "**Topic / Question:** [Enter research topic]\n"
            "**Scope & Constraints:** [Key aspects to focus on]\n\n"
            "Please search for authoritative sources, synthesize the findings into structured "
            "sections, compare competing viewpoints, and cite all claims inline with `[1]`, `[2]`."
        ),
    ),
    StructuredPrompt(
        id="root-cause-debug",
        title="Root-Cause Bug Investigation",
        description="Systematic hypothesis testing and minimal fix",
        icon="dialog-warning-symbolic",
        template=(
            "### Bug Root-Cause Analysis\n"
            "**Observed Symptom / Traceback:**\n"
            "```\n[Paste error or symptom]\n```\n"
            "**Expected Behavior:** [What should happen instead]\n\n"
            "Please identify:\n"
            "1. The exact root cause.\n"
            "2. Why existing guards failed.\n"
            "3. Minimal, verified fix with regression test."
        ),
    ),
]


DEFAULT_SKILLS: list[tuple[str, str, str, str, str]] = [
    (
        "code-architect",
        "Code Architect",
        "Clean, modular, production-grade software architecture and code",
        "applications-engineering-symbolic",
        """You are operating with the **Code Architect** skill active:
- Write complete, self-contained, idiomatic code with clear type annotations.
- Favor simplicity, explicit error handling, and clean separation of concerns.
- Always analyze time and space complexity (`O(...)`) for non-trivial algorithms.
- Avoid placeholders or truncated snippets unless explicitly asked.""",
    ),
    (
        "math-latex-expert",
        "Math & Physics Rigor",
        "Format derivations with clean inline ($...$) and display ($$...$$) LaTeX",
        "accessories-calculator-symbolic",
        """You are operating with the **Math & Physics Rigor** skill active:
- Use `$...$` for inline variables and equations, and `$$...$$` on its own lines for display equations.
- Express fractions with `\\frac{a}{b}`, roots with `\\sqrt{x}`, integrals with `\\int_{a}^{b}`, sums with `\\sum_{i=1}^{n}`, and matrices with `\\begin{pmatrix}...\\end{pmatrix}`.
- Define every symbol introduced and state conservation laws or assumptions explicitly.""",
    ),
    (
        "gnome-hig-specialist",
        "GNOME 50 & Linux Specialist",
        "Expert guidance on GTK 4, Libadwaita, PyGObject, systemd, and Linux",
        "computer-symbolic",
        """You are operating with the **GNOME 50 & Linux Specialist** skill active:
- Follow the GNOME Human Interface Guidelines (HIG) and modern Libadwaita 1.9 widget patterns (`Adw.NavigationSplitView`, `Adw.ToolbarView`, `Adw.PreferencesDialog`, `Adw.AlertDialog`).
- Prefer native GIO/GLib APIs, XDG Base Directory specifications, and freedesktop.org standards (`libsecret` for credentials).""",
    ),
    (
        "concise-technical-editor",
        "Concise Technical Writer",
        "Direct, high-signal explanations with zero filler",
        "document-edit-symbolic",
        """You are operating with the **Concise Technical Writer** skill active:
- Lead with the direct answer or solution in the very first sentence.
- Use structured Markdown tables and bullet points for comparisons.
- Eliminate conversational filler, hedging, and repetition.""",
    ),
]


class SkillsManager:
    """Loads, saves, and formats reusable Markdown skills on-demand."""

    def __init__(self, skills_dir: Path | None = None) -> None:
        self.skills_dir = skills_dir or (get_data_dir() / "skills")
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._seed_default_skills()

    def _seed_default_skills(self) -> None:
        existing = list(self.skills_dir.glob("*.md"))
        if existing:
            return
        for slug, name, desc, icon, body in DEFAULT_SKILLS:
            self.save_skill(
                slug=slug,
                name=name,
                description=desc,
                icon=icon,
                instructions=body,
            )

    def list_skills(self) -> list[Skill]:
        skills: list[Skill] = []
        for path in sorted(self.skills_dir.glob("*.md")):
            skill = self._parse_skill_file(path)
            if skill:
                skills.append(skill)
        return skills

    def get_skill(self, slug: str) -> Skill | None:
        path = self.skills_dir / f"{slug}.md"
        if not path.exists():
            for sk in self.list_skills():
                if sk.slug == slug:
                    return sk
            return None
        return self._parse_skill_file(path)

    def load_active_skills_markdown(self, active_slugs: list[str]) -> str:
        """Load the Markdown instructions for the selected skills on-demand."""
        if not active_slugs:
            return ""
        blocks: list[str] = []
        for slug in active_slugs:
            sk = self.get_skill(slug)
            if sk:
                blocks.append(f"### Skill: {sk.name}\n{sk.instructions.strip()}")
        return "\n\n".join(blocks)

    def save_skill(
        self,
        slug: str,
        name: str,
        description: str,
        instructions: str,
        icon: str = "starred-symbolic",
    ) -> Skill:
        safe_slug = re.sub(r"[^a-z0-9_-]+", "-", slug.lower().strip()).strip("-")
        if not safe_slug:
            safe_slug = "custom-skill"
        path = self.skills_dir / f"{safe_slug}.md"
        frontmatter = {
            "name": name.strip() or safe_slug,
            "description": description.strip(),
            "icon": icon.strip() or "starred-symbolic",
        }
        yaml_text = yaml.safe_dump(frontmatter, sort_keys=False).strip()
        content = f"---\n{yaml_text}\n---\n\n{instructions.strip()}\n"
        path.write_text(content, encoding="utf-8")
        return Skill(
            slug=safe_slug,
            name=frontmatter["name"],
            description=frontmatter["description"],
            icon=frontmatter["icon"],
            instructions=instructions.strip(),
            file_path=path,
        )

    def delete_skill(self, slug: str) -> bool:
        path = self.skills_dir / f"{slug}.md"
        if path.exists():
            path.unlink()
            return True
        return False

    @staticmethod
    def _parse_skill_file(path: Path) -> Skill | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None

        slug = path.stem
        name = slug.replace("-", " ").title()
        description = ""
        icon = "starred-symbolic"
        body = raw.strip()

        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    if isinstance(meta, dict):
                        name = str(meta.get("name") or name)
                        description = str(meta.get("description") or "")
                        icon = str(meta.get("icon") or icon)
                except Exception:
                    pass
                body = parts[2].strip()

        return Skill(
            slug=slug,
            name=name,
            description=description,
            icon=icon,
            instructions=body,
            file_path=path,
        )

