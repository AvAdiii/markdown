"""Deterministic stage checks (ADR-007). Each check returns a dict with ok, reasons and data.
No model is called here; the stage-1 judge verdict is passed in by the orchestrator."""
from __future__ import annotations

import ast
import fnmatch
import json
import re
import sys
from pathlib import Path

from common import CONFIG, HARNESS, sh

AC_ID = r"AC-(?:FR|NFR-[A-Z]+)-\d{3}-\d+"
READY_TAGS = {"STATED", "CONFIRMED", "DERIVED", "REPO"}
# A1 skill, section 3.5: weak words and the seed lexicon of unmeasured quality terms
WEAK = ["may", "might", "could", "possibly", "as appropriate", "if possible", "etc", "and/or", "tbd",
        "user-friendly", "flexible", "fast", "quick", "efficient", "scalable", "robust", "secure", "reliable",
        "intuitive", "seamless", "high-performance", "easy", "modern", "best", "minimal", "sufficient",
        "adequate", "appropriate", "properly", "correctly", "nicely", "reasonable"]


# ------------------------------------------------------------------------------------------ stage 1

def parse_records(text: str) -> dict[str, dict]:
    """Parse A1-format records: '### ID: title' followed by '- Key: value' lines."""
    records: dict[str, dict] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^###\s+([A-Z]+(?:-[A-Z]+)?-\d{3})\s*:?\s*(.*)$", line.strip())
        if m:
            cur = {"id": m.group(1), "title": m.group(2), "fields": {}, "acceptance": [], "dup": m.group(1) in records}
            records[m.group(1) + ("#dup" if cur["dup"] else "")] = cur
            continue
        if line.startswith("## "):
            cur = None
            continue
        if cur is None:
            continue
        fm = re.match(r"^\s*-\s*([A-Za-z ]+):\s*(.*)$", line)
        if fm:
            key = fm.group(1).strip().lower()
            if key == "acceptance":
                cur["acceptance"].append(fm.group(2).strip())
            else:
                cur["fields"][key] = fm.group(2).strip()
        elif cur["acceptance"] and line.startswith("  ") and line.strip():
            cur["acceptance"][-1] += " " + line.strip()  # wrapped acceptance line
    return records


def _strip_code(s: str) -> str:
    return re.sub(r"`[^`]*`", " ", s)


def weak_words(s: str) -> list[str]:
    body = _strip_code(s).lower()
    found = []
    for w in WEAK:
        if re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", body):
            found.append(w)
    return found


def check_spec(text: str) -> dict:
    reasons: list[str] = []
    recs = parse_records(text)
    dups = [r["id"] for r in recs.values() if r["dup"]]
    if dups:
        reasons.append(f"duplicate record ids: {', '.join(dups)}")
    reqs = [r for r in recs.values() if re.match(r"^(FR|NFR-[A-Z]+)-\d{3}$", r["id"])]
    if not reqs:
        reasons.append("no FR or NFR records found (expected '### FR-001: title' records)")
    ready, blocked, acs, must_ready = [], [], [], []
    seen_ac: set[str] = set()
    for r in reqs:
        status = r["fields"].get("status", "").upper()
        tag = r["fields"].get("tag", "").split()[0].upper() if r["fields"].get("tag") else ""
        prio = r["fields"].get("priority", "")
        if status.startswith("READY"):
            ready.append(r["id"])
            if tag not in READY_TAGS:
                reasons.append(f"{r['id']}: READY but tag is {tag or 'missing'} (READY needs one of {sorted(READY_TAGS)})")
            if not r["acceptance"]:
                reasons.append(f"{r['id']}: READY but has no acceptance criterion")
            if prio.lower().startswith("must"):
                must_ready.append(r["id"])
            for a in r["acceptance"]:
                m = re.match(rf"^({AC_ID})\s*:\s*(.*)$", a)
                if not m:
                    reasons.append(f"{r['id']}: acceptance line without a valid AC id: '{a[:80]}'")
                    continue
                ac_id, body = m.group(1), m.group(2)
                if ac_id in seen_ac:
                    reasons.append(f"{ac_id}: duplicate acceptance id")
                seen_ac.add(ac_id)
                if not ac_id.startswith("AC-" + r["id"] + "-"):
                    reasons.append(f"{ac_id}: id does not match its requirement {r['id']}")
                if not re.search(r"\bgiven\b.+\bwhen\b.+\bthen\b", body, re.I | re.S):
                    reasons.append(f"{ac_id}: not in Given/When/Then form")
                ww = weak_words(body)
                if ww:
                    reasons.append(f"{ac_id}: weak or unmeasured words {ww} (A1 rule 3.5)")
                acs.append({"id": ac_id, "req": r["id"], "text": body})
        else:
            blocked.append({"id": r["id"], "status": r["fields"].get("status", "")})
    questions = []
    for r in recs.values():
        if r["id"].startswith("Q-"):
            questions.append({"id": r["id"], "title": r["title"],
                              "question": r["fields"].get("question", ""),
                              "tier": r["fields"].get("tier", ""), "options": r["fields"].get("options", ""),
                              "status": r["fields"].get("status", ""), "blocks": r["fields"].get("blocks", "")})
    open_p1 = [q for q in questions if q["tier"].upper().startswith("P1") and q["status"].lower().startswith("open")]
    needs_clarification = not must_ready and bool(open_p1)
    if not must_ready and not needs_clarification:
        reasons.append("no Must requirement is READY")
    return {"ok": not reasons and not needs_clarification, "reasons": reasons,
            "needs_clarification": needs_clarification, "ready": ready, "blocked": blocked,
            "acs": acs, "must_ready": must_ready, "questions": questions, "open_p1": open_p1}


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"criteria": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "testable": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["id", "testable", "reason"]}}},
    "required": ["criteria"],
}


