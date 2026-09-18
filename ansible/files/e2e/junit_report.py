#!/usr/bin/env python3
"""Parse JUnit XML and build combined e2e run summaries.

Per-test HTML reports come from the test runners (pytest-html for k0rdent-apis,
make test-functional / junit2html for operator suites). This script only extracts
case counts and writes summary.txt / summary.yml / index.html for the run.

Stdlib only: runs with the CMP's system python3, not the e2e venv.

    junit_report.py counts --junit report.xml --json cases.json
    junit_report.py summary --run-dir /path/<run_id> --run-id <run_id> \
                            --suite <suite> --results results.json
"""

from __future__ import annotations

import argparse
import html
import json
import os
import xml.etree.ElementTree as ET

CASE_STATUSES = ("passed", "failed", "error", "skipped")

INDEX_CSS = """
body { font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
       margin: 2rem; color: #1b1f24; }
h1 { font-size: 1.4rem; margin-bottom: .25rem; }
p.meta { color: #57606a; margin-top: 0; }
table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #d8dee4; }
th { background: #f6f8fa; font-weight: 600; }
tr.failed td, tr.error td { background: #fff5f5; }
td.num { text-align: right; white-space: nowrap; }
span.badge { display: inline-block; min-width: 4.5rem; text-align: center;
             padding: .1rem .5rem; border-radius: 999px; font-size: .8rem;
             font-weight: 600; color: #fff; }
span.passed { background: #1a7f37; }
span.failed, span.error { background: #cf222e; }
"""


def parse_junit(path):
    """Return (cases, error). Missing or broken XML yields an empty case list."""
    if not os.path.isfile(path):
        return [], "no JUnit report at %s" % path
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return [], "unparsable JUnit report: %s" % exc

    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    cases = []
    for suite in suites:
        for case in suite.findall("testcase"):
            status, message = "passed", ""
            for tag, mapped in (("failure", "failed"),
                                ("error", "error"),
                                ("skipped", "skipped")):
                node = case.find(tag)
                if node is not None:
                    status = mapped
                    message = (node.get("message") or node.text or "").strip()
                    break
            try:
                elapsed = float(case.get("time") or 0.0)
            except ValueError:
                elapsed = 0.0
            cases.append({
                "name": case.get("name", ""),
                "classname": case.get("classname", ""),
                "status": status,
                "time": elapsed,
                "message": message,
            })
    return cases, None


def count_cases(cases):
    counts = {status: 0 for status in CASE_STATUSES}
    counts["total"] = len(cases)
    counts["time"] = 0.0
    for case in cases:
        counts[case["status"]] += 1
        counts["time"] += case["time"]
    counts["time"] = round(counts["time"], 2)
    return counts


def case_line(counts):
    return "%d cases: %d passed, %d failed, %d error, %d skipped" % (
        counts["total"], counts["passed"], counts["failed"],
        counts["error"], counts["skipped"],
    )


def badge(status):
    return '<span class="badge %s">%s</span>' % (status, status.upper())


def render_index(run_id, suite, run_dir, entries, totals, case_totals, out_path):
    rows = []
    for entry in entries:
        counts = entry["cases"]
        relative = os.path.relpath(entry["outdir"], run_dir)
        links = []
        for filename in ("report.html", "report.xml", "ginkgo.log", "pytest.log"):
            candidate = os.path.join(entry["outdir"], filename)
            if os.path.isfile(candidate):
                href = html.escape(os.path.join(relative, filename))
                links.append('<a href="%s">%s</a>' % (href, filename))
        css_class = "passed" if entry["status"] == "PASS" else "failed"
        rows.append(
            '<tr class="{css}"><td>{badge}</td><td>{name}</td>'
            '<td class="num">{total}</td><td class="num">{passed}</td>'
            '<td class="num">{failed}</td><td class="num">{skipped}</td>'
            "<td>{links}</td></tr>".format(
                css=css_class,
                badge=badge("passed" if entry["status"] == "PASS" else "failed"),
                name=html.escape(entry["name"]), total=counts["total"],
                passed=counts["passed"],
                failed=counts["failed"] + counts["error"],
                skipped=counts["skipped"], links=" &middot; ".join(links) or "-",
            )
        )

    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>e2e run {run_id}</title><style>{css}</style></head><body>
