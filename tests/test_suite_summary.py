"""Module accounting uses JUnit outcomes, not test counts or static lists."""

from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

import pytest

from tools.suite_summary import summarize


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "suite_summary.py"


@pytest.mark.parametrize(("cases", "expected"), [
    ('''<testcase classname="tests.test_a.TestOne"/>
        <testcase classname="tests.test_a.TestTwo"><skipped/></testcase>
        <testcase classname="tests.test_b"><skipped/></testcase>
        <testcase classname="tests.test_c"><error/></testcase>''', (3, 2, 1)),
    ('''<testcase classname="tests.test_a"><skipped/></testcase>
        <testcase classname="tests.test_a"><skipped/></testcase>''', (1, 0, 1)),
    ('''<testcase classname="tests.test_a"><failure/></testcase>
        <testcase classname="tests.test_a"><skipped/></testcase>''', (1, 1, 0)),
    ('<testcase classname="tests.test_import"><skipped type="pytest.skip"/></testcase>', (1, 0, 1)),
    ('<testcase classname="" name="tests.test_import"><skipped/></testcase>', (1, 0, 1)),
    ('<testcase classname="" name="tests/test_import.py"><error/></testcase>', (1, 1, 0)),
    ('''<testcase file="tests/test_a.py" classname="ignored"><skipped/></testcase>
        <testcase classname="tests.test_a.TestClass"/>''', (1, 1, 0)),
    ('<testsuite name="empty"/>', (0, 0, 0)),
])
def test_aggregation(cases, expected):
    result = summarize(ET.fromstring(f"<testsuites><testsuite>{cases}</testsuite></testsuites>"))
    assert result == expected
    assert result[0] == result[1] + result[2]


def run_summary(*args):
    return subprocess.run(
        ["python3", "-B", str(SCRIPT), *map(str, args)],
        capture_output=True, text=True, check=False,
    )


def test_printed_line(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text('<testsuite><testcase classname="tests.test_a"/></testsuite>')
    result = run_summary(report)
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == "suites: declared=1 executed=1 skipped=0\n"
    assert re.fullmatch(r"suites: declared=\d+ executed=\d+ skipped=\d+", result.stdout.rstrip("\n"))


@pytest.mark.parametrize("content", [None, "<broken"])
def test_input_error(tmp_path, content):
    report = tmp_path / "report.xml"
    if content is not None:
        report.write_text(content)
    result = run_summary(report)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "suite_summary:" in result.stderr


def test_usage_error():
    assert run_summary().returncode == 2
