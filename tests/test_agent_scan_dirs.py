"""_agent_scan_dirs must be reachable from production. Issue #161 agents half.

_agent_scan_dirs existed but was never called from measure_components(), so
project-scoped and global agents were never counted. This test verifies the
wiring: agents in ~/.claude/agents/ and <cwd>/.claude/agents/ are now counted
in components["agents"].

Run: python3 -m pytest tests/test_gap7_agent_scan.py -v
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tempfile.mkdtemp(prefix="to-gap7-"))
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    import measure
    return measure


def _make_agent(root: Path, name: str, description: str = "an agent"):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n",
        encoding="utf-8",
    )


def test_agents_are_counted_in_measure_components(m, tmp_path, monkeypatch):
    """Agents in both global and project dirs must appear in components['agents']."""
    # Set up a fake global agents dir
    fake_home = tmp_path / "home"
    fake_claude = fake_home / ".claude"
    fake_claude.mkdir(parents=True)
    _make_agent(fake_claude / "agents", "global-agent-1")
    _make_agent(fake_claude / "agents", "global-agent-2")

    # Set up a project with its own agents
    proj = tmp_path / "project"
    (proj / ".claude").mkdir(parents=True)
    _make_agent(proj / ".claude" / "agents", "project-agent-1")

    monkeypatch.setattr(m, "CLAUDE_DIR", fake_claude)
    monkeypatch.chdir(proj)

    # Call measure_components (the function that builds the inventory)
    result = m.measure_components()

    assert "agents" in result, "components must have an 'agents' key"
    agents = result["agents"]
    assert agents["count"] == 3, (
        f"Expected 3 agents (2 global + 1 project), got {agents['count']}. "
        f"Names: {agents['names']}"
    )
    assert "global-agent-1" in agents["names"]
    assert "global-agent-2" in agents["names"]
    assert "project-agent-1" in agents["names"]
    assert agents["tokens"] > 0, "agent frontmatter tokens must be counted"


def test_agents_absent_when_no_agent_dirs(m, tmp_path, monkeypatch):
    """When no agents dirs exist, components['agents'] reports zero, not missing."""
    fake_home = tmp_path / "home"
    fake_claude = fake_home / ".claude"
    fake_claude.mkdir(parents=True)
    monkeypatch.setattr(m, "CLAUDE_DIR", fake_claude)
    monkeypatch.chdir(tmp_path)

    result = m.measure_components()

    assert "agents" in result, "agents key must always exist"
    assert result["agents"]["count"] == 0
    assert result["agents"]["names"] == []
