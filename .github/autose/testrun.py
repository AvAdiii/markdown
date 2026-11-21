"""Run unittest and write {test_id: outcome} as JSON. Usage:
    python testrun.py OUT.json [--start tests] [--files tests/a.py tests/b.py]
Outcomes: pass, fail, error, skip. A module that fails to import is reported as error with id
'<module>::import'."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


class Recorder(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.outcomes: dict[str, dict] = {}

    def _rec(self, test, outcome, detail=""):
        self.outcomes[test.id()] = {"outcome": outcome, "detail": detail[-1500:]}

    def addSuccess(self, test):
        super().addSuccess(test)
        self._rec(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._rec(test, "fail", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._rec(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._rec(test, "skip", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._rec(test, "pass")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._rec(test, "fail", "unexpected success")


def main() -> int:
    import os
    sys.path.insert(0, os.getcwd())  # the repository root, so that 'tests.*' modules import
    out = Path(sys.argv[1])
    args = sys.argv[2:]
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    if "--files" in args:
        files = args[args.index("--files") + 1:]
        for f in files:
            mod = f[:-3].replace("/", ".") if f.endswith(".py") else f
            try:
                suite.addTests(loader.loadTestsFromName(mod))
            except Exception as e:  # import or syntax error in the test module
                suite.addTest(_ImportError(mod, e))
    else:
        start = args[args.index("--start") + 1] if "--start" in args else "tests"
        suite = loader.discover(start, top_level_dir=".")
    runner = unittest.TextTestRunner(resultclass=Recorder, verbosity=0, stream=open("/dev/null", "w"))
    res = runner.run(suite)
    outcomes = res.outcomes
    for test, err in res.errors:  # loader errors (_FailedTest) when discovery hits a broken module
        outcomes.setdefault(test.id(), {"outcome": "error", "detail": err[-1500:]})
    out.write_text(json.dumps(outcomes, indent=1, sort_keys=True))
    counts = {}
    for v in outcomes.values():
        counts[v["outcome"]] = counts.get(v["outcome"], 0) + 1
    print(json.dumps(counts))
    return 0


class _ImportError(unittest.TestCase):
    def __init__(self, mod, exc):
        super().__init__("runTest")
        self._mod, self._exc = mod, exc

    def id(self):
        return f"{self._mod}::import"

    def runTest(self):
        raise self._exc


if __name__ == "__main__":
    sys.exit(main())
