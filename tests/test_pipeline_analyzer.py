#!/usr/bin/env python3
"""UNIT B (hardened): Pipeline analyzer tests — corpus + bypass rejection.

Validates that pipeline_analyzer:
  1. Correctly classifies read-only pipelines
  2. Rejects every injection bypass string from the adversarial review
  3. Rejects side-effecting commands
  4. Measures eligibility rate on the safe read-only corpus.

FINAL HARDENING (4 remaining bypasses):
  - Redirects to real files (>, >>, 1>, 2>, &>) are REJECTED
  - curl/wget dropped from whitelist
  - aws/gcloud/az dropped from whitelist entirely
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
    _token_has_glued_operator,
    _raw_command_has_dangerous_constructs,
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

    def test_non_redirects(self):
        assert not _is_redirect_token("file.txt")
        assert not _is_redirect_token("--option")
        assert not _is_redirect_token("git")

    def test_redirect_stripping_basic(self):
        """2>&1 is fd-to-fd, no file target, safe."""
        tokens = ["command", "2>&1"]
        clean, has_file = _strip_redirections(tokens)
        assert clean == ["command"]
        assert has_file is False

    def test_redirect_stripping_dev_null(self):
        """>/dev/null is a safe sink, not a file write."""
        tokens = ["command", ">", "/dev/null"]
        clean, has_file = _strip_redirections(tokens)
        assert clean == ["command"]
        assert has_file is False

    def test_redirect_to_real_file_rejected(self):
        """>/etc/hosts is a file WRITE."""
        tokens = ["printf", "x", ">", "/etc/hosts"]
        clean, has_file = _strip_redirections(tokens)
        assert clean == ["printf", "x"]
        assert has_file is True, "Redirect to /etc/hosts must be flagged as file write"

    def test_redirect_append_to_real_file_rejected(self):
        """>>/var/log/x is a file APPEND = write."""
        tokens = ["echo", "log", ">>", "/var/log/app.log"]
        clean, has_file = _strip_redirections(tokens)
        assert has_file is True, ">> to real file must be flagged as file write"

    def test_redirect_mixed(self):
        tokens = ["git", "log", "2>&1", "|", "head"]
        clean, has_file = _strip_redirections(tokens)
        assert clean == ["git", "log", "|", "head"]
        assert has_file is False


class TestStageSplitting:
    def test_simple_pipe(self):
        tokens = ["git", "log", "|", "head"]
        stages = _split_stages(tokens)
        assert len(stages) == 2

    def test_double_ampersand(self):
        tokens = ["git", "log", "&&", "git", "status"]
        stages = _split_stages(tokens)
        assert len(stages) == 2

    def test_multi_pipe(self):
        tokens = ["grep", "-r", "TODO", "src/", "|", "sort", "|", "uniq", "-c"]
        stages = _split_stages(tokens)
        assert len(stages) == 3


# ============================================================================
# Stage-level read-only classification
# ============================================================================

class TestStageReadOnly:
    def test_git_log_is_read_only(self):
        ok, reason = _is_stage_read_only(["git", "log", "--oneline"])
        assert ok, f"git log should be read-only, got: {reason}"

    def test_git_push_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["git", "push"])
        assert not ok

    def test_head_is_read_only(self):
        ok, reason = _is_stage_read_only(["head", "-20"])
        assert ok

    def test_sort_is_read_only(self):
        ok, reason = _is_stage_read_only(["sort", "-rn"])
        assert ok

    def test_sed_without_i_is_read_only(self):
        ok, reason = _is_stage_read_only(["sed", "s/foo/bar/g"])
        assert ok

    def test_sed_with_i_flag_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["sed", "-i", "s/foo/bar/g", "file.txt"])
        assert not ok

    def test_python_script_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["python", "script.py"])
        assert not ok

    def test_python_m_pytest_is_read_only(self):
        ok, reason = _is_stage_read_only(["python", "-m", "pytest"])
        assert ok

    def test_python_m_pip_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["python", "-m", "pip", "install", "x"])
        assert not ok

    def test_bash_is_never_read_only(self):
        ok, reason = _is_stage_read_only(["bash", "-c", "ls"])
        assert not ok

    def test_sudo_is_never_read_only(self):
        ok, reason = _is_stage_read_only(["sudo", "ls"])
        assert not ok

    def test_npm_test_is_read_only(self):
        ok, reason = _is_stage_read_only(["npm", "test"])
        assert ok

    def test_cat_is_read_only(self):
        ok, reason = _is_stage_read_only(["cat", "file.txt"])
        assert ok

    def test_echo_is_read_only(self):
        ok, reason = _is_stage_read_only(["echo", "hello"])
        assert ok

    def test_unknown_command_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["some-random-tool", "--flag"])
        assert not ok

    def test_wc_is_read_only(self):
        ok, reason = _is_stage_read_only(["wc", "-l"])
        assert ok

    def test_docker_compose_up_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["docker", "compose", "up"])
        assert not ok

    def test_docker_compose_ps_is_read_only(self):
        ok, reason = _is_stage_read_only(["docker", "compose", "ps"])
        assert ok

    def test_terraform_plan_is_read_only(self):
        ok, reason = _is_stage_read_only(["terraform", "plan"])
        assert ok

    def test_kubectl_get_is_read_only(self):
        ok, reason = _is_stage_read_only(["kubectl", "get", "pods"])
        assert ok

    def test_kubectl_delete_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["kubectl", "delete", "pod"])
        assert not ok

    # --- POST-AUDIT: hardened checks ---

    def test_git_branch_d_flag_rejected(self):
        ok, reason = _is_stage_read_only(["git", "branch", "-d", "old-branch"])
        assert not ok

    def test_git_branch_D_flag_rejected(self):
        ok, reason = _is_stage_read_only(["git", "branch", "-D", "old-branch"])
        assert not ok

    def test_git_gc_prune_not_in_allowlist(self):
        ok, reason = _is_stage_read_only(["git", "gc", "--prune=now"])
        assert not ok

    def test_git_update_ref_not_in_allowlist(self):
        ok, reason = _is_stage_read_only(["git", "update-ref", "-d", "ref"])
        assert not ok

    def test_find_delete_rejected(self):
        ok, reason = _is_stage_read_only(["find", ".", "-name", "*.pyc", "-delete"])
        assert not ok

    def test_find_exec_rejected(self):
        ok, reason = _is_stage_read_only(["find", ".", "-exec", "rm", "{}", ";"])
        assert not ok

    def test_find_ok_rejected(self):
        ok, reason = _is_stage_read_only(["find", ".", "-ok", "rm", "{}", ";"])
        assert not ok

    def test_find_without_destructive_flags_is_ok(self):
        ok, reason = _is_stage_read_only(["find", "src", "-name", "*.py"])
        assert ok, f"find without destructive flags should be read-only: {reason}"

    def test_sqlite3_replace_rejected(self):
        ok, reason = _is_stage_read_only(["sqlite3", "db", "REPLACE INTO t VALUES(1)"])
        assert not ok

    def test_sqlite3_vacuum_rejected(self):
        ok, reason = _is_stage_read_only(["sqlite3", "db", "VACUUM"])
        assert not ok

    def test_sqlite3_dot_import_rejected(self):
        ok, reason = _is_stage_read_only(["sqlite3", "db", ".import", "data.csv", "t"])
        assert not ok

    def test_sqlite3_pragma_rejected(self):
        ok, reason = _is_stage_read_only(["sqlite3", "db", "PRAGMA", "journal_mode=OFF"])
        assert not ok

    def test_sqlite3_select_is_ok(self):
        ok, reason = _is_stage_read_only(["sqlite3", "db", "SELECT * FROM t"])
        assert ok, f"sqlite3 SELECT should be read-only: {reason}"

    # --- POST-AUDIT: removed from whitelist ---

    def test_awk_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["awk", "{print $1}"])
        assert not ok

    def test_tee_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["tee", "file.txt"])
        assert not ok

    def test_xargs_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["xargs", "rm", "-rf"])
        assert not ok

    def test_env_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["env", "rm", "-rf", "x"])
        assert not ok

    def test_command_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["command", "rm", "-rf", "x"])
        assert not ok

    def test_npm_run_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["npm", "run", "deploy"])
        assert not ok

    def test_terraform_state_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["terraform", "state", "rm"])
        assert not ok

    def test_git_config_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["git", "config", "--global", "user.name", "evil"])
        assert not ok

    def test_docker_pull_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["docker", "pull", "evil-image"])
        assert not ok

    def test_mvn_deploy_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["mvn", "deploy"])
        assert not ok

    def test_deno_run_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["deno", "run", "-A", "evil.ts"])
        assert not ok

    def test_bun_run_is_not_read_only(self):
        ok, reason = _is_stage_read_only(["bun", "run", "evil.ts"])
        assert not ok

    # --- FINAL HARDENING: cloud CLIs dropped entirely ---

    def test_aws_not_in_whitelist(self):
        ok, reason = _is_stage_read_only(["aws", "s3", "ls"])
        assert not ok, "aws should NOT be in whitelist"

    def test_gcloud_not_in_whitelist(self):
        ok, reason = _is_stage_read_only(["gcloud", "compute", "instances", "list"])
        assert not ok, "gcloud should NOT be in whitelist"

    def test_az_not_in_whitelist(self):
        ok, reason = _is_stage_read_only(["az", "vm", "list"])
        assert not ok, "az should NOT be in whitelist"

    def test_curl_not_in_whitelist(self):
        ok, reason = _is_stage_read_only(["curl", "-s", "https://api/x"])
        assert not ok, "curl should NOT be in whitelist"

    def test_wget_not_in_whitelist(self):
        ok, reason = _is_stage_read_only(["wget", "-q", "https://api/x"])
        assert not ok, "wget should NOT be in whitelist"


# ============================================================================
# Pre-tokenization injection screen tests (B1 bypass strings)
# ============================================================================

INJECTION_BYPASSES = [
    # Newline injection
    ("ls -la\nrm -rf build", "newline-injection"),
    # Command substitution
    ("cat $(rm -rf x)", "cmd-sub-dollar"),
    ("cat `rm -rf x`", "cmd-sub-backtick"),
    # Process substitution
    ("cat <(rm -rf x)", "process-sub-input"),
    # Glued operators
    ("cat a|rm b", "glued-pipe"),
    ("cat a;rm b", "glued-semicolon"),
    # Bare ampersand (async)
    ("echo hello & rm -rf x", "bare-ampersand"),
    # find destructive
    ("find . -delete", "find-delete"),
    ("find . -exec rm {} \\;", "find-exec"),
    # Launcher bypasses
    ("env rm -rf x", "env-launcher"),
    ("command rm -rf x", "command-launcher"),
    # tee / xargs (no longer whitelisted)
    ("tee /etc/passwd", "tee-write"),
    ("xargs rm -rf", "xargs-destructive"),
    # git destructive
    ("git branch -D main", "git-branch-delete"),
    ("git gc --prune=now", "git-gc"),
    ("git config --global user.name evil", "git-config-write"),
    ("git update-ref -d refs/heads/x", "git-update-ref"),
    # terraform / npm
    ("terraform state rm resource", "terraform-state-rm"),
    ("npm run deploy", "npm-run-arbitrary"),
    # deno / bun / mvn
    ("deno run -A evil.ts", "deno-run"),
    ("bun run evil.ts", "bun-run"),
    ("mvn deploy", "mvn-deploy"),
    ("docker pull evil", "docker-pull"),
    # python -m pip
    ("python3 -m pip install evil", "python-m-pip"),
    # sqlite3 mutations
    ("sqlite3 db 'REPLACE INTO t VALUES(1)'", "sqlite3-replace"),
    ("sqlite3 db VACUUM", "sqlite3-vacuum"),
    ("sqlite3 db .import data.csv t", "sqlite3-dot-import"),
    # kubectl config
    ("kubectl config use-context prod", "kubectl-config"),
    # === FINAL 4 BYPASSES ===
    # 1. Redirect to real file
    ("printf x > /etc/hosts", "redirect-to-file-write"),
    # 2. curl (dropped from whitelist)
    ("curl -X DELETE https://api/x", "curl-mutating"),
    # 3. aws s3 rm (cloud CLI dropped)
    ("aws s3 rm s3://bucket --recursive", "aws-s3-rm"),
    # 4. gcloud compute instances delete (cloud CLI dropped)
    ("gcloud compute instances delete vm1 -q", "gcloud-compute-delete"),
]


class TestInjectionBypasses:
    """Every injection bypass must be rejected."""

    @pytest.mark.parametrize("command,description", INJECTION_BYPASSES)
    def test_bypass_is_rejected(self, command, description):
        is_ro, reason = is_read_only_pipeline(command)
        assert not is_ro, (
            f"BYPASS: '{description}' was accepted as read-only!\n"
            f"Command: {command}\n"
            f"Reason given: {reason}"
        )


# ============================================================================
# Glued-operator and dangerous-construct tests
# ============================================================================

class TestGluedOperatorDetection:
    def test_pipe_in_token_detected(self):
        assert _token_has_glued_operator("a|rm") is True
        assert _token_has_glued_operator("cat;rm") is True

    def test_pipe_as_standalone_not_flagged(self):
        assert _token_has_glued_operator("|") is False


class TestDangerousConstructs:
    def test_newline_detected(self):
        has_danger, reason = _raw_command_has_dangerous_constructs("ls\nrm -rf /")
        assert has_danger

    def test_command_sub_dollar_detected(self):
        has_danger, reason = _raw_command_has_dangerous_constructs("echo $(rm -rf /)")
        assert has_danger

    def test_command_sub_backtick_detected(self):
        has_danger, reason = _raw_command_has_dangerous_constructs("echo `rm -rf /`")
        assert has_danger

    def test_process_sub_detected(self):
        has_danger, reason = _raw_command_has_dangerous_constructs("cat <(ls)")
        assert has_danger

    def test_clean_command_not_flagged(self):
        has_danger, reason = _raw_command_has_dangerous_constructs("git log | head -20")
        assert not has_danger


# ============================================================================
# Pipeline-level classification: CORPUS TESTS
# ============================================================================

# Read-only pipelines: should ALL be eligible.
# FINAL: cloud CLIs and curl/wget removed.
READ_ONLY_PIPELINES = [
    ("git log --oneline | head -20", "git log piped to head"),
    ("cat package.json | grep version", "cat piped to grep"),
    ("grep -r FIXME src/ | sort | uniq -c | sort -rn | head -10", "multi-stage pipeline"),
    ("git log --oneline | tail -5", "git log piped to tail"),
    ("ls -la | wc -l", "ls piped to wc"),
    ("git diff HEAD~1 | grep '^+' | wc -l", "git diff + grep + wc"),
    ("du -sh * | sort -rh | head -10", "du + sort + head"),
    ("cat file.txt | tr '[:lower:]' '[:upper:]'", "cat + tr"),
    ("git branch -a | grep -v '^*' | sort", "git branch + grep + sort"),
    ("npm test 2>&1 | grep -E '(passing|failing)'", "npm test with stderr redirect"),
    ("git status && ls -la", "git status && ls"),
    ("echo 'build info' && git log -1", "echo && git log"),
    ("pwd && ls && git log -1", "pwd && ls && git log"),
    ("date && whoami && hostname", "system info chain"),
    ("git log --format='%h %s' | column -t", "git log + column"),
    ("rg 'TODO' src/ | sort", "ripgrep + sort"),
    ("grep -rn 'import' --include='*.ts' . | head -20", "grep + head"),
    ("git grep 'FIXME' | sort -u", "git grep + sort -u"),
    ("docker ps | grep api", "docker ps + grep"),
    ("docker images | head -10", "docker images + head"),
    ("kubectl get pods | grep Running | wc -l", "kubectl get + grep + wc"),
    ("kubectl describe pod my-pod | grep 'Image:'", "kubectl describe + grep"),
    ("find src -name '*.py' | wc -l", "find + wc"),
    ("find . -name '*.py' -type f | sort", "find + sort"),
    ("jq '.name' package.json | sort", "jq + sort"),
    ("yq '.dependencies' package.json", "yq read-only"),
]

# Side-effecting / dangerous commands: should ALL be rejected
SIDE_EFFECTING = [
    # Git write commands
    ("git push origin main", "git push is a write"),
    ("git commit -m 'fix'", "git commit is a write"),
    ("git add . && git commit -m 'update'", "git add + commit chain"),
    ("git fetch && git checkout main", "git fetch is a side effect"),
    ("git branch -d old-branch", "git branch -d is destructive"),
    ("git branch -D old-branch", "git branch -D is destructive"),
    ("git gc --prune=now", "git gc not in allow-list"),
    ("git config user.name evil", "git config not in allow-list"),
    ("git update-ref -d ref", "git update-ref not in allow-list"),

    # Mixed pipeline (read-only pipe, write in one stage)
    ("rm -rf build/ | wc -l", "rm is destructive"),
    ("mkdir -p dist && ls dist", "mkdir is a write"),
    ("npm install express | tail -5", "npm install has side effects"),
    ("pip install requests 2>&1 | grep Success", "pip install has side effects"),

    # Shell interpreters
    ("bash -c 'ls | grep foo'", "bash is a shell interpreter"),
    ("sh script.sh | head", "sh is a shell interpreter"),

    # Privilege escalation
    ("sudo ls /root", "sudo is privilege escalation"),

    # Node with -e
    ("node -e 'console.log(1)' | head", "node -e is arbitrary code"),

    # Python as interpreter
    ("python script.py 2>&1 | tail -30", "python script.py is arbitrary code"),

    # sed -i
    ("sed -i 's/old/new/g' file.txt", "sed -i is a write"),

    # docker compose side-effecting
    ("docker compose up -d && docker compose ps", "docker compose up has side effects"),

    # kubectl delete
    ("kubectl delete pod my-pod && kubectl get pods", "kubectl delete has side effects"),

    # Terraform apply
    ("terraform apply -auto-approve 2>&1 | tail", "terraform apply is a write"),

    # Mixed safe + unsafe
    ("docker compose build && docker compose ps", "docker compose build has side effects"),
    ("git log --oneline | head && git commit -m 'update'", "mixed: git log + git commit"),
    ("pytest tests/ && git add .", "pytest + git add (write)"),

    # Post-audit: commands removed from whitelist
    ("env rm -rf x", "env is never-read-only"),
    ("command rm -rf x", "command is never-read-only"),
    ("xargs rm -rf", "xargs is never-read-only"),
    ("tee /etc/passwd", "tee is not in whitelist"),
    ("awk 'BEGIN{print 1}'", "awk is not in whitelist"),
    ("npm run deploy", "npm run is not in compound whitelist"),
    ("terraform state rm x", "terraform state is not in compound whitelist"),
    ("kubectl config use-context prod", "kubectl config is not in compound whitelist"),
    ("docker pull evil", "docker pull is not in compound whitelist"),
    ("python3 -m pip install evil", "python -m pip is not safe module"),
    ("mvn deploy", "mvn deploy is not test"),
    ("deno run -A evil.ts", "deno run is not test"),
    ("bun run evil.ts", "bun run is not test"),

    # find destructive
    ("find . -name '*.pyc' -delete", "find -delete is destructive"),
    ("find . -exec rm {} \\;", "find -exec is destructive"),
    ("find . -ok rm {} \\;", "find -ok is destructive"),

    # sqlite3 write
    ("sqlite3 db 'REPLACE INTO t VALUES(1)'", "sqlite3 REPLACE"),
    ("sqlite3 db VACUUM", "sqlite3 VACUUM"),
    ("sqlite3 db .import data.csv t", "sqlite3 .import"),
    ("sqlite3 db PRAGMA journal_mode=OFF", "sqlite3 PRAGMA"),

    # === FINAL 4 BYPASSES (also tested as per-stage read-only) ===
    # 1. Redirect to real file
    ("printf x > /etc/hosts", "redirect-to-file-write"),
    # 2. curl (dropped from whitelist)
    ("curl -X DELETE https://api/x", "curl-mutating"),
    ("curl -s https://api/x", "curl-even-plain-GET-is-dropped"),
    # 3. aws (dropped from whitelist)
    ("aws s3 rm s3://bucket --recursive", "aws-s3-rm"),
    ("aws s3 ls", "aws-even-ls-is-dropped"),
    # 4. gcloud (dropped from whitelist)
    ("gcloud compute instances delete vm1 -q", "gcloud-compute-delete"),
    ("gcloud compute instances list", "gcloud-even-list-is-dropped"),
    # az (also dropped)
    ("az vm delete --name vm1", "az-vm-delete"),
    ("az vm list", "az-even-list-is-dropped"),
]


class TestPipelineEligibility:
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
        is_ro, reason = is_read_only_pipeline("echo 'unclosed quote")
        assert not is_ro


# ============================================================================
# CORPUS ELIGIBILITY MEASUREMENT
# ============================================================================

def test_eligibility_rate_on_corpus():
    """UNIT B eligibility rate on safe read-only pipelines. Target: >80%."""
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

    print(f"\n=== CORPUS ELIGIBILITY ===")
    print(f"Total read-only pipeline commands: {total}")
    print(f"Eligible for compression: {eligible}")
    print(f"Eligibility rate: {rate:.1f}%")
    if failures:
        print(f"Rejected ({len(failures)}):")
        for f in failures:
            print(f)

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


class TestDiagnosticFunction:
    def test_eligible_pipeline_has_detailed_stages(self):
        result = get_pipeline_eligibility("git log | head -20")
        assert result["is_eligible"] is True
        assert result["stage_count"] == 2

    def test_rejected_pipeline_reports_failing_stage(self):
        result = get_pipeline_eligibility("git log | rm -rf /")
        assert result["is_eligible"] is False

    def test_empty_command_diagnostic(self):
        result = get_pipeline_eligibility("")
        assert result["is_eligible"] is False
        assert result["reason"] == "empty"
