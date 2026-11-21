"""PreToolUse hook: per-role path and command policy (ADR-005). Reads the hook event on stdin and
prints a deny decision when the call is outside the role's policy. Every decision is logged."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
CONFIG = json.loads((HARNESS / "config.json").read_text())

ROLE = os.environ.get("AUTOSE_ROLE", "")
WS = Path(os.environ.get("AUTOSE_WS", os.getcwd())).resolve()
STAGE_DIR = Path(os.environ.get("AUTOSE_STAGE_DIR", "/nonexistent")).resolve()
READ_DIRS = [Path(p).resolve() for p in os.environ.get("AUTOSE_READ_DIRS", "").split(os.pathsep) if p]
DENY_PATHS = [p for p in os.environ.get("AUTOSE_DENY_PATHS", "").split(os.pathsep) if p]
LOG = os.environ.get("AUTOSE_GUARD_LOG")

WRITE_SCOPE = {
    "analyst": {"stage_files": ["spec.md"]},
    "architect": {"stage_files": ["design.md"]},
    "developer": {"globs": CONFIG["developer_write_globs"]},
    "tester": {"globs": CONFIG["tester_write_globs"]},
    "reviewer": {},
    "judge": {},
}

BASH_DENY = [
    (r"\bgit\s+(push|commit|reset|checkout|switch|rebase|stash|worktree|remote|config|fetch|pull|clone|"
     r"am|apply|cherry-pick|merge|tag|branch\s+-[dDmM]|update-ref|fsck|cat-file|reflog)\b",
     "git state changes and object inspection are done by the orchestrator"),
    (r"\b(curl|wget|nc|ncat|netcat|ssh|scp|rsync|telnet|ftp)\b", "no network tools"),
    (r"\b(pip|pip3|uv|poetry|conda|npm|npx|apt|apt-get)\s+(install|add|i)\b", "no package installs"),
    (r"\bgh\s", "no GitHub CLI for agents"),
    (r"(^|[\s;&|])(env|printenv|set)(\s*$|\s*[|;&])", "environment dumps are not needed"),
    (r"/proc/[^\s]*/environ", "no reading process environments"),
    (r"\bsudo\b", "no sudo"),
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\.\.)", "no recursive deletes outside the workspace"),
    (r"(^|[\s;&|])\.github/(workflows|autose)", "the workflow and harness are off limits"),
    (r"socket\.|urllib\.request|http\.client|requests\.", "no network from scripts"),
]


def decide(event: dict) -> tuple[bool, str]:
    tool = event.get("tool_name", "")
    inp = event.get("tool_input") or {}
    if tool in ("WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit", "Skill"):
        return False, f"{tool} is not available in this pipeline"
    if tool in ("Read", "Grep", "Glob"):
        target = inp.get("file_path") or inp.get("path") or str(WS)
        p = _resolve(target)
        if any(str(p).startswith(d) for d in DENY_PATHS):
            return False, "that path belongs to another stage"
        if _inside(p, WS) or any(_inside(p, d) for d in READ_DIRS) or str(p).startswith(("/usr", "/opt", "/tmp")) \
                or "site-packages" in str(p):
            if _inside(p, WS / ".github"):
                return True, "read of harness allowed (no secrets there)"
            return True, "read inside allowed roots"
        return False, f"read outside the workspace is not allowed for {ROLE}: {target}"
    if tool in ("Edit", "Write", "MultiEdit"):
        p = _resolve(inp.get("file_path", ""))
        scope = WRITE_SCOPE.get(ROLE, {})
        if any(p == (STAGE_DIR / f).resolve() for f in scope.get("stage_files", [])):
            return True, "stage artifact"
        if _inside(p, WS):
            rel = str(p.relative_to(WS))
            if any(fnmatch.fnmatch(rel, g) for g in scope.get("globs", [])):
                return True, f"write to {rel} allowed"
            return False, f"{ROLE} may not write {rel}; allowed: {scope.get('globs') or scope.get('stage_files') or 'nothing'}"
        return False, f"{ROLE} may not write outside the workspace: {p}"
    if tool == "Bash":
        if ROLE not in ("architect", "developer", "tester", "reviewer"):
            return False, "Bash is not available to this role"
        cmd = inp.get("command", "")
        for pat, why in BASH_DENY:
            if re.search(pat, cmd):
                return False, f"blocked: {why}"
        if any(d and d in cmd for d in DENY_PATHS):
            return False, "that path belongs to another stage"
        return True, "bash allowed"
    if tool in ("StructuredOutput", "TodoWrite", "ToolSearch", "LS"):
        return True, "harmless"
    return False, f"tool {tool} is not in the {ROLE} policy"


def _resolve(target: str) -> Path:
    p = Path(target)
    if not p.is_absolute():
        p = WS / p
    try:
        return p.resolve()
    except Exception:
        return p


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    allowed, why = decide(event)
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps({"role": ROLE, "tool": event.get("tool_name"), "allowed": allowed, "why": why,
                                "input": {k: str(v)[:300] for k, v in (event.get("tool_input") or {}).items()}})
                    + "\n")
    if not allowed:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": f"autose guard: {why}"}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
