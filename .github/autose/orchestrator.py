"""Build job: deterministic state machine over stages 1-4 (ADR-002). Agents do the work, checks.py
decides, this file routes. Environment:
  AUTOSE_TRIGGER  trigger.json from the gate job
  AUTOSE_ROUND    0, 1 or 2
  AUTOSE_START    requirements | design | implementation | testing
  AUTOSE_PREV     directory with the previous round's build + review artifacts (rounds 1, 2)
  AUTOSE_OUT      output directory (uploaded as the round's build artifact)
  AUTOSE_WS       the repository checkout at the base commit
  AUTOSE_PYTHON   python with the test dependencies
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import checks
from agents import run_agent
from common import CONFIG, dump_json, load_json, log, set_output, sh, untrusted_block, update_status, write_summary

ORDER = ["requirements", "design", "implementation", "testing"]
ICON = {"passed": "passed", "failed": "FAILED", "running": "running", "pending": "-", "skipped": "reused",
        "stopped": "STOPPED"}


class Stop(Exception):
    def __init__(self, stage: str, reason: str, kind: str = "check-failed"):
        super().__init__(reason)
        self.stage, self.reason, self.kind = stage, reason, kind


class Pipeline:
    def __init__(self) -> None:
        self.trigger = json.loads(Path(os.environ["AUTOSE_TRIGGER"]).read_text())
        self.issue = int(self.trigger["issue"])
        self.round = int(os.environ.get("AUTOSE_ROUND", "0"))
        self.start = os.environ.get("AUTOSE_START", "requirements")
        self.prev = Path(os.environ["AUTOSE_PREV"]) if os.environ.get("AUTOSE_PREV") else None
        self.out = Path(os.environ["AUTOSE_OUT"]).resolve()
        self.ws = Path(os.environ["AUTOSE_WS"]).resolve()
        self.py = os.environ.get("AUTOSE_PYTHON", sys.executable)
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "checks").mkdir(exist_ok=True)
        self.ledger = self.out / "usage.jsonl"
        self.state = load_json(self.out / "state.json") or {
            "issue": self.issue, "round": self.round, "start": self.start, "events": [],
            "stages": {s: {"status": "pending", "attempts": 0} for s in ORDER},
            "budgets": {k: 0 for k in CONFIG["budgets"]}, "final": None}
        self.base = sh(["git", "rev-parse", "HEAD"], cwd=self.ws).stdout.strip()
        self.state["base"] = self.base
        self.deny = [str(self.out)]  # agents never read other stages' outputs directly

    # ------------------------------------------------------------------ bookkeeping
    def event(self, stage: str, what: str, **kw) -> None:
        self.state["events"].append({"t": time.strftime("%H:%M:%S"), "stage": stage, "what": what, **kw})
        self.save()

    def set_stage(self, stage: str, status: str, **kw) -> None:
        self.state["stages"][stage].update(status=status, **kw)
        self.save()
        self.publish(f"round {self.round}: {stage} {status}")

    def save(self) -> None:
        dump_json(self.out / "state.json", self.state)

    def publish(self, headline: str, extra: str = "") -> None:
        rows = []
        for s in ORDER:
            st = self.state["stages"][s]
            detail = "; ".join(st.get("reasons", [])[:2])[:200].replace("|", "/").replace("\n", " ")
            rows.append((s, ICON.get(st["status"], st["status"]), f"attempts {st['attempts']}. {detail}"))
        rows += [("release", "-", "next job"), ("review", "-", "next job")]
        update_status(self.issue, rows, headline, extra)

    def stage_dir(self, stage: str) -> Path:
        d = self.out.parent / f"work-{stage}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def agent(self, role: str, stage: str, prompt: str, **kw) -> dict:
        n = self.state["stages"][stage]["attempts"]
        res = run_agent(role, prompt, self.ws, self.stage_dir(stage),
                        self.out / "transcripts" / f"{stage}-{role}-{n}.jsonl",
                        deny_paths=self.deny, ledger=self.ledger, label=f"r{self.round}-{stage}-{n}", **kw)
        if not res["ok"] and res.get("error_kind") in ("provider", "timeout"):
            raise Stop(stage, f"{role} agent: {res.get('error_kind')}: {res.get('error', '')[:400]}",
                       "provider-unavailable" if res["error_kind"] == "provider" else "timeout")
        return res

    # ------------------------------------------------------------------ git workspace helpers
    def reset_ws(self) -> None:
        sh(["git", "reset", "-q", "--hard", self.base], cwd=self.ws)
        sh(["git", "clean", "-fdq"], cwd=self.ws)

    def apply(self, patch: str) -> None:
        if patch.strip():
            p = self.out.parent / "apply.patch"
            p.write_text(patch)
            sh(["git", "apply", "--whitespace=nowarn", str(p)], cwd=self.ws)

    def capture(self, pathspecs: list[str]) -> str:
        sh(["git", "add", "-A"], cwd=self.ws)
        diff = sh(["git", "diff", "--cached", "--binary", self.base, "--", *pathspecs], cwd=self.ws).stdout
        sh(["git", "reset", "-q"], cwd=self.ws)
        return diff

    # ------------------------------------------------------------------ inputs
    def prev_text(self, name: str) -> str:
        if self.prev:
            for p in self.prev.rglob(name):
                return p.read_text()
        return ""

    def review_findings(self, category: str | None = None) -> list[dict]:
        data = load_json(next(self.prev.rglob("review.json"), Path("/x")), {}) if self.prev else {}
        found = [f for f in (data or {}).get("findings", []) if f.get("blocking", True)]
        return [f for f in found if category is None or f.get("category") == category]

    def issue_input(self) -> str:
        t = self.trigger
        parts = [f"Issue #{self.issue}: {t['title']}\n\n{t['body'] or '(no body)'}"]
        for i, c in enumerate(t.get("maintainer_comments", []), 1):
            parts.append(f"--- maintainer comment {i} by @{c['author']} ---\n{c['body']}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ stage 1
    def requirements(self) -> str:
        stage = "requirements"
        d = self.stage_dir(stage)
        spec_path = d / "spec.md"
        raw = self.issue_input()
        sha = hashlib.sha256(raw.encode()).hexdigest()
        feedback = ""
        findings = self.review_findings("requirements")
        if findings and self.prev_text("spec.md"):
            spec_path.write_text(self.prev_text("spec.md"))
            feedback = "A reviewer says the specification misreads the issue:\n" + json.dumps(findings, indent=1)
        for attempt in range(CONFIG["budgets"]["spec_retries"] + 1):
            self.state["stages"][stage]["attempts"] += 1
            self.set_stage(stage, "running")
            prompt = (f"Write the specification for GitHub issue #{self.issue} of Python-Markdown to "
                      f"`{spec_path}`.\nInput SHA-256: {sha}\nThe repository is your working directory "
                      f"(read only for you).\n\n{untrusted_block('issue', raw)}\n")
            if feedback:
                prompt += f"\n\n`{spec_path}` already exists. Fix these problems in it:\n{feedback}\n"
            self.agent("analyst", stage, prompt)
            text = spec_path.read_text() if spec_path.exists() else ""
            res = checks.check_spec(text)
            judge = {}
            if not res["needs_clarification"] and res["acs"] and not res["reasons"]:
                judge = self.judge(res["acs"])
                bad = [c for c in judge.get("criteria", []) if not c.get("testable")]
                known = {a["id"] for a in res["acs"]}
                missing = known - {c.get("id") for c in judge.get("criteria", [])}
                if missing:
                    res["reasons"].append(f"judge returned no verdict for {sorted(missing)}")
                for c in bad:
                    res["reasons"].append(f"{c['id']}: judged not testable: {c.get('reason', '')}")
                res["ok"] = not res["reasons"]
            dump_json(self.out / "checks" / f"spec-{attempt}.json", {**res, "judge": judge})
            if res["needs_clarification"]:
                self.finish_artifact(spec_path, "spec.md", False, ["No Must requirement can be READY without answers "
                                                                   "to the open P1 questions."])
                self.ask_clarification(res["open_p1"])
                raise Stop(stage, "needs clarification: open P1 questions block every Must requirement",
                           "needs-clarification")
            if res["ok"]:
                self.state["ready_acs"] = [a["id"] for a in res["acs"]]
                self.finish_artifact(spec_path, "spec.md", True, [f"{len(res['acs'])} acceptance criteria, all "
                                                                  f"testable; READY: {', '.join(res['ready'])}"])
                self.set_stage(stage, "passed", reasons=[])
                return (self.out / "spec.md").read_text()
            self.set_stage(stage, "failed", reasons=res["reasons"])
            self.event(stage, "check failed", reasons=res["reasons"])
            feedback = "\n".join(f"- {r}" for r in res["reasons"])
        self.finish_artifact(spec_path, "spec.md", False, res["reasons"])
        raise Stop(stage, "specification check failed after retry: " + "; ".join(res["reasons"][:5]))

    def judge(self, acs: list[dict]) -> dict:
        prompt = "Acceptance criteria:\n" + "\n".join(f"- {a['id']}: {a['text']}" for a in acs)
        res = self.agent("judge", "requirements", prompt, json_schema=checks.JUDGE_SCHEMA)
        return res.get("structured") or {}

    def ask_clarification(self, questions: list[dict]) -> None:
        from common import gh_api, repo
        lines = ["**autose needs answers before it can continue.** The issue leaves open decisions that "
                 "change what a correct fix is. A maintainer can answer below and comment `/autose` to restart.\n"]
        for q in questions:
            lines.append(f"**{q['id']}: {q['title']}**\n{q['question']}\n\nOptions: {q['options']}\n")
        try:
            gh_api(f"repos/{repo()}/issues/{self.issue}/comments", "POST", {"body": "\n".join(lines)})
        except Exception as e:
            log(f"could not post questions: {e}")

    def finish_artifact(self, src: Path, name: str, ok: bool, notes: list[str]) -> None:
        text = src.read_text() if src.exists() else f"# {name}\n\n(the agent did not write this file)\n"
        text += (f"\n\n---\n## autose check: {'PASSED' if ok else 'FAILED'}\n" +
                 "\n".join(f"- {n}" for n in notes) + "\n")
        (self.out / name).write_text(text)

    # ------------------------------------------------------------------ stage 2
    def design(self, spec: str, feedback: str = "") -> str:
        stage = "design"
        d = self.stage_dir(stage)
        path = d / "design.md"
        if feedback and self.prev_text("design.md") and not path.exists():
            path.write_text(self.prev_text("design.md"))
        for attempt in range(CONFIG["budgets"]["design_retries"] + 1):
            self.state["stages"][stage]["attempts"] += 1
            self.set_stage(stage, "running")
            self.reset_ws()
            prompt = (f"Plan the change for this specification and write the plan to `{path}`.\n\n"
                      f"<spec.md>\n{spec}\n</spec.md>\n")
            if feedback:
                prompt += f"\n`{path}` exists from an earlier attempt. Revise it. Problems to address:\n{feedback}\n"
            self.agent("architect", stage, prompt)
            self.reset_ws()  # discard anything the architect's probes changed
            text = path.read_text() if path.exists() else ""
            res = checks.check_design(text, self.ws, self.state.get("ready_acs", []))
            dump_json(self.out / "checks" / f"design-{self.state['stages'][stage]['attempts']}.json", res)
            if res["ok"]:
                self.finish_artifact(path, "design.md", True, [f"{len(res['rows'])} planned changes; every "
                                                               f"READY criterion mapped; symbols found"])
                self.set_stage(stage, "passed", reasons=[])
                return (self.out / "design.md").read_text()
            self.set_stage(stage, "failed", reasons=res["reasons"])
            feedback = "\n".join(f"- {r}" for r in res["reasons"])
        self.finish_artifact(path, "design.md", False, res["reasons"])
        raise Stop(stage, "design check failed after retry: " + "; ".join(res["reasons"][:5]))

    # ------------------------------------------------------------------ stages 3 + 4
    def build_and_test(self, spec: str, design: str, state: str, upstream_changed: bool) -> None:
        b = CONFIG["budgets"]
        impl = self.prev_text("impl.patch") if self.round else ""
        tests = self.prev_text("tests.patch") if self.round else ""
        dev_feedback = self._fmt_findings(self.review_findings("implementation")) if self.round else ""
        test_feedback = self._fmt_findings(self.review_findings("testing")) if self.round else ""
        if tests and upstream_changed and not test_feedback:
            test_feedback = "The specification or design was revised in this round; update the tests to match it."
        need_tests = not tests or bool(test_feedback)
        consecutive = 0
        last_failed_hash = None
        dispute = ""
        baseline = self.baseline()
        while True:
            if state == "implementation":
                st = self.state["stages"]["implementation"]
                if st["attempts"] >= b["impl_attempts"]:
                    raise Stop("implementation", f"implementation budget used ({b['impl_attempts']} attempts)")
                st["attempts"] += 1
                self.set_stage("implementation", "running")
                self.reset_ws()
                self.apply(impl)
                self.apply(tests)
                prompt = (f"Implement this change in the working directory.\n\n<spec.md>\n{spec}\n</spec.md>\n\n"
                          f"<design.md>\n{design}\n</design.md>\n")
                if tests:
                    prompt += "\nThe tester's tests for this change are already in `tests/` (you cannot edit them).\n"
                if dev_feedback:
                    prompt += f"\nThe previous attempt did not pass. Fix this:\n{dev_feedback}\n"
                res = self.agent("developer", "implementation", prompt)
                dispute = "\n".join(l for l in (res.get("text") or "").splitlines() if l.startswith("TEST_DISPUTE"))
                new_impl = self.capture(CONFIG["developer_write_globs"])
                stray = checks.check_paths(checks.changed_files(self.ws), CONFIG["developer_write_globs"] +
                                           CONFIG["tester_write_globs"])
                self.reset_ws()
                self.apply(new_impl)
                bres = checks.check_build(self.ws, self.py)
                if stray:
                    bres["reasons"].insert(0, f"(discarded edits outside allowed paths: {stray})")
                dump_json(self.out / "checks" / f"build-{st['attempts']}.json", bres)
                h = hashlib.sha256(new_impl.encode()).hexdigest()
                if not bres["ok"]:
                    self.set_stage("implementation", "failed", reasons=bres["reasons"])
                    if h == last_failed_hash:
                        raise Stop("implementation", "no progress: the same failing diff was produced twice",
                                   "no-progress")
                    last_failed_hash = h
                    dev_feedback = "\n".join(f"- {r}" for r in bres["reasons"])
                    impl = new_impl
                    continue
                impl = new_impl
                (self.out / "impl.patch").write_text(impl)
                self.set_stage("implementation", "passed", reasons=[], files=bres["files"])
                state = "testing"
            if state == "testing":
                st = self.state["stages"]["testing"]
                if need_tests:
                    if st["attempts"] >= b["tester_attempts"] + b["test_disputes"]:
                        raise Stop("testing", "tester budget used")
                    st["attempts"] += 1
                    self.set_stage("testing", "running")
                    self.reset_ws()
                    self.apply(tests)
                    prompt = (f"Write the tests for this change. The working directory is the base commit.\n\n"
                              f"<spec.md>\n{spec}\n</spec.md>\n\n<design.md>\n{design}\n</design.md>\n")
                    if tests:
                        prompt += "\nTests from an earlier attempt are already in `tests/`. Revise them.\n"
                    if test_feedback:
                        prompt += f"\nProblems to fix:\n{test_feedback}\n"
                    self.agent("tester", "testing", prompt)
                    tests = self.capture(CONFIG["tester_write_globs"])
                    (self.out / "tests.patch").write_text(tests)
                    need_tests = False
                    test_feedback = ""
                tres, detail = self.evaluate(impl, tests, baseline)
                n = len(list((self.out / "checks").glob("tests-*.json")))
                dump_json(self.out / "checks" / f"tests-{n}.json", {**tres, **detail})
                self.write_test_report(tres, detail)
                if tres["ok"]:
                    self.set_stage("testing", "passed", reasons=[])
                    self.reset_ws()
                    self.apply(impl)
                    self.apply(tests)
                    (self.out / "diff.patch").write_text(self.capture(["."]))
                    return
                self.set_stage("testing", "failed", reasons=tres["reasons"])
                if tres["owner"] == "tester":
                    need_tests = True
                    test_feedback = "\n".join(f"- {r}" for r in tres["reasons"])
                    continue
                if dispute and self.state["budgets"]["test_disputes"] < b["test_disputes"]:
                    self.state["budgets"]["test_disputes"] += 1
                    self.event("testing", "developer disputes a test", dispute=dispute)
                    need_tests = True
                    test_feedback = "The developer disputes these tests; fix them only if they contradict the spec:\n" + dispute
                    dispute = ""
                    continue
                consecutive += 1
                dev_feedback = self._fmt_test_failures(tres)
                if consecutive >= 2 and self.state["budgets"]["design_revisits"] < b["design_revisits"]:
                    self.state["budgets"]["design_revisits"] += 1
                    self.event("design", "revisit after two failed implementation attempts")
                    design = self.design(spec, "Two implementation attempts failed the tests:\n" + dev_feedback)
                    consecutive = 0
                    need_tests = True
                    test_feedback = "The design changed; update the tests to the new Interface section if needed."
                state = "implementation"

    def baseline(self) -> dict:
        path = self.out / "baseline.json"
        if not path.exists():
            self.reset_ws()
            sh([self.py, "-m", "pip", "install", "-q", "--no-deps", "-e", "."], cwd=self.ws)
            checks.run_tests(self.ws, self.py, path)
        return json.loads(path.read_text())

    def evaluate(self, impl: str, tests: str, baseline: dict) -> tuple[dict, dict]:
        files = checks.test_files_in(self._patch_files(tests))
        mods = [f for f in files]
        # base commit + tests only
        self.reset_ws()
        self.apply(tests)
        base_new = checks.run_tests(self.ws, self.py, self.out / "tests-base.json", mods) if mods else {}
        base_new = {t: v for t, v in base_new.items() if t not in baseline}
        # head = base + implementation + tests
        self.reset_ws()
        self.apply(impl)
        self.apply(tests)
        sh([self.py, "-m", "pip", "install", "-q", "--no-deps", "-e", "."], cwd=self.ws, check=False)
        head_new = checks.run_tests(self.ws, self.py, self.out / "tests-head-new.json", mods) if mods else {}
        head_new = {t: v for t, v in head_new.items() if t not in baseline}
        head_all = checks.run_tests(self.ws, self.py, self.out / "tests-head-all.json")
        test_src = "\n".join(l for l in tests.splitlines() if l.startswith("+"))
        res = checks.check_tests(new_ids_head=head_new, base_results=base_new, head_results=head_all,
                                 baseline=baseline, ready_acs=self.state.get("ready_acs", []), test_source=test_src)
        return res, {"base_new": base_new, "head_new": head_new,
                     "head_counts": _counts(head_all), "baseline_counts": _counts(baseline)}

    def write_test_report(self, res: dict, d: dict) -> None:
        acs = self.state.get("ready_acs", [])
        rows = []
        for ac in acs:
            key = ac.lower().replace("-", "_")
            ids = [t for t in d["head_new"] if key in t.lower()]
            for t in ids or ["(none)"]:
                rows.append(f"| {ac} | `{t.split('.')[-1]}` | {d['base_new'].get(t, {}).get('outcome', '-')} | "
                            f"{d['head_new'].get(t, {}).get('outcome', '-')} |")
        others = [t for t in d["head_new"] if not any(a.lower().replace('-', '_') in t.lower() for a in acs)]
        md = [f"# Test report: issue #{self.issue}, round {self.round}", "",
              f"Base commit `{self.base[:7]}`. New tests are run on the base commit (they must fail there) "
              f"and on the head (they must pass). The full existing suite is run on the head.", "",
              "| Acceptance criterion | Test | Base | Head |", "|---|---|---|---|", *rows, ""]
        if others:
            md += [f"Other new tests: {', '.join('`' + t.split('.')[-1] + '`' for t in others)}", ""]
        md += [f"Full suite, base before the change: {d['baseline_counts']}",
               f"Full suite, head: {d['head_counts']}", ""]
        if res["regressions"]:
            md += ["## Regressions", *[f"- `{t}`" for t in list(res["regressions"])[:20]], ""]
        if res["failing_new"]:
            md += ["## New tests failing on head", *[f"- `{t}`: {v['detail'][-300:]}" for t, v in
                                                     list(res["failing_new"].items())[:10]], ""]
        md += [f"## autose check: {'PASSED' if res['ok'] else 'FAILED'}", *[f"- {r}" for r in res["reasons"]],
               "" if res["ok"] else f"- routed to: {res['owner']}"]
        hist = [e for e in self.state["events"] if e["stage"] in ("implementation", "testing", "design")]
        if hist:
            md += ["", "## History", *[f"- {e['t']} {e['stage']}: {e['what']}" for e in hist]]
        (self.out / "test_report.md").write_text("\n".join(md) + "\n")

    @staticmethod
    def _patch_files(patch: str) -> list[str]:
        return re.findall(r"^\+\+\+ b/(.+)$", patch, re.M)

    @staticmethod
    def _fmt_findings(findings: list[dict]) -> str:
        if not findings:
            return ""
        return "An independent reviewer requested changes:\n" + "\n".join(
            f"- [{f.get('category')}] {f.get('title')} ({f.get('file')}): {f.get('evidence', '')[:800]}" for f in findings)

    @staticmethod
    def _fmt_test_failures(res: dict) -> str:
        lines = [f"- {r}" for r in res["reasons"]]
        for t, v in list(res["failing_new"].items())[:8]:
            lines.append(f"- new test `{t}`:\n```\n{v['detail'][-900:]}\n```")
        for t, v in list(res["regressions"].items())[:8]:
            lines.append(f"- regression `{t}`:\n```\n{v['detail'][-600:]}\n```")
        return "\n".join(lines)

    # ------------------------------------------------------------------ main
    def run(self) -> int:
        start = ORDER.index(self.start)
        for s in ORDER[:start]:
            self.state["stages"][s]["status"] = "skipped"
        if self.round and self.prev:  # carry the previous round's patches forward (ADR-004)
            for name in ("impl.patch", "tests.patch"):
                if self.prev_text(name) and not (self.out / name).exists():
                    (self.out / name).write_text(self.prev_text(name))
        self.publish(f"round {self.round} started at {self.start}")
        try:
            if start <= 0:
                spec = self.requirements()
            else:
                spec = self.prev_text("spec.md")
                self.state["ready_acs"] = [a["id"] for a in checks.check_spec(spec)["acs"]]
                (self.out / "spec.md").write_text(spec)
            if start <= 1:
                fb = self._fmt_findings(self.review_findings("design")) if self.round else ""
                design = self.design(spec, fb)
            else:
                design = self.prev_text("design.md")
                (self.out / "design.md").write_text(design)
            self.build_and_test(spec, design, "implementation" if start <= 2 else "testing",
                                upstream_changed=bool(self.round) and start <= 1)
            self.state["final"] = {"status": "ready", "reason": "stages 1-4 passed"}
            self.save()
            self.publish(f"round {self.round}: stages 1-4 passed, releasing")
            set_output("ready", "true")
            write_summary(f"### Build round {self.round}: stages 1-4 passed")
            return 0
        except Exception as e:  # harness bug or infrastructure failure: still explain and stop (ADR-009)
            if not isinstance(e, Stop):
                import traceback
                cur = next((k for k, v in self.state["stages"].items() if v["status"] == "running"), "requirements")
                e = Stop(cur, f"harness error: {type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}", "harness-error")
            s = e
            if s.stage in self.state["stages"]:
                self.state["stages"][s.stage]["status"] = "stopped"
                self.state["stages"][s.stage].setdefault("reasons", []).insert(0, s.reason)
            self.state["final"] = {"status": "stopped", "stage": s.stage, "kind": s.kind, "reason": s.reason}
            self.save()
            (self.out / "STOPPED.md").write_text(f"# Run stopped at {s.stage}\n\nKind: {s.kind}\n\n{s.reason}\n")
            self.publish(f"stopped at {s.stage} ({s.kind})", f"**Why:** {s.reason[:1500]}")
            set_output("ready", "false")
            write_summary(f"### Build round {self.round} stopped at {s.stage}: {s.kind}\n\n{s.reason}")
            return 1 if s.kind == "harness-error" else 0


def _counts(results: dict) -> dict:
    c: dict[str, int] = {}
    for v in results.values():
        c[v["outcome"]] = c.get(v["outcome"], 0) + 1
    return c


if __name__ == "__main__":
    sys.exit(Pipeline().run())
