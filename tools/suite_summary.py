"""Summarize pytest JUnit XML without coupling to pytest plugin internals.

JUnit supplies actual outcomes, including import skips and errors. This stdlib
post-processor has its own exit status; the caller must preserve pytest's status.
"""

import argparse
from pathlib import PurePosixPath
import sys
import xml.etree.ElementTree as ET


def summarize(root):
    """Return (declared, executed, skipped), counting only modules with cases."""
    modules = {}
    for case in root.iter("testcase"):
        if case.get("file"):
            parts = list(PurePosixPath(case.get("file")).with_suffix("").parts)
        else:
            # Import skips / collection errors may put the module in name only.
            name = case.get("classname") or case.get("name", "")
            parts = name.removesuffix(".py").replace("/", ".").split(".")
        # Class-based tests append class names; stop at the test module itself.
        end = next((i for i, part in enumerate(parts) if part.startswith("test_")), None)
        if end is None:
            raise ValueError("testcase has no test module in file or classname")
        start = parts.index("tests") if "tests" in parts[:end] else 0
        module = ".".join(parts[start:end + 1])
        executed = case.find("skipped") is None
        modules[module] = modules.get(module, False) or executed
    executed = sum(modules.values())
    return len(modules), executed, len(modules) - executed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", help="pytest JUnit XML report")
    args = parser.parse_args(argv)
    try:
        declared, executed, skipped = summarize(ET.parse(args.xml).getroot())
    except (OSError, ET.ParseError, ValueError) as error:
        print(f"suite_summary: {error}", file=sys.stderr)
        return 2
    print(f"suites: declared={declared} executed={executed} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
