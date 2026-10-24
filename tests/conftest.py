"""On GitHub Actions, turn every failure (setup, test body or teardown) into an annotation.

Job logs aren't always easy to reach; annotations show up on the run page and via the API.
"""
import os

_IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def pytest_runtest_logreport(report):
    if _IN_CI and report.failed:
        detail = str(report.longrepr)[-3000:]
        print(f"\n::error title={_escape(report.nodeid)} ({report.when})::{_escape(detail)}", flush=True)


def pytest_terminal_summary(terminalreporter, exitstatus):
    if _IN_CI:
        stats = {k: len(v) for k, v in terminalreporter.stats.items() if k}
        print(f"\n::notice title=pytest summary::exit={exitstatus} {stats}", flush=True)