# ------------------------------------------------------------------------------------------ stage 2

def parse_change_table(text: str) -> list[dict]:
    rows = []
    in_section = False
    header = None
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip().lower().startswith("## changes")
            header = None
            continue
        if not in_section or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        row = dict(zip(header, cells))
        rows.append({
            "file": row.get("file", "").strip("` "),
            "symbol": row.get("symbol", "").strip("` "),
            "change": row.get("change", "").lower().strip("` "),
            "acs": re.findall(AC_ID, row.get("acs", "")),
        })
    return rows


def symbol_exists(path: Path, symbol: str) -> bool:
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return False
    parts = symbol.replace("()", "").split(".")
    scope = tree.body
    for i, part in enumerate(parts):
        found = None
        for node in scope:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == part:
                found = node
                break
            if isinstance(node, ast.Assign) and i == len(parts) - 1 and any(
                    isinstance(t, ast.Name) and t.id == part for t in node.targets):
                found = node
                break
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == part:
                found = node
                break
        if found is None:
            return False
        scope = getattr(found, "body", [])
    return True


def check_design(text: str, ws: Path, ready_acs: list[str]) -> dict:
    reasons: list[str] = []
    rows = parse_change_table(text)
    if not rows:
        reasons.append("no '## Changes' table with columns File | Symbol | Change | ACs")
    named_symbol = False
    for r in rows:
        f, sym, ch = r["file"], r["symbol"], r["change"]
        if ch not in ("modify", "add", "delete"):
            reasons.append(f"{f}: change must be modify, add or delete (got '{ch}')")
            continue
        p = ws / f
        if ch in ("modify", "delete") and not p.exists():
            reasons.append(f"{f}: listed as {ch} but the file does not exist")
            continue
        if ch == "add" and p.exists():
            reasons.append(f"{f}: listed as add but the file already exists")
        allowed = CONFIG["developer_write_globs"] + CONFIG["tester_write_globs"]
        if not any(fnmatch.fnmatch(f, g) for g in allowed):
            reasons.append(f"{f}: outside the paths the pipeline may change ({allowed})")
        if f.endswith(".py") and sym and sym not in ("-", "—", "n/a"):
            named_symbol = True
            if ch == "modify":
                for s in [x.strip() for x in sym.split(",")]:
                    if s.startswith("new ") or s.endswith("(new)"):
                        continue
                    if not symbol_exists(p, s):
                        reasons.append(f"{f}: symbol '{s}' not found (use Class.method, function or Class; "
                                       f"prefix new symbols with 'new ')")
    if rows and not named_symbol:
        reasons.append("the plan names no Python function or class to change")
    covered = {a for r in rows for a in r["acs"]}
    missing = [a for a in ready_acs if a not in covered]
    if missing:
        reasons.append(f"READY acceptance criteria not mapped to any change: {', '.join(missing)}")
    if any(r["file"].startswith("markdown/") for r in rows) and not any(r["file"] == "docs/changelog.md" for r in rows):
        reasons.append("markdown/ changes but no docs/changelog.md row (the repo's changelog-enforcer CI requires it)")
    if "## interface" not in text.lower():
        reasons.append("no '## Interface' section (the tester writes tests from it)")
    return {"ok": not reasons, "reasons": reasons, "rows": rows}


# ------------------------------------------------------------------------------------------ stage 3

def changed_files(ws: Path) -> list[str]:
    out = sh(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ws).stdout
    files = []
    for line in out.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ")[1]
        files.append(path.strip('"'))
    return files


def check_paths(files: list[str], globs: list[str]) -> list[str]:
    return [f for f in files if not any(fnmatch.fnmatch(f, g) for g in globs)]


