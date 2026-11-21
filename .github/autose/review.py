"""Review job (stage 6, ADR-008). Runs the independent reviewer on the issue + diff + checkout at the PR
head, validates its structured verdict, posts it as a PR review, and computes the route.
Environment: AUTOSE_TRIGGER, AUTOSE_WS (PR head checkout), AUTOSE_PR, AUTOSE_ROUND, AUTOSE_VARIANT
(isolated | shared), AUTOSE_REVIEWER_MODEL (optional), AUTOSE_SHARED_DIR (shared variant only),
AUTOSE_OUT, AUTOSE_LAST_ROUND ('true' when no rework round is left), GH_TOKEN (used only here,
after the reviewer process has exited; never passed to the agent)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from agents import run_agent
from common import CONFIG, dump_json, gh, gh_api, log, repo, run_url, set_output, sh, untrusted_block, \
    update_status, write_summary

CATEGORIES = ["requirements", "design", "implementation", "testing"]
SCHEMA = {
    "type": "object",
    "properties": {
        "own_checks": {"type": "array", "description": "Your acceptance checks derived from the issue, each verified by running code",
                       "items": {"type": "object", "properties": {
                           "check": {"type": "string"}, "command": {"type": "string"},
                           "result": {"type": "string", "enum": ["pass", "fail", "not_run"]},
                           "output_excerpt": {"type": "string"}},
                           "required": ["check", "command", "result", "output_excerpt"]}},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "category": {"type": "string", "enum": CATEGORIES},
            "blocking": {"type": "boolean"}, "file": {"type": "string"}, "line": {"type": "integer"},
            "evidence": {"type": "string"}},
            "required": ["title", "category", "blocking", "file", "evidence"]}},
        "notes": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
    },
    "required": ["own_checks", "findings", "notes", "summary", "verdict"],
}
MAX_DIFF = 60000


def build_prompt(t: dict, diff: str, base: str, variant: str, shared: str) -> str:
    issue = f"Issue #{t['issue']}: {t['title']}\n\n{t['body'] or '(no body)'}"
    for i, c in enumerate(t.get("maintainer_comments", []), 1):
        issue += f"\n\n--- maintainer comment {i} by @{c['author']} ---\n{c['body']}"
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + f"\n... (diff truncated; run `git diff {base[:12]}...HEAD` to see the rest)"
    p = (f"Review this pull request. The repository at the PR head is your working directory; the base "
         f"commit is `{base}` (`git diff {base[:12]}...HEAD` shows the change).\n\n"
         f"{untrusted_block('issue', issue)}\n\n<diff>\n{diff}\n</diff>\n")
    if variant == "shared" and shared:
        p += ("\n\nContext from the authors of this pull request (their description, specification, design and "
              f"test report):\n<author-context>\n{shared}\n</author-context>\n")
    return p


def validate(v: dict) -> tuple[dict, list[str]]:
    notes = []
    blocking = [f for f in v.get("findings", []) if f.get("blocking")]
    valid_blocking = [f for f in blocking if f.get("file") and len(f.get("evidence", "").strip()) >= 20]
    if v.get("verdict") == "approve" and valid_blocking:
        notes.append("verdict was approve but blocking findings exist; treated as request_changes")
        v["verdict"] = "request_changes"
    if v.get("verdict") == "request_changes" and not valid_blocking:
        notes.append("request_changes without a blocking finding that has a file and evidence; posted as a comment")
        v["verdict"] = "comment"
    v["_blocking"] = valid_blocking
    return v, notes


def route(blocking: list[dict]) -> str:
    cats = [f["category"] for f in blocking if f.get("category") in CATEGORIES]
    return min(cats, key=CATEGORIES.index) if cats else ""


def render(v: dict, model: str, variant: str, rnd: int, rt: str, last: bool, notes: list[str]) -> str:
    head = {"approve": "APPROVE", "request_changes": "REQUEST CHANGES", "comment": "COMMENT"}[v["verdict"]]
    md = [f"## autose independent review: {head}",
          f"<sub>Round {rnd}. Reviewer model `{model}`, variant `{variant}`. The reviewer saw only the issue, "
          f"the diff and the repository at the PR head, and could run code but not push.</sub>", "",
          v.get("summary", ""), "", "### The reviewer's own acceptance checks (derived from the issue)",
          "| Check | Command | Result |", "|---|---|---|"]
    for c in v.get("own_checks", []):
        cmd = c.get("command", "").replace("|", "\\|").replace("\n", " ")[:160]
        md.append(f"| {c.get('check', '').replace('|', '/')[:160]} | `{cmd}` | {c.get('result')} |")
    blocking = v.get("_blocking", [])
    if blocking:
        md += ["", "### Blocking findings"]
        for f in blocking:
            loc = f"{f.get('file')}:{f.get('line')}" if f.get("line") else f.get("file")
            md += [f"- **[{f['category']}] {f['title']}** (`{loc}`)", f"  <details><summary>evidence</summary>\n\n"
                   f"```\n{f.get('evidence', '')[:2500]}\n```\n</details>"]
    other = [f for f in v.get("findings", []) if not f.get("blocking")]
    if other or v.get("notes"):
        md += ["", "### Non-blocking"]
        md += [f"- [{f.get('category')}] {f.get('title')} (`{f.get('file')}`)" for f in other]
        md += [f"- {n}" for n in v.get("notes", [])]
    md += ["", "### Routing"]
    if v["verdict"] == "approve":
        md.append("Approved. A maintainer decides whether to merge.")
    elif v["verdict"] == "request_changes":
        md.append(f"Earliest stage named by a blocking finding: **{rt}**. " +
                  ("Review rounds are used up; a maintainer decides." if last else f"The next round restarts at {rt}."))
    for n in notes:
        md.append(f"- validation: {n}")
    md += ["", f"Run: {run_url()}"]
    return "\n".join(md)


def main() -> int:
    t = json.loads(Path(os.environ["AUTOSE_TRIGGER"]).read_text())
    ws = Path(os.environ["AUTOSE_WS"]).resolve()
    out = Path(os.environ.get("AUTOSE_OUT", "review-out")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pr = int(os.environ["AUTOSE_PR"])
    rnd = int(os.environ.get("AUTOSE_ROUND", "0"))
    variant = os.environ.get("AUTOSE_VARIANT", "isolated") or "isolated"
    model = os.environ.get("AUTOSE_REVIEWER_MODEL") or CONFIG["models"]["reviewer"]
    last = os.environ.get("AUTOSE_LAST_ROUND", "false") == "true"
    base = sh(["git", "merge-base", "origin/main", "HEAD"], cwd=ws).stdout.strip()
    diff = sh(["git", "diff", f"{base}...HEAD"], cwd=ws).stdout
    (out / "reviewed.diff").write_text(diff)
    shared = ""
    if variant == "shared":  # experiment E1 only: give the reviewer the authors' context
        prd = gh_api(f"repos/{repo()}/pulls/{pr}")
        shared = f"PR description:\n{prd.get('body') or ''}"
        sd = os.environ.get("AUTOSE_SHARED_DIR")
        if sd and Path(sd).exists():
            for name in ("spec.md", "design.md", "test_report.md"):
                for p in Path(sd).rglob(name):
                    shared += f"\n\n<{name}>\n{p.read_text()}\n</{name}>"
                    break
    prompt = build_prompt(t, diff, base, variant, shared)
    stage_dir = out.parent / "work-review"
    res = {}
    v: dict = {}
    for attempt in range(CONFIG["budgets"]["reviewer_evidence_retries"] + 1):
        res = run_agent("reviewer", prompt, ws, stage_dir, out / "transcripts" / f"review-{attempt}.jsonl",
                        json_schema=SCHEMA, model=model, deny_paths=[str(out)], ledger=out / "usage.jsonl",
                        label=f"r{rnd}-review-{variant}")
        v = res.get("structured") or {}
        if res.get("error_kind") == "sandbox":  # no command could run: no verdict is better than a guess
            v = {}
            break
        if not res["ok"] or not v:
            continue
        executed = [c for c in v.get("own_checks", []) if c.get("command") and c.get("result") in ("pass", "fail")]
        if v.get("verdict") == "approve" and not executed:
            prompt += ("\n\nYour previous answer approved without any executed verification. Approval requires that "
                       "you run your checks. Do the review again and record the commands you ran.")
            continue
        break
    if not v:
        reason = f"reviewer produced no verdict ({res.get('error_kind')}): {res.get('error', '')[:300]}"
        (out / "review.md").write_text(f"## autose independent review: NO VERDICT\n\n{reason}\n")
        dump_json(out / "review.json", {"verdict": "none", "error": reason})
        set_output("verdict", "none")
        set_output("route", "")
        update_status(int(t["issue"]), [("review", "STOPPED", reason[:200])], "stopped at review")
        return 0
    v, notes = validate(v)
    rt = route(v["_blocking"]) if v["verdict"] == "request_changes" else ""
    md = render(v, model, variant, rnd, rt, last, notes)
    (out / "review.md").write_text(md)
    dump_json(out / "review.json", {**{k: v[k] for k in v if not k.startswith("_")},
                                    "findings": [{**f, "blocking": f in v["_blocking"]} for f in v.get("findings", [])],
                                    "route": rt, "model": model, "variant": variant, "round": rnd,
                                    "validation_notes": notes, "pr": pr})
    event = {"approve": "APPROVE", "request_changes": "REQUEST_CHANGES", "comment": "COMMENT"}[v["verdict"]]
    head = sh(["git", "rev-parse", "HEAD"], cwd=ws).stdout.strip()
    try:
        gh_api(f"repos/{repo()}/pulls/{pr}/reviews", "POST", {"commit_id": head, "body": md, "event": event})
    except Exception as e:  # e.g. approvals by Actions disabled: fall back to a comment review
        log(f"posting {event} failed ({e}); posting as COMMENT")
        gh_api(f"repos/{repo()}/pulls/{pr}/reviews", "POST", {"commit_id": head, "body": md, "event": "COMMENT"})
    set_output("verdict", v["verdict"])
    set_output("route", rt)
    set_output("pr", str(pr))
    write_summary(md)
    detail = f"{v['verdict']}" + (f", route to {rt}" if rt else "") + f" (PR #{pr})"
    final = v["verdict"] != "request_changes" or last
    update_status(int(t["issue"]), [("review", v["verdict"], detail)],
                  ("finished: a maintainer decides" if final else f"review requested changes, round {rnd + 1} starts at {rt}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
