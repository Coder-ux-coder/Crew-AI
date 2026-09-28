"""Crew's own automatic checks on every change, on top of the checks the team sets for itself.

scan() reads the lines a change adds (its git diff) and finds:

  leaked secrets  the owner's real keys (from ~/.crew/secrets.env) and well-known key formats. They block the
                  work: a secret that reaches the shared history is a security incident.
  risky code      patterns that often mean an injection hole, security switched off, unsafe handling of
                  untrusted data or errors swallowed silently. They go to the reviewer, who judges each one in
                  context (a pattern can be perfectly safe where it is).

It needs nothing installed, works for any language and takes a fraction of a second. It is a safety net under
the team's own tests and security checks and the reviewer's judgement, not a replacement for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops

PY = (".py",)
JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")
WEB = JS + (".html", ".htm")
TEMPLATES = (".html", ".htm", ".jinja", ".j2", ".djhtml")

SECRET_FORMATS = [
    ("a private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}|\bgithub_pat_[A-Za-z0-9_]{50,}")),
    ("an Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{24,}")),
    ("an OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{40,}")),
    ("a Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a Stripe secret key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{20,}")),
]
# Values that are plainly made up are not secrets.
PLACEHOLDER = re.compile(r"(?i)example|dummy|fake|placeholder|redacted|your[_-]?(?:key|token)|x{6,}|\*{4,}|<[^>]+>|\.\.\.")

RISKY = [
    ("runs text as code (eval/exec)", re.compile(r"(?<![\w.])(?:eval|exec)\s*\("), PY + JS),
    ("builds a function from text", re.compile(r"\bnew\s+Function\s*\("), JS),
    ("runs a shell command with shell=True", re.compile(r"\bshell\s*=\s*True"), PY),
    ("runs a shell command built from a string", re.compile(r"\bos\.(?:system|popen)\s*\("), PY),
    ("runs a shell command built from a string", re.compile(r"\b(?:child_process\.)?exec(?:Sync)?\s*\(\s*(?:`|[\"'][^\"']*[\"']\s*\+)"), JS),
    ("unpickles data (unsafe for anything untrusted)", re.compile(r"\bpickle\.loads?\s*\("), PY),
    ("yaml.load without a safe loader", re.compile(r"\byaml\.load\s*\((?![^)]*SafeLoader)"), PY),
    ("switches off certificate checks", re.compile(
        r"\bverify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED|\bCERT_NONE\b"), None),
    ("SQL built from a string (injection risk)", re.compile(
        r"""(?i)\b(?:execute|executemany|executescript|query|raw)\s*\(\s*(?:f["']|["'][^"']*\b(?:select|insert|update|delete)\b[^"']*["']\s*(?:%|\+|\.format\b))"""),
     PY + JS),
    ("SQL built from a string (injection risk)", re.compile(
        r"""(?i)["'`]\s*(?:select\b[^"'`]*\bfrom|insert\s+into|update\s+\w+\s+set|delete\s+from)\b[^"'`]*["'`]\s*\+\s*[\w(]"""), None),
    ("writes raw HTML (cross-site scripting risk)", re.compile(
        r"\.(?:inner|outer)HTML\s*\+?=|dangerouslySetInnerHTML|document\.write\s*\(|\bv-html\s*="), WEB),
    ("marks text as safe HTML (cross-site scripting risk)", re.compile(r"\bmark_safe\s*\(|\|\s*safe\b|\bMarkup\s*\("),
     PY + TEMPLATES),
    ("debug mode switched on", re.compile(r"\bDEBUG\s*=\s*True\b|\.run\([^)]*\bdebug\s*=\s*True"), PY),
    ("weak hash (MD5/SHA-1) — not for passwords or signatures", re.compile(
        r"\bhashlib\.(?:md5|sha1)\s*\(|createHash\(\s*['\"](?:md5|sha1)['\"]"), PY + JS),
    ("a password or key written into the code", re.compile(
        r"""(?i)\b(?:password|passwd|secret|api_?key|access_?token|auth_?token|private_?key)\b["']?\s*[:=]\s*["'][^"'\s]{8,}["']"""),
     None),
    ("open to every website (CORS *)", re.compile(
        r"""Access-Control-Allow-Origin["']?\s*[:,=]\s*["']\*|\borigins?\s*=\s*\[?\s*["']\*["']|\bcors\(\s*\)"""), None),
    ("readable and writable by everyone (777)", re.compile(r"\bchmod\s+(?:-R\s+)?777\b|\b0o777\b"), None),
    ("temporary file created unsafely", re.compile(r"\btempfile\.mktemp\s*\("), PY),
    ("catches every error, including stop signals", re.compile(r"^\s*except\s*:"), PY),
    ("swallows an error silently", re.compile(r"^\s*except(?:\s+(?:Exception|BaseException))?\s*(?:as\s+\w+\s*)?:\s*pass\s*$"), PY),
    ("swallows an error silently", re.compile(r"\bcatch\s*(?:\(\s*\w*\s*\))?\s*\{\s*\}"), JS),
]
# Patterns that say little in tests (fixtures hold fake passwords; tests may start debug servers).
NOT_IN_TESTS = {"a password or key written into the code", "debug mode switched on", "runs text as code (eval/exec)",
                "weak hash (MD5/SHA-1) — not for passwords or signatures"}
SKIP_FILES = re.compile(r"(?:^|/)(?:node_modules|vendor|dist|build|\.venv|venv|__pycache__)/|\.min\.(?:js|css)$|"
                        r"(?:^|/)(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|go\.sum)$")
TEST_FILES = re.compile(r"(?:^|/)(?:tests?|spec|__tests__)/|(?:^|/)test_[^/]*$|_test\.\w+$|\.(?:test|spec)\.\w+$")
MAX_LINES = 50_000
MAX_FINDINGS = 40


@dataclass
class Finding:
    file: str
    line: int
    what: str
    code: str = ""
    blocking: bool = False

    def where(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class Scan:
    findings: list[Finding] = field(default_factory=list)
    lines: int = 0

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def risky(self) -> list[Finding]:
        return [f for f in self.findings if not f.blocking]


def added_lines(diff_text: str):
    """(file, line number in the new file, text) for every line a unified diff adds."""
    path, n = None, 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            continue
        if raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            n = int(m.group(1)) if m else 0
            continue
        if path is None or raw.startswith(("---", "\\", "diff ", "index ")):
            continue
        if raw.startswith("+"):
            yield path, n, raw[1:]
            n += 1
        elif raw.startswith(" "):
            n += 1


def _mask(text: str, secrets: list[str]) -> str:
    for value in secrets:
        text = text.replace(value, "•••")
    for _, rx in SECRET_FORMATS:
        text = rx.sub("•••", text)
    return text


def scan_text(diff_text: str, secrets: list[str] | None = None) -> Scan:
    """Scan the added lines of a unified diff. `secrets` are the owner's real key values."""
    secrets = sorted({s for s in (secrets or []) if len(s) >= 8}, key=len, reverse=True)
    out = Scan()
    for path, line, text in added_lines(diff_text):
        if out.lines >= MAX_LINES or len(out.findings) >= MAX_FINDINGS:
            break
        out.lines += 1
        if SKIP_FILES.search(path):
            continue
        leaked = next((v for v in secrets if v in text), None)
        if leaked:
            out.findings.append(Finding(path, line, "one of your real keys (from Crew's secrets)", blocking=True))
            continue
        fmt = next((label for label, rx in SECRET_FORMATS
                    if (m := rx.search(text)) and not PLACEHOLDER.search(m.group(0))), None)
        if fmt:
            out.findings.append(Finding(path, line, f"what looks like {fmt}", blocking=True))
            continue
        ext = Path(path).suffix.lower()
        in_tests = bool(TEST_FILES.search(path))
        for label, rx, exts in RISKY:
            if exts is not None and ext not in exts:
                continue
            if in_tests and label in NOT_IN_TESTS:
                continue
            if rx.search(text):
                if label == "a password or key written into the code" and PLACEHOLDER.search(text):
                    continue
                out.findings.append(Finding(path, line, label, _mask(text.strip(), secrets)[:160]))
                break
    return out


def scan(cwd: Path, base: str, secrets: list[str] | None = None) -> Scan:
    """Scan what HEAD adds on top of `base` in a git worktree."""
    proc = gitops.git(cwd, "diff", "--unified=0", "--no-color", "--no-ext-diff", f"{base}...HEAD", check=False,
                      timeout=120)
    return scan_text(proc.stdout if proc.returncode == 0 else "", secrets)


def blocking_text(result: Scan, base: str) -> str:
    """Why the work goes back to its author (no secret is ever repeated)."""
    where = "\n".join(f"- {f.where()}: {f.what}" for f in result.blocking[:10])
    return ("Crew's automatic scan found what looks like a real secret in your change. A secret in the code or in "
            "the git history is a security incident, so this cannot be merged:\n" + where + "\n"
            "Remove it and read it from the environment instead (the owner's keys are already in your environment). "
            "Because it is in your branch's commits, squash them so that the secret never reaches the shared history "
            f"(git reset --soft $(git merge-base HEAD {base}) && git commit -m \"<what the task does>\") — the one time "
            "you may rewrite your own task branch. If it is a made-up example, write it so that it cannot be mistaken "
            "for a real key (for example sk-EXAMPLE). Then resubmit.")


def final_blocking_text(result: Scan) -> str:
    """The finished work contains what looks like a secret: it is already in the shared history."""
    where = "\n".join(f"- {f.where()}: {f.what}" for f in result.blocking[:10])
    return ("Crew's automatic scan found what looks like a real secret in the finished work:\n" + where + "\n"
            "Remove it from the code and read it from the environment instead, then run the checks. Because it is "
            "already in the project's history, say plainly in the report that the owner should replace (rotate) "
            "that key.")


def review_notes(result: Scan, limit: int = 15) -> str:
    """For the reviewer: lines to look at (empty when there is nothing)."""
    if not result.risky:
        return ""
    rows = "\n".join(f"- {f.where()}: {f.what}" + (f" — `{f.code}`" if f.code else "") for f in result.risky[:limit])
    more = f"\n(and {len(result.risky) - limit} more)" if len(result.risky) > limit else ""
    return ("Crew's automatic scan flagged these added lines. Check each one: it is a real defect if untrusted "
            "input can reach it, if it switches a protection off, or if it hides errors; it is fine if it is safe "
            "in context (say so briefly).\n" + rows + more)


def summary(result: Scan) -> str:
    """One line for the final review and the report."""
    if not result.findings:
        return f"Crew's automatic scan of the whole change ({result.lines} added lines): nothing flagged."
    return (f"Crew's automatic scan of the whole change ({result.lines} added lines) flagged "
            f"{len(result.blocking)} possible secret(s) and {len(result.risky)} risky line(s):\n"
            + "\n".join(f"- {f.where()}: {f.what}" for f in result.findings[:12]))
