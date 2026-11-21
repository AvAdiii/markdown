"""Release job (stage 5): deterministic, no model. Applies the tested diff on a branch, pushes it with
the autose-dev App token, opens or updates the PR, and waits for the repository's CI (ADR-001, ADR-007).
Environment: AUTOSE_TRIGGER, AUTOSE_BUILD (build artifact dir), AUTOSE_ROUND, AUTOSE_WS,
AUTOSE_APP_TOKEN (autose-dev App token: PR create/edit; the checkout's push credential is the same token),
AUTOSE_GH_TOKEN / AUTOSE_READ_TOKEN (GITHUB_TOKEN: status comment, read check runs), AUTOSE_BOT(_EMAIL)."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from common import CONFIG, dump_json, log, repo, run_url, set_output, sh, update_status, write_summary


def gh(args: list[str]) -> str:
    """gh as the autose-dev App (so the PR author is the bot, not the workflow token)."""
    return sh(["gh", *args], env=dict(os.environ, GH_TOKEN=os.environ["AUTOSE_APP_TOKEN"])).stdout


def section(md: str, heading: str) -> str:
    m = re.search(rf"^## {heading}\s*$(.*?)(?=^## |\Z)", md, re.M | re.S)
    return m.group(1).strip() if m else ""


def requirement_lines(spec: str) -> list[str]:
    lines = []
    for m in re.finditer(r"^### (FR-\d{3}|NFR-[A-Z]+-\d{3}):?\s*(.*?)$(.*?)(?=^### |^## |\Z)", spec, re.M | re.S):
        body = m.group(3)
        status = re.search(r"- Status:\s*(.*)", body)
        stmt = re.search(r"- Statement:\s*(.*)", body)
        if status and status.group(1).upper().startswith("READY"):
            lines.append(f"- **{m.group(1)}** {stmt.group(1) if stmt else m.group(2)}")
    return lines


def pr_body(t: dict, build: Path, rnd: int, ci: str) -> str:
    spec = (build / "spec.md").read_text() if (build / "spec.md").exists() else ""
    design = (build / "design.md").read_text() if (build / "design.md").exists() else ""
    report = (build / "test_report.md").read_text() if (build / "test_report.md").exists() else ""
    state = json.loads((build / "state.json").read_text()) if (build / "state.json").exists() else {}
    rows = [f"| {s} | {v.get('status')} | {v.get('attempts')} |" for s, v in state.get("stages", {}).items()]
    table = re.search(r"\| Acceptance criterion.*?(?=\n\n)", report, re.S)
    return "\n".join([
        f"Closes #{t['issue']}.",
        "",
        "This pull request was produced by the autose workflow. A maintainer decides whether to merge it.",
        "",
        "## What the issue asks (READY requirements from `spec.md`)",
        *(requirement_lines(spec) or ["(see spec.md)"]),
        "",
        "## Approach (from `design.md`)",
        section(design, "Summary") or "(see design.md)",
        "",
        section(design, "Changes"),
        "",
        "## Tests (from `test_report.md`)",
        table.group(0) if table else "(see test_report.md)",
        "",
        "## Stage checks",
        "| Stage | Result | Attempts |", "|---|---|---|", *rows,
        f"| release | {ci} | - |",
        "",
        f"Round {rnd}. Run and all stage artifacts: {run_url()}",
    ])


def wait_for_ci(sha: str, read_env: dict) -> tuple[bool, str, list]:
    deadline = time.time() + CONFIG["ci_wait_s"]
    seen_any_at = None
    runs: list = []
    while time.time() < deadline:
        time.sleep(30)
        out = sh(["gh", "api", f"repos/{repo()}/commits/{sha}/check-runs?per_page=100"], env=read_env, check=False)
        try:
            runs = json.loads(out.stdout).get("check_runs", [])
        except Exception:
            continue
        if not runs:
            if time.time() > deadline - CONFIG["ci_wait_s"] + 180:  # nothing after 3 min
                return False, "no CI check started for the head commit within 3 minutes", runs
            continue
        seen_any_at = seen_any_at or time.time()
        pending = [r for r in runs if r["status"] != "completed"]
        if pending or time.time() - seen_any_at < 45:  # let late workflows register
            continue
        bad = [r for r in runs if r["conclusion"] not in ("success", "neutral", "skipped")]
        if bad:
            return False, "failing checks: " + ", ".join(f"{r['name']} ({r['conclusion']}) {r['html_url']}"
                                                          for r in bad), runs
        return True, f"{len(runs)} checks passed", runs
    return False, f"CI did not finish within {CONFIG['ci_wait_s']} s", runs


def main() -> int:
    t = json.loads(Path(os.environ["AUTOSE_TRIGGER"]).read_text())
    build = Path(os.environ["AUTOSE_BUILD"])
    rnd = int(os.environ.get("AUTOSE_ROUND", "0"))
    ws = Path(os.environ["AUTOSE_WS"])
    out = Path(os.environ.get("AUTOSE_OUT", "release-out"))
    out.mkdir(parents=True, exist_ok=True)
    issue = int(t["issue"])
    branch = f"{CONFIG['branch_prefix']}{issue}"
    state = json.loads((build / "state.json").read_text())
    base = state["base"]
    read_env = dict(os.environ, GH_TOKEN=os.environ["AUTOSE_READ_TOKEN"])

    # tree = base + tested diff
    sh(["git", "checkout", "-q", "--detach", base], cwd=ws)
    sh(["git", "apply", "--index", "--whitespace=nowarn", str((build / "diff.patch").resolve())], cwd=ws)
    tree = sh(["git", "write-tree"], cwd=ws).stdout.strip()
    remote = sh(["git", "ls-remote", "origin", f"refs/heads/{branch}"], cwd=ws).stdout.split()
    parent = base
    force = True
    if rnd > 0 and remote:
        sh(["git", "fetch", "-q", "origin", f"refs/heads/{branch}"], cwd=ws)
        parent, force = remote[0], False  # rework rounds add a commit on top of the previous round
    bot = os.environ.get("AUTOSE_BOT", "autose-dev[bot]")
    env = dict(os.environ, GIT_AUTHOR_NAME=bot, GIT_COMMITTER_NAME=bot,
               GIT_AUTHOR_EMAIL=os.environ.get("AUTOSE_BOT_EMAIL", "autose@users.noreply.github.com"),
               GIT_COMMITTER_EMAIL=os.environ.get("AUTOSE_BOT_EMAIL", "autose@users.noreply.github.com"))
    msg = f"{'Fix' if rnd == 0 else 'Address review'}: {t['title']} (#{issue}, round {rnd})\n\nautose run: {run_url()}"
    commit = sh(["git", "commit-tree", tree, "-p", parent, "-m", msg], cwd=ws, env=env).stdout.strip()
    push = ["git", "push", "-q", "origin", f"{commit}:refs/heads/{branch}"]
    if force:
        push.insert(2, "--force")
    sh(push, cwd=ws)
    log(f"pushed {commit[:7]} to {branch}")

    body = pr_body(t, build, rnd, "waiting for CI")
    (out / "pr.md").write_text(body)
    existing = json.loads(gh(["pr", "list", "-R", repo(), "--head", branch, "--state", "open", "--json",
                              "number,url"]))
    if existing:
        number, url = existing[0]["number"], existing[0]["url"]
        gh(["pr", "edit", str(number), "-R", repo(), "--body-file", str(out / "pr.md")])
    else:
        url = gh(["pr", "create", "-R", repo(), "--base", "main", "--head", branch,
                  "--title", f"Fix #{issue}: {t['title']}", "--body-file", str(out / "pr.md")]).strip()
        number = int(url.rstrip("/").split("/")[-1])
    log(f"PR {url}")
    update_status(issue, [("release", "running", f"PR {url}, waiting for CI")], "release: PR open, CI running")

    ok, detail, runs = wait_for_ci(commit, read_env)
    body = pr_body(t, build, rnd, "CI passed" if ok else "CI FAILED")
    body += f"\n\n## autose check (release): {'PASSED' if ok else 'FAILED'}\n- PR: {url}\n- head: `{commit}`\n- {detail}\n"
    (out / "pr.md").write_text(body)
    gh(["pr", "edit", str(number), "-R", repo(), "--body-file", str(out / "pr.md")])
    dump_json(out / "release.json", {"pr": number, "url": url, "head": commit, "ci_ok": ok, "detail": detail,
                                     "checks": [{"name": r["name"], "conclusion": r["conclusion"]} for r in runs]})
    set_output("pr", str(number))
    set_output("head", commit)
    set_output("ci_ok", "true" if ok else "false")
    write_summary(f"### Release round {rnd}\n\nPR: {url}\n\nCI: {detail}")
    update_status(issue, [("release", "passed" if ok else "STOPPED", f"{url}: {detail}"[:300])],
                  "release done, independent review next" if ok else "stopped at release: CI failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
