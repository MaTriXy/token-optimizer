"""Shared credential detection and redaction for Token Optimizer.

Provides compiled regex patterns for common API keys, tokens, and secrets,
plus scan/redact functions usable by bash compression, read cache, and
tool archive writers.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# (label, compiled_regex) pairs. Label is used in redaction placeholders.
CREDENTIAL_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("AWS access key",          re.compile(r"AKIA[0-9A-Z]{16}")),
    ("OpenAI/Anthropic key",    re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("Anthropic key",           re.compile(r"sk-ant-[a-zA-Z0-9\-]{20,}")),
    ("GitHub PAT classic",      re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("GitHub OAuth token",      re.compile(r"gho_[a-zA-Z0-9]{36}")),
    ("GitHub server token",     re.compile(r"ghs_[a-zA-Z0-9]{36}")),
    ("GitHub refresh token",    re.compile(r"ghr_[a-zA-Z0-9]{36}")),
    ("GitHub fine-grained PAT", re.compile(r"github_pat_[a-zA-Z0-9_]{80,}")),
    ("npm token",               re.compile(r"npm_[a-zA-Z0-9]{36}")),
    ("Slack bot token",         re.compile(r"xoxb-[0-9]+-[a-zA-Z0-9]+")),
    ("Slack user token",        re.compile(r"xoxp-[0-9]+-[a-zA-Z0-9]+")),
    ("Slack app token",         re.compile(r"xoxa-[0-9]+-[a-zA-Z0-9]+")),
    ("Stripe live key",         re.compile(r"sk_live_[a-zA-Z0-9]{24,}")),
    ("Stripe restricted key",   re.compile(r"rk_live_[a-zA-Z0-9]{24,}")),
    ("HuggingFace token",       re.compile(r"hf_[a-zA-Z0-9]{34}")),
    ("Bearer token",            re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.I)),
    ("Google API key",          re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("Google OAuth token",      re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}")),
    ("JWT",                     re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("PEM private key",         re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Database URI",            re.compile(r"(?:postgres|postgresql|mysql|mongodb|mongodb\+srv|redis)://[^:\s/]+:[^@\s]+@", re.I)),
    ("HTTP basic auth URL",     re.compile(r"https?://[^:\s/@]+:[^@\s]+@", re.I)),
    # Credentials passed as URL query/matrix parameters OR OAuth-implicit-flow
    # fragment params (e.g. ?token=..., ?api_key=..., ;password=..., #access_token=...).
    # The named `keep` group captures the "?name="/"#name=" prefix so redaction
    # preserves the parameter name and blanks only the value (see redact_credentials).
    # The value class stops at the next delimiter (& # ; whitespace quote < >) but
    # otherwise matches EVERYTHING — including brackets — so a secret that itself
    # contains a `[` (common in passwords) is redacted whole, not leaked past the
    # bracket. To still avoid re-wrapping an already-inserted "[CREDENTIAL REDACTED:
    # ...]" placeholder (when the value is itself another credential shape an earlier
    # pattern redacted, e.g. ?token=<Bearer ...>), a negative lookahead skips a value
    # that begins with the placeholder rather than excluding brackets from real values.
    ("URL auth param",          re.compile(
        r"(?P<keep>[?&#;](?:authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret"
        r"|session[_-]?token|id[_-]?token|api[_-]?key|sessionid|session|password|passwd|signature"
        r"|secret|bearer|token|auth|sig|pwd|key|jwt)=)(?!\[CREDENTIAL REDACTED:)[^&#;\s\"'<>]+",
        re.I,
    )),
]

# Bare compiled patterns list for backward compat with bash_compress.py
PATTERNS_ONLY: List["re.Pattern[str]"] = [pat for _, pat in CREDENTIAL_PATTERNS]

# ---------------------------------------------------------------------------
# H-8: fast pre-check for credential redaction.
#
# The old redact_credentials ran 23 sequential re.sub() calls on the full
# text unconditionally: 97ms for 10K lines, 675ms for 50K lines, even when
# the text contained NO credentials (the common case for command output).
# Python's re engine uses backtracking, not a DFA, so combining all
# patterns into a single alternation is actually SLOWER (154ms for 10K
# clean lines) due to the complex NFA state per character.
#
# The fix: a fast prefix scan using simple string containment checks
# before any regex runs. If none of the credential prefixes appear in the
# text, skip all 23 re.sub() calls entirely. This makes clean text O(n)
# with a tiny constant (a single str.find pass per prefix), while text
# with credentials still gets the full sequential redaction (correctness
# preserved, no regex complexity change).
#
# The prefix list is derived from the literal prefixes of each pattern:
# "AKIA", "sk-", "ghp_", "gho_", "ghs_", "ghr_", "github_pat_", "npm_",
# "xoxb-", "xoxp-", "xoxa-", "sk_live_", "rk_live_", "hf_", "Bearer",
# "AIza", "ya29.", "eyJ", "-----BEGIN", and the URL scheme prefixes for
# database/basic-auth URIs. The URL auth param pattern has no single
# literal prefix (it matches parameter names), so we check for "=" as a
# coarse pre-filter — but only if other prefixes didn't already match.
# ---------------------------------------------------------------------------
_CREDENTIAL_PREFIXES: Tuple[str, ...] = (
    "AKIA", "sk-", "ghp_", "gho_", "ghs_", "ghr_", "github_pat_",
    "npm_", "xoxb-", "xoxp-", "xoxa-", "sk_live_", "rk_live_", "hf_",
    "Bearer", "bearer", "AIza", "ya29.", "eyJ",
    "-----BEGIN",  # PEM private key
    "postgres://", "postgresql://", "mysql://", "mongodb://",
    "mongodb+srv://", "redis://",  # database URI
    "http://", "https://",  # basic auth URL (coarse, but covers the pattern)
)
# URL auth param parameter names (lowercase, checked case-insensitively).
_URL_AUTH_PARAM_NAMES: Tuple[str, ...] = (
    "authorization=", "access_token=", "access-token=", "refresh_token=",
    "refresh-token=", "client_secret=", "client-secret=", "session_token=",
    "session-token=", "id_token=", "id-token=", "api_key=", "api-key=",
    "sessionid=", "session=", "password=", "passwd=", "signature=",
    "secret=", "bearer=", "token=", "auth=", "sig=", "pwd=", "key=",
    "jwt=",
)


def _text_may_contain_credentials(text: str) -> bool:
    """Fast prefix scan: return True if any credential prefix appears in text.

    This is a coarse pre-filter using str.find (C-level, no regex engine).
    False negatives would be a security bug, so every prefix is checked.
    False positives are fine — the full regex suite runs and finds nothing.
    """
    for prefix in _CREDENTIAL_PREFIXES:
        if prefix in text:
            return True
    # URL auth param names are case-insensitive in the pattern. Use a
    # lowercase copy for the check.
    lower = text.lower()
    for name in _URL_AUTH_PARAM_NAMES:
        if name in lower:
            return True
    return False


def scan_for_credentials(text: str) -> List[Tuple[str, str, int]]:
    """Scan text for credentials. Returns [(label, matched_text, line_number), ...]."""
    results = []
    for line_num, line in enumerate(text.splitlines()):
        for label, pat in CREDENTIAL_PATTERNS:
            m = pat.search(line)
            if m:
                results.append((label, m.group(), line_num))
    return results


def redact_credentials(text: str) -> str:
    """Replace credential matches with [CREDENTIAL REDACTED: <type>] placeholders.

    A pattern may define a named `keep` group for a non-secret prefix that should
    survive redaction (e.g. the "?token=" part of a URL auth parameter); only the
    value after it is replaced. Patterns without a `keep` group redact the whole
    match, unchanged.

    H-8: uses a fast prefix scan to skip all 23 re.sub() calls when the text
    contains no credential prefixes (the common case for clean command output).
    This makes clean text O(n) with a tiny constant instead of O(n × 23) regex
    passes. Text with credentials still gets the full sequential redaction,
    preserving correctness and the existing two-phase ordering (standalone
    credentials before URL auth params, so the negative lookahead works).
    """
    # H-8: fast path — skip all regex work if no credential prefix is present.
    if not _text_may_contain_credentials(text):
        return text
    for label, pat in CREDENTIAL_PATTERNS:
        if "keep" in pat.groupindex:
            text = pat.sub(rf"\g<keep>[CREDENTIAL REDACTED: {label}]", text)
        else:
            text = pat.sub(f"[CREDENTIAL REDACTED: {label}]", text)
    return text
