"""Gate job (ADR-010): decide whether this event starts a run, who asked, and what the agents get.
Never interpolates event text into a shell; reads the event payload file."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from common import CONFIG, gh, gh_api, log, repo, run_url, set_output, update_status

ALLOWED = {"admin", "maintain", "write"}
_perm_cache: dict[str, str] = {}


def permission(user: str) -> str:
    if user not in _perm_cache:
        try:
            _perm_cache[user] = gh_api(f"repos/{repo()}/collaborators/{user}/permission").get("permission", "none")
        except Exception:
            _perm_cache[user] = "none"
    return _perm_cache[user]


def stop(reason: str, reply_to: int | None = None) -> int:
    log(f"not starting: {reason}")
    set_output("proceed", "false")
    if reply_to:
        try:
            gh_api(f"repos/{repo()}/issues/{reply_to}/comments", "POST",
                   {"body": f"autose did not start: {reason}"})
        except Exception:
            pass
    return 0


def main() -> int:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    ev = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    start, pr, variant, rmodel, rework = "requirements", "", "isolated", "", "true"

    if event_name == "workflow_dispatch":
        inp = ev.get("inputs", {})
        issue = int(inp["issue"])
        start = inp.get("start_stage") or "requirements"
        pr = inp.get("pr") or ""
        variant = inp.get("reviewer_variant") or "isolated"
        rmodel = inp.get("reviewer_model") or ""
        rework = str(inp.get("rework", "true")).lower()
        actor = os.environ.get("GITHUB_ACTOR", "")
        sender = inp.get("requested_by") or actor if actor == "github-actions[bot]" else actor
        if permission(sender) not in ALLOWED:
            return stop(f"@{sender} does not have write access")
        comment_id = None
    else:
        sender_obj = ev.get("sender", {})
        sender = sender_obj.get("login", "")
        if sender_obj.get("type") == "Bot":
            return stop("events from bots are ignored")
        issue_obj = ev["issue"]
        number = issue_obj["number"]
        comment_id = None
        if event_name == "issues":
            if ev.get("label", {}).get("name") != CONFIG["label"]:
                return stop("label is not the autose label")
        elif event_name == "issue_comment":
            body = ev["comment"]["body"].strip()
            if not body.startswith("/autose"):
                return stop("comment is not an /autose command")
            comment_id = ev["comment"]["id"]
        else:
            return stop(f"unsupported event {event_name}")
        if permission(sender) not in ALLOWED:
            return stop(f"@{sender} needs write access to start autose", reply_to=number)
        if comment_id:
            try:
                gh_api(f"repos/{repo()}/issues/comments/{comment_id}/reactions", "POST", {"content": "eyes"})
            except Exception:
                pass
        if issue_obj.get("pull_request"):
            # PR comment: resolve the issue and re-dispatch under the issue's concurrency group (ADR-010)
            prd = gh_api(f"repos/{repo()}/pulls/{number}")
            m = re.match(re.escape(CONFIG["branch_prefix"]) + r"(\d+)$", prd["head"]["ref"])
            if not m or prd["state"] != "open":
                return stop("/autose on a PR works only on open autose/issue-N pull requests", reply_to=number)
            gh(["workflow", "run", "autose.yml", "-R", repo(), "-f", f"issue={m.group(1)}", "-f", f"pr={number}",
                "-f", "start_stage=review", "-f", f"requested_by={sender}"])
            log(f"re-dispatched as a review run for issue {m.group(1)}")
            set_output("proceed", "false")
            return 0
        issue = number

    data = gh_api(f"repos/{repo()}/issues/{issue}")
    if data.get("pull_request"):
        return stop("the issue number points to a pull request")
    comments = gh_api(f"repos/{repo()}/issues/{issue}/comments?per_page=100") or []
    maint = []
    for c in comments:
        user = c["user"]["login"]
        if c["user"].get("type") == "Bot" or "<!-- autose" in (c.get("body") or ""):
            continue
        if permission(user) in ALLOWED:  # only maintainers' comments count as stakeholder answers
            text = re.sub(r"^/autose[^\n]*\n?", "", c["body"].strip()).strip()
            if text:
                maint.append({"author": user, "body": text, "created_at": c["created_at"]})
    trigger = {"issue": issue, "title": data["title"], "body": data.get("body") or "",
               "author": data["user"]["login"], "maintainer_comments": maint, "requested_by": sender,
               "event": event_name, "start_stage": start, "pr": pr, "reviewer_variant": variant,
               "reviewer_model": rmodel, "rework": rework, "run_url": run_url()}
    Path("trigger.json").write_text(json.dumps(trigger, indent=2))
    for k in ("issue", "pr", "reviewer_variant", "reviewer_model", "rework"):
        set_output(k, str(trigger[k]))
    set_output("start_stage", start)
    set_output("proceed", "true")
    if start == "requirements":
        update_status(issue, [(s, "-", "") for s in
                              ("requirements", "design", "implementation", "testing", "release", "review")],
                      f"run started by @{sender}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