def check_build(ws: Path, python: str) -> dict:
    reasons: list[str] = []
    files = changed_files(ws)
    if not files:
        reasons.append("the implementation is empty (no file changed)")
    bad = check_paths(files, CONFIG["developer_write_globs"])
    if bad:
        reasons.append(f"files outside the developer's paths: {bad}")
    steps = [
        ("compileall", [python, "-m", "compileall", "-q", "markdown"]),
        ("install", [python, "-m", "pip", "install", "-q", "--no-deps", "-e", "."]),
        ("import", [python, "-c", "import markdown, markdown.extensions; "
                                  "print(markdown.markdown('*x* [a](b)', extensions=['extra', 'toc']))"]),
    ]
    log_lines = []
    for name, cmd in steps:
        res = sh(cmd, cwd=ws, check=False, timeout=300)
        log_lines.append(f"$ {' '.join(cmd)}\nexit {res.returncode}\n{res.stdout[-800:]}{res.stderr[-1500:]}")
        if res.returncode != 0:
            reasons.append(f"{name} failed: {(res.stderr or res.stdout)[-600:]}")
            break
    py_changed = [f for f in files if f.endswith(".py") and (ws / f).exists()]
    if py_changed and not reasons:
        res = sh([python, "-m", "flake8", *py_changed], cwd=ws, check=False, timeout=300)
        log_lines.append(f"$ flake8 {' '.join(py_changed)}\nexit {res.returncode}\n{res.stdout[-1500:]}")
        if res.returncode != 0:
            reasons.append(f"flake8 (the repo's CI lint) failed:\n{res.stdout[-1200:]}")
    return {"ok": not reasons, "reasons": reasons, "files": files, "log": "\n".join(log_lines)}


# ------------------------------------------------------------------------------------------ stage 4

def run_tests(ws: Path, python: str, out: Path, files: list[str] | None = None) -> dict:
    cmd = [python, str(HARNESS / "testrun.py"), str(out)]
    cmd += ["--files", *files] if files else ["--start", CONFIG["test_runner_start"]]
    res = sh(cmd, cwd=ws, check=False, timeout=900)
    try:
        return json.loads(out.read_text())
    except Exception:
        return {"<runner>::crash": {"outcome": "error", "detail": (res.stdout + res.stderr)[-2000:]}}


def test_files_in(files: list[str]) -> list[str]:
    return [f for f in files if f.startswith("tests/") and f.endswith(".py") and Path(f).name.startswith("test")]


def check_tests(*, new_ids_head: dict, base_results: dict, head_results: dict, baseline: dict,
                ready_acs: list[str], test_source: str) -> dict:
    """new_ids_head: outcomes of the new tests on head. base_results: outcomes of the same tests on the
    base commit. head_results: full suite on head. baseline: full suite on base before any change."""
    reasons: list[str] = []
    owner = None  # who has to fix it: 'tester' or 'developer'
    norm_src = test_source.lower().replace("-", "_")
    uncovered = [a for a in ready_acs if a.lower().replace("-", "_") not in norm_src]
    if not new_ids_head:
        reasons.append("no new test was added")
        owner = "tester"
    if uncovered:
        reasons.append(f"acceptance criteria without a test that names them: {', '.join(uncovered)}")
        owner = "tester"
    import_errors = [t for t, v in new_ids_head.items() if t.endswith("::import")]
    if import_errors:
        reasons.append(f"test modules fail to import on head: {import_errors}: "
                       f"{new_ids_head[import_errors[0]]['detail'][-500:]}")
        owner = owner or "tester"
    if new_ids_head and not any(v["outcome"] in ("fail", "error") for v in base_results.values()):
        reasons.append("every new test also passes on the base commit, so none of them detects the change")
        owner = owner or "tester"
    failing_new = {t: v for t, v in new_ids_head.items() if v["outcome"] in ("fail", "error") and not t.endswith("::import")}
    regressions = {t: v for t, v in head_results.items()
                   if baseline.get(t, {}).get("outcome") == "pass" and v["outcome"] in ("fail", "error")}
    vanished = [t for t, v in baseline.items() if v["outcome"] == "pass" and t not in head_results]
    if failing_new:
        reasons.append(f"{len(failing_new)} new test(s) fail on the implementation")
        owner = owner or "developer"
    if regressions:
        reasons.append(f"{len(regressions)} existing test(s) now fail (regressions)")
        owner = owner or "developer"
    if vanished:
        reasons.append(f"{len(vanished)} existing test(s) disappeared: {vanished[:5]}")
        owner = owner or "tester"
    return {"ok": not reasons, "reasons": reasons, "owner": owner, "failing_new": failing_new,
            "regressions": regressions, "uncovered": uncovered}


if __name__ == "__main__":
    # local helper: python checks.py spec FILE | design FILE WS
    kind = sys.argv[1]
    if kind == "spec":
        print(json.dumps(check_spec(Path(sys.argv[2]).read_text()), indent=1))
    elif kind == "design":
        print(json.dumps(check_design(Path(sys.argv[2]).read_text(), Path(sys.argv[3]), sys.argv[4:]), indent=1))