<h1>e2e run {run_id}</h1>
<p class="meta">suite {suite} &middot; {runs} runs, {runs_passed} passed,
{runs_failed} failed &middot; {cases}</p>
<table><thead><tr><th>Status</th><th>Test</th><th>Cases</th><th>Passed</th>
<th>Failed</th><th>Skipped</th><th>Artifacts</th></tr></thead>
<tbody>{body}</tbody></table>
<p class="meta"><a href="summary.txt">summary.txt</a> &middot;
<a href="summary.yml">summary.yml</a></p>
</body></html>
""".format(run_id=html.escape(run_id), css=INDEX_CSS, suite=html.escape(suite),
           runs=totals["ran"], runs_passed=totals["passed"],
           runs_failed=totals["failed"], cases=html.escape(case_line(case_totals)),
           body="".join(rows))

    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(page)


def yaml_quote(value):
    return json.dumps(value if isinstance(value, str) else str(value))


def write_summary_files(run_id, suite, run_dir, entries, totals, case_totals):
    lines = [
        "e2e run: %s" % run_id,
        "suite: %s" % suite,
        "results: %s/" % run_dir,
        "",
    ]
    width = max([len(entry["name"]) for entry in entries] + [4])
    for entry in entries:
        lines.append("%-4s  %-*s  %s" % (
            entry["status"], width, entry["name"], case_line(entry["cases"]),
        ))
    lines += [
        "",
        "%d runs, %d passed, %d failed" % (
            totals["ran"], totals["passed"], totals["failed"]),
        case_line(case_totals),
    ]
    with open(os.path.join(run_dir, "summary.txt"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    yaml = [
        "run_id: %s" % yaml_quote(run_id),
        "suite: %s" % yaml_quote(suite),
        "results_dir: %s" % yaml_quote(run_dir),
        "ran: %d" % totals["ran"],
        "passed: %d" % totals["passed"],
        "failed: %d" % totals["failed"],
        "cases:",
    ]
    for key in ("total",) + CASE_STATUSES:
        yaml.append("  %s: %d" % (key, case_totals[key]))
    yaml.append("tests:")
    for entry in entries:
        yaml.append("  - name: %s" % yaml_quote(entry["name"]))
        yaml.append("    test: %s" % yaml_quote(entry.get("test", entry["name"])))
        yaml.append("    status: %s" % yaml_quote(entry["status"]))
        yaml.append("    rc: %s" % entry.get("rc", ""))
        yaml.append("    outdir: %s" % yaml_quote(entry["outdir"]))
        if entry.get("label_filter"):
            yaml.append("    label_filter: %s" % yaml_quote(entry["label_filter"]))
        yaml.append("    cases:")
        for key in ("total",) + CASE_STATUSES:
            yaml.append("      %s: %d" % (key, entry["cases"][key]))
    with open(os.path.join(run_dir, "summary.yml"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(yaml) + "\n")


def cmd_counts(args):
    cases, _note = parse_junit(args.junit)
    counts = count_cases(cases)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"counts": counts, "cases": cases}, handle, indent=2)

    print(case_line(counts))
    return 0


def cmd_summary(args):
    with open(args.results, encoding="utf-8") as handle:
        results = json.load(handle)

    entries = []
    for result in results:
        outdir = result["outdir"]
        cached = os.path.join(outdir, "cases.json")
        if os.path.isfile(cached):
            with open(cached, encoding="utf-8") as handle:
                counts = json.load(handle)["counts"]
        else:
            counts = count_cases(parse_junit(os.path.join(outdir, "report.xml"))[0])
        entry = dict(result)
        entry["cases"] = counts
        entries.append(entry)

    totals = {
        "ran": len(entries),
        "passed": sum(1 for e in entries if e["status"] == "PASS"),
        "failed": sum(1 for e in entries if e["status"] != "PASS"),
    }
    case_totals = {key: 0 for key in ("total",) + CASE_STATUSES}
    case_totals["time"] = 0.0
    for entry in entries:
        for key in ("total",) + CASE_STATUSES:
            case_totals[key] += entry["cases"][key]
        case_totals["time"] += entry["cases"].get("time", 0.0)

    write_summary_files(args.run_id, args.suite, args.run_dir,
                        entries, totals, case_totals)
    render_index(args.run_id, args.suite, args.run_dir, entries, totals,
                 case_totals, os.path.join(args.run_dir, "index.html"))
    print(case_line(case_totals))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    counts = sub.add_parser("counts", help="parse JUnit XML into cases.json")
    counts.add_argument("--junit", required=True)
    counts.add_argument("--json", required=True)
    counts.set_defaults(func=cmd_counts)

    summary = sub.add_parser("summary", help="combine a run into summary files")
    summary.add_argument("--run-dir", required=True)
    summary.add_argument("--run-id", required=True)
    summary.add_argument("--suite", default="")
    summary.add_argument("--results", required=True)
    summary.set_defaults(func=cmd_summary)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
