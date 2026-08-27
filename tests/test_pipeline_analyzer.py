#!/usr/bin/env python3
"""UNIT B: Pipeline analyzer tests — corpus + eligibility measurement.

Validates that pipeline_analyzer correctly classifies read-only pipelines
and rejects side-effecting commands. Includes the 153-command corpus shapes
from the finding and measures eligibility rate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the scripts dir is importable
SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pipeline_analyzer import (
    get_pipeline_eligibility,
    is_read_only_pipeline,
    _split_stages,
    _strip_redirections,
    _is_redirect_token,
    _is_stage_read_only,
)


# ============================================================================
# Unit tests: token classification
# ============================================================================

class TestRedirectTokens:
    def test_standard_redirects(self):
        assert _is_redirect_token(">")
        assert _is_redirect_token(">>")
        assert _is_redirect_token("<")
        assert _is_redirect_token("<<")

    def test_numeric_redirects(self):
        assert _is_redirect_token("2>&1")
        assert _is_redirect_token("1>&2")
        assert _is_redirect_token("2>/dev/null")
        assert _is_redirect_token("2>&1")

    def test_non_redirects(self):
        assert not _is_redirect_token("file.txt")
        assert not _is_redirect_token("--option")
        assert not _is_redirect_token("git")
        assert not _is_redirect_token("2")  # bare number is not a redirect

    def test_redirect_stripping_basic(self):
        tokens = ["command", "2>&1"]
        assert _strip_redirections(tokens) == ["command"]

    def test_redirect_stripping_with_target(self):
        tokens = ["command", ">", "/dev/null"]
        assert _strip_redirections(tokens) == ["command"]

    def test_redirect_stripping_mixed(self):
        tokens = ["git", "log", "2>&1", "|", "head"]
        assert _strip_redirections(tokens) == ["git", "log", "|", "head"]


class TestStageSplitting:
    def test_simple_pipe(self):
        tokens = ["git", "log", "|", "head"]
        stages = _split_stages(tokens)
        assert len(stages) == 2
        assert stages[0] == ["git", "log"]
        assert stages[1] == ["head"]

    def test_double_ampersand(self):
        tokens = ["git", "fetch", "&&", "git", "status"]
        stages = _split_stages(tokens)
        assert len(stages) == 2
        assert stages[0] == ["git", "fetch"]
        assert stages[1] == ["git", "status"]

    def test_semicolon(self):
        tokens = ["echo", "hello", ";", "ls", "-la"]
        stages = _split_stages(tokens)
        assert len(stages) == 2
        assert stages[0] == ["echo", "hello"]
        assert stages[1] == ["ls", "-la"]

    def test_multi_pipe(self):
        tokens = ["grep", "-r", "TODO", "src/", "|", "sort", "|", "uniq", "-c"]
        stages = _split_stages(tokens)
        assert len(stages) == 3

    def test_no_separator(self):
        tokens = ["git", "status"]
        stages = _split_stages(tokens)
        assert len(stages) == 1
        assert stages[0] == ["git", "status"]


# ============================================================================
# Stage-level read-only classification
# ============================================================================

class TestStageReadOnly:
    def test_git_log_is_read_only(self):
        ok, reason = _is_stage_read_only(["git", "log", "--oneline"])
        assert ok, f"git log should be read-only, got: {reason}"

    def test_git_push_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["git", "push"])
        assert not ok, f"git push should NOT be read-only"

    def test_head_is_read_only(self):
        ok, reason = _is_stage_read_only(["head", "-20"])
        assert ok, f"head should be read-only, got: {reason}"

    def test_sort_is_read_only(self):
        ok, reason = _is_stage_read_only(["sort", "-rn"])
        assert ok, f"sort should be read-only, got: {reason}"

    def test_sed_without_i_is_read_only(self):
        ok, reason = _is_stage_read_only(["sed", "s/foo/bar/g"])
        assert ok, f"sed without -i should be read-only, got: {reason}"

    def test_sed_with_i_flag_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["sed", "-i", "s/foo/bar/g", "file.txt"])
        assert not ok, "sed -i should NOT be read-only"

    def test_python_script_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["python", "script.py"])
        assert not ok, "python script.py should NOT be read-only"

    def test_python_m_pytest_is_read_only(self):
        ok, reason = _is_stage_read_only(["python", "-m", "pytest"])
        assert ok, f"python -m pytest should be read-only, got: {reason}"

    def test_bash_is_never_read_only(self):
        ok, reason = _is_stage_read_only(["bash", "-c", "ls"])
        assert not ok, "bash -c should NEVER be read-only"

    def test_sudo_is_never_read_only(self):
        ok, reason = _is_stage_read_only(["sudo", "ls"])
        assert not ok, "sudo should NEVER be read-only"

    def test_npm_test_is_read_only(self):
        ok, reason = _is_stage_read_only(["npm", "test"])
        assert ok, f"npm test should be read-only, got: {reason}"

    def test_cat_is_read_only(self):
        ok, reason = _is_stage_read_only(["cat", "file.txt"])
        assert ok, f"cat file.txt should be read-only, got: {reason}"

    def test_echo_is_read_only(self):
        ok, reason = _is_stage_read_only(["echo", "hello"])
        assert ok, f"echo should be read-only, got: {reason}"

    def test_unknown_command_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["some-random-tool", "--flag"])
        assert not ok, "unknown command should NOT be read-only"

    def test_empty_stage(self):
        ok, reason = _is_stage_read_only([])
        assert not ok

    def test_grep_is_read_only(self):
        ok, reason = _is_stage_read_only(["grep", "-r", "pattern"])
        assert ok, f"grep should be read-only, got: {reason}"

    def test_wc_is_read_only(self):
        ok, reason = _is_stage_read_only(["wc", "-l"])
        assert ok, f"wc should be read-only, got: {reason}"

    def test_awk_is_read_only(self):
        ok, reason = _is_stage_read_only(["awk", "{print $1}"])
        assert ok, f"awk should be read-only, got: {reason}"

    def test_docker_compose_up_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["docker", "compose", "up"])
        assert not ok, "docker compose up should NOT be read-only"

    def test_docker_compose_ps_is_read_only(self):
        ok, reason = _is_stage_read_only(["docker", "compose", "ps"])
        assert ok, f"docker compose ps should be read-only, got: {reason}"

    def test_terraform_plan_is_read_only(self):
        ok, reason = _is_stage_read_only(["terraform", "plan"])
        assert ok, f"terraform plan should be read-only, got: {reason}"

    def test_kubectl_get_is_read_only(self):
        ok, reason = _is_stage_read_only(["kubectl", "get", "pods"])
        assert ok, f"kubectl get should be read-only, got: {reason}"

    def test_kubectl_delete_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["kubectl", "delete", "pod"])
        assert not ok, "kubectl delete should NOT be read-only"


# ============================================================================
# Pipeline-level classification: CORPUS TESTS
# ============================================================================

# Read-only pipelines: should ALL be eligible
READ_ONLY_PIPELINES = [
    # Pipes (previously excluded by |)
    ("git log --oneline | head -20", "git log piped to head"),
    ("cat package.json | grep version", "cat piped to grep"),
    ("find src -name '*.py' | xargs grep TODO", "find + xargs + grep"),
    ("ps aux | grep node | wc -l", "ps + grep + wc"),
    ("grep -r FIXME src/ | sort | uniq -c | sort -rn | head -10", "multi-stage pipeline"),
    ("git log --oneline | tail -5", "git log piped to tail"),
    ("ls -la | wc -l", "ls piped to wc"),
    ("git diff HEAD~1 | grep '^+' | wc -l", "git diff + grep + wc"),
    ("du -sh * | sort -rh | head -10", "du + sort + head"),
    ("cat file.txt | tr '[:lower:]' '[:upper:]'", "cat + tr"),
    ("git branch -a | grep -v '^*' | sort", "git branch + grep + sort"),

    # Redirects (previously excluded by >, 2>&1)
    ("npm test 2>&1 | grep -E '(passing|failing)'", "npm test with stderr redirect"),

    # List operators with all read-only stages
    ("git status && ls -la", "git status && ls"),
    ("echo 'build info' && git log -1", "echo && git log"),
    ("pwd && ls && git branch", "pwd && ls && git branch"),
    ("date && whoami && hostname", "system info chain"),

    # Command substitution with read-only content
    ("echo Node version: $(node --version)", "echo with node version substitution"),

    # Pipeline consumers
    ("git log --format='%h %s' | column -t", "git log + column"),
    ("cat data.csv | cut -d',' -f1,3 | sort | uniq -c", "CSV pipeline"),
    ("awk '{print $1}' access.log | sort | uniq -c | sort -rn | head -5", "awk + sort + uniq + sort + head"),

    # Search pipelines
    ("rg 'TODO' src/ | sort", "ripgrep + sort"),
    ("grep -rn 'import.*from' --include='*.ts' . | head -20", "grep + head"),
    ("git grep 'FIXME' | sort -u", "git grep + sort -u"),

    # Docker read-only
    ("docker ps | grep api", "docker ps + grep"),
    ("docker images | head -10", "docker images + head"),

    # kubectl read-only
    ("kubectl get pods | grep Running | wc -l", "kubectl get + grep + wc"),
    ("kubectl describe pod my-pod | grep 'Image:'", "kubectl describe + grep"),

    # find + exec-like (xargs with read-only)
    ("find . -name '*.py' | xargs wc -l | sort -rn | head -5", "find + xargs wc + sort + head"),
]

# Side-effecting / dangerous commands: should ALL be rejected
SIDE_EFFECTING = [
    # Git write commands
    ("git push origin main", "git push is a write"),
    ("git commit -m 'fix'", "git commit is a write"),
    ("git add . && git commit -m 'update'", "git add + commit chain"),
    ("git fetch && git checkout main", "git fetch is a side effect"),

    # Mixed pipeline (read-only pipe, write first stage)
    ("rm -rf build/ | wc -l", "rm is destructive"),
    ("mkdir -p dist && ls dist", "mkdir is a write"),
    ("npm install express | tail -5", "npm install has side effects"),
    ("pip install requests 2>&1 | grep Success", "pip install has side effects"),

    # Shell interpreters
    ("bash -c 'ls | grep foo'", "bash is a shell interpreter"),
    ("sh script.sh | head", "sh is a shell interpreter"),

    # Privilege escalation
    ("sudo ls /root", "sudo is privilege escalation"),

    # Node with -e (arbitrary code)
    ("node -e 'console.log(1)' | head", "node -e is arbitrary code"),

    # Python as interpreter (not -m known-module)
    ("python script.py 2>&1 | tail -30", "python script.py is arbitrary code"),

    # sed -i (in-place edit)
    ("sed -i 's/old/new/g' file.txt", "sed -i is a write"),

    # docker compose side-effecting
    ("docker compose up -d && docker compose ps", "docker compose up has side effects"),

    # kubectl delete
    ("kubectl delete pod my-pod && kubectl get pods", "kubectl delete has side effects"),

    # Terraform apply (write)
    ("terraform apply -auto-approve 2>&1 | tail", "terraform apply is a write"),

    # Mixed safe + unsafe
    ("docker compose build && docker compose ps", "docker compose build has side effects"),
    ("git log --oneline | head && git commit -m 'update'", "mixed: git log + git commit"),
    ("pytest tests/ && git add .", "pytest + git add (write)"),
]

# Edge cases that should be rejected gracefully
EDGE_CASES = [
    ("", "empty string"),
    ("   ", "whitespace only"),
    ("", "really empty"),  # already tested
]


class TestPipelineEligibility:
    """Corpus-based eligibility tests."""

    @pytest.mark.parametrize("command,description", READ_ONLY_PIPELINES)
    def test_read_only_pipeline_is_eligible(self, command, description):
        is_ro, reason = is_read_only_pipeline(command)
        assert is_ro, (
            f"Expected '{description}' to be eligible, but rejected: {reason}\n"
            f"Command: {command}"
        )

    @pytest.mark.parametrize("command,description", SIDE_EFFECTING)
    def test_side_effecting_pipeline_is_rejected(self, command, description):
        is_ro, reason = is_read_only_pipeline(command)
        assert not is_ro, (
            f"Expected '{description}' to be REJECTED, but it was accepted.\n"
            f"Command: {command}\n"
            f"Reason given: {reason}"
        )

    def test_empty_command(self):
        is_ro, reason = is_read_only_pipeline("")
        assert not is_ro

    def test_unparseable_command(self):
        # Malformed quoting should be rejected gracefully
        is_ro, reason = is_read_only_pipeline("echo 'unclosed quote")
        assert not is_ro
        assert "unparseable" in reason.lower() or "quoting" in reason.lower()


# ============================================================================
# CORPUS ELIGIBILITY MEASUREMENT
# ============================================================================

def test_eligibility_rate_on_corpus():
    """UNIT B eligibility rate must be FAR above the 1.3% baseline.

    This test asserts that on the READ_ONLY_PIPELINES corpus (which represents
    the shapes that were previously categorically excluded by metachar detection
    in bash_hook.py), the new pipeline_analyzer correctly identifies them as
    eligible.
    """
    total = len(READ_ONLY_PIPELINES)
    eligible = 0
    failures = []

    for cmd, desc in READ_ONLY_PIPELINES:
        is_ro, reason = is_read_only_pipeline(cmd)
        if is_ro:
            eligible += 1
        else:
            failures.append(f"  {desc}: {reason}")

    rate = eligible / total * 100 if total > 0 else 0

    # Report for the record
    print(f"\n=== CORPUS ELIGIBILITY ===")
    print(f"Total read-only pipeline commands: {total}")
    print(f"Eligible for compression: {eligible}")
    print(f"Eligibility rate: {rate:.1f}%")
    if failures:
        print(f"Rejected ({len(failures)}):")
        for f in failures:
            print(f)

    # The target from the design: >50% on the corpus (up from 1.3%)
    # We should actually achieve 100% since all commands in READ_ONLY_PIPELINES
    # are deliberately read-only. But some may be rejected due to conservative
    # classification; the bar is set at >80% to allow for edge cases.
    assert rate > 80.0, (
        f"Eligibility rate {rate:.1f}% is too low. Target: >80%.\n"
        f"Failed commands: {failures}"
    )


def test_safety_zero_false_positives():
    """ALL side-effecting commands must be rejected. Zero tolerance."""
    total = len(SIDE_EFFECTING)
    false_positives = []

    for cmd, desc in SIDE_EFFECTING:
        is_ro, reason = is_read_only_pipeline(cmd)
        if is_ro:
            false_positives.append(f"  {desc}: incorrectly classified as read-only")

    rate = len(false_positives) / total * 100 if total > 0 else 0
    print(f"\n=== SAFETY VIOLATIONS ===")
    print(f"Total side-effecting commands tested: {total}")
    print(f"False positives (classified read-only): {len(false_positives)}")
    if false_positives:
        for fp in false_positives:
            print(fp)

    assert len(false_positives) == 0, (
        f"SAFETY VIOLATION: {len(false_positives)} side-effecting commands "
        f"were incorrectly classified as read-only!"
    )


# ============================================================================
# Diagnostic function tests
# ============================================================================

class TestDiagnosticFunction:
    def test_eligible_pipeline_has_detailed_stages(self):
        result = get_pipeline_eligibility("git log | head -20")
        assert result["is_eligible"] is True
        assert result["stage_count"] == 2
        assert len(result["stages"]) == 2
        assert result["stages"][0]["is_read_only"] is True
        assert result["stages"][1]["is_read_only"] is True

    def test_rejected_pipeline_reports_failing_stage(self):
        result = get_pipeline_eligibility("git log | rm -rf /")
        assert result["is_eligible"] is False
        # The reason should identify the failing stage
        assert "stage" in result["reason"].lower() or "not-whitelisted" in result["reason"].lower()

    def test_empty_command_diagnostic(self):
        result = get_pipeline_eligibility("")
        assert result["is_eligible"] is False
        assert result["reason"] == "empty"
