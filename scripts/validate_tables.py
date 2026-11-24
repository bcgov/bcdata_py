"""Validate the bundled table metadata (data/table_metadata.toml) against the live services.

Checks that every table exists in the WFS, that each primary_key and sort_key column
is present in the WFS table schema, and that each bcdc_object_name lets the BC Data Catalogue
search find the table's schema. Column values are not checked for uniqueness.

    python scripts/validate_tables.py [--report report.md]

Exit codes:
    0  table metadata is valid
    3  problems found in table metadata (listed in the log, and in --report if given)
    other  the check itself failed (eg the services could not be reached)
"""

import argparse
import logging
import sys

import bcdata

LOG_FORMAT = "%(asctime)s:%(levelname)s:%(name)s: %(message)s"

# distinct from the exit code of an unhandled exception (1), so CI can tell
# "metadata needs fixing" apart from "the check could not run"
EXIT_PROBLEMS_FOUND = 3


def find_problems():
    """Return a list of (table, problem description) for invalid table metadata"""
    WFS = bcdata.wfs.BCWFS()
    available = {t.lower() for t in WFS.list_tables()}
    problems = []
    for table, fields in sorted(bcdata.table_metadata.items()):
        if table not in available:
            problems.append((table, "table not available via WFS"))
            continue
        # check key columns against the WFS schema, which is what paged requests use
        columns = {c.lower() for c in WFS.get_schema(table.upper())["properties"]}
        for key in ("primary_key", "sort_key"):
            if key in fields and fields[key] not in columns:
                problems.append((table, f"{key} `{fields[key]}` not found in table"))
        if "bcdc_object_name" in fields and not bcdata.get_table_definition(table)["schema"]:
            problems.append(
                (
                    table,
                    f"no catalogue schema found via bcdc_object_name `{fields['bcdc_object_name']}`",
                )
            )
    return problems


def write_report(problems, path):
    """Write problems as a markdown report (used as the body of the CI issue)"""
    lines = [
        (
            f"`scripts/validate_tables.py` found {len(problems)} problem(s) in "
            "`data/table_metadata.toml`:"
        ),
        "",
        "| Table | Problem |",
        "|---|---|",
    ]
    lines += [f"| `{table}` | {problem} |" for table, problem in problems]
    lines += [
        "",
        (
            "Update or remove the affected entries in `data/table_metadata.toml`, then confirm "
            "with `python scripts/validate_tables.py`. This issue is closed automatically when "
            "validation passes."
        ),
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", help="write any problems found to this markdown file")
    args = parser.parse_args()

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format=LOG_FORMAT)
    log = logging.getLogger(__name__)

    problems = find_problems()
    for table, problem in problems:
        log.error(f"{table}: {problem}")
    if problems:
        if args.report:
            write_report(problems, args.report)
        log.error(f"{len(problems)} problem(s) found in table metadata")
        sys.exit(EXIT_PROBLEMS_FOUND)
    log.info(f"Validated metadata for {len(bcdata.table_metadata)} tables")


if __name__ == "__main__":
    main()
