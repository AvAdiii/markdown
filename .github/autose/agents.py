"""Runs one agent = one fresh headless Claude Code session with a role-specific prompt, tool list,
model and guard hook (ADR-001, ADR-005). Agents never receive a GitHub credential."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from common import CONFIG, HARNESS, log

ROLE_TOOLS = {
    "analyst": ["Read", "Grep", "Glob", "Write"],
    "architect": ["Read", "Grep", "Glob", "Write", "Bash"],
    "developer": ["Read", "Grep", "Glob", "Edit", "Write", "Bash"],
    "tester": ["Read", "Grep", "Glob", "Edit", "Write", "Bash"],
    "reviewer": ["Read", "Grep", "Glob", "Bash"],
    "judge": [],
}
ALWAYS_DENIED = ["WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit", "Skill"]
ENV_PASSTHROUGH = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV", "pythonLocation",
                   "PYTHONPATH", "USER", "SHELL", "TERM",
                   # model credential: needed by the CLI itself, scrubbed from Bash subprocesses below
                   "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]
PROVIDER_ERRORS = ("rate limit", "rate_limit", "usage limit", "overloaded", "529", "429",
                   "authentication", "invalid api key", "401", "credit balance", "quota")


def _settings_file(work: Path) -> Path:
    settings = {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f"python3 {HARNESS / 'guard.py'}", "timeout": 30}]}]},
        "permissions": {"deny": ALWAYS_DENIED},
    }
    path = work / "agent-settings.json"
    path.write_text(json.dumps(settings))
    return path


def agent_env(role: str, ws: Path, stage_dir: Path, guard_log: Path, deny_paths: list[str],
              read_dirs: list[Path]) -> dict:
    env = {k: os.environ[k] for k in ENV_PASSTHROUGH if k in os.environ}
    env.update({
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",       # keep the model token out of Bash subprocesses
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "AUTOSE_ROLE": role,
        "AUTOSE_WS": str(ws),
        "AUTOSE_STAGE_DIR": str(stage_dir),
        "AUTOSE_GUARD_LOG": str(guard_log),
        "AUTOSE_DENY_PATHS": os.pathsep.join(deny_paths),
        "AUTOSE_READ_DIRS": os.pathsep.join(str(p) for p in read_dirs),
    })
    return env


SANDBOX_FAILURE = "Sandbox is required but failed to initialize"


def _classify_error(text: str) -> str:
    low = text.lower()
    return "provider" if any(k in low for k in PROVIDER_ERRORS) else "agent"


def run_agent(role: str, prompt: str, ws: Path, stage_dir: Path, transcript: Path, *,
              json_schema: dict | None = None, read_dirs: list[Path] | None = None,
              deny_paths: list[str] | None = None, model: str | None = None,
              ledger: Path | None = None, label: str = "") -> dict:
    """Run one agent session. Returns a dict with ok, text, structured, error_kind, usage."""
    model = model or CONFIG["models"][role]
    max_turns = CONFIG["max_turns"][role]
    tools = ROLE_TOOLS[role]
    stage_dir.mkdir(parents=True, exist_ok=True)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    guard_log = transcript.parent / "guard.log"
    system = (HARNESS / "prompts" / f"{role}.md").read_text()
    if role == "analyst":  # A1 skill + pipeline profile (the profile overrides the skill)
        system = (HARNESS / "skills" / "requirements-specifier" / "SKILL.md").read_text() + "\n\n" + system
    read_dirs = read_dirs or []
    cmd = ["claude", "-p", "--model", model,
           "--tools", ",".join(tools) if tools else "",
           "--disallowedTools", ",".join(ALWAYS_DENIED),
           "--append-system-prompt", system,
           "--settings", str(_settings_file(stage_dir)),
           "--setting-sources", "",
           "--strict-mcp-config", "--no-session-persistence",
           "--max-turns", str(max_turns),
           "--output-format", "stream-json", "--verbose"]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    for d in [stage_dir, *read_dirs]:
        cmd += ["--add-dir", str(d)]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    env = agent_env(role, ws, stage_dir, guard_log, deny_paths or [], [stage_dir, *read_dirs])

    attempts = CONFIG["budgets"]["provider_retries"] + 1
    result: dict = {}
    for attempt in range(attempts):
        start = time.time()
        final = None
        timed_out = False
        sandbox_broken = False
        with open(transcript, "a") as tf:
            tf.write(json.dumps({"autose": "session-start", "role": role, "model": model, "label": label,
                                 "attempt": attempt, "prompt": prompt}) + "\n")
            proc = subprocess.Popen(cmd, cwd=ws, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            try:
                out, err = proc.communicate(prompt, timeout=CONFIG["agent_timeout_s"])
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                timed_out = True
            for line in out.splitlines():
                tf.write(line + "\n")
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "result":
                    final = ev
                elif SANDBOX_FAILURE in line:  # Bash ran in no sandbox at all: an infrastructure fault
                    sandbox_broken = True
            if err.strip():
                tf.write(json.dumps({"autose": "stderr", "text": err[-4000:]}) + "\n")
        dur = round(time.time() - start, 1)
        usage = (final or {}).get("usage") or {}
        result = {
            "role": role, "model": model, "label": label, "duration_s": dur,
            "turns": (final or {}).get("num_turns"),
            "cost_usd": (final or {}).get("total_cost_usd"),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
            "text": (final or {}).get("result") or "",
            "structured": (final or {}).get("structured_output"),
            "subtype": (final or {}).get("subtype"),
        }
        if timed_out:
            result.update(ok=False, error_kind="timeout")
        elif sandbox_broken:
            result.update(ok=False, error_kind="sandbox",
                          error="the Bash sandbox (bubblewrap + socat) failed to start, so no command could run")
        elif final is None or final.get("is_error") or proc.returncode != 0:
            blob = json.dumps(final or {}) + err[-2000:]
            kind = "max_turns" if (final or {}).get("subtype") == "error_max_turns" else _classify_error(blob)
            result.update(ok=False, error_kind=kind, error=blob[-1500:])
        else:
            result.update(ok=True, error_kind=None)
        if ledger:
            with open(ledger, "a") as lf:
                lf.write(json.dumps({k: v for k, v in result.items() if k not in ("text", "structured")}) + "\n")
        log(f"agent {role} ({label}) ok={result['ok']} kind={result.get('error_kind')} turns={result['turns']} "
            f"{dur}s out_tokens={result['output_tokens']}")
        if result.get("error_kind") == "provider" and attempt + 1 < attempts:
            log("provider error, waiting 60 s before the single retry")
            time.sleep(60)
            continue
        break
    return result
