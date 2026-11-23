"""Validate the bundled table metadata (src/bcdata/tables.toml) against the live services.

Checks that every table exists in the WFS, that each primary_key and sort_key column
is present in the WFS table schema, and that each bcdc_object_name lets the BC Data Catalogue
search find the table's schema. Column values are not checked for uniqueness.

    python scripts/validate_tables.py
"""

import logging
import sys

import bcdata

LOG_FORMAT = "%(asctime)s:%(levelname)s:%(name)s: %(message)s"


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format=LOG_FORMAT)
    log = logging.getLogger(__name__)

    WFS = bcdata.wfs.BCWFS()
    available = {t.lower() for t in WFS.list_tables()}
    missing = sorted(set(bcdata.table_metadata) - available)
    for table in missing:
        log.error(f"Table not available via WFS: {table}")

    errors = len(missing)
    for table, fields in bcdata.table_metadata.items():
        if table in missing:
            continue
        # check key columns against the WFS schema, which is what paged requests use
        columns = {c.lower() for c in WFS.get_schema(table.upper())["properties"]}
        if "bcdc_object_name" in fields and not bcdata.get_table_definition(table)["schema"]:
            log.error(f"{table}: no schema found via bcdc_object_name {fields['bcdc_object_name']}")
            errors += 1
        for key in ("primary_key", "sort_key"):
            if key in fields and fields[key] not in columns:
                log.error(f"{table}: {key} {fields[key]} not found in table")
                errors += 1

    if errors:
        sys.exit(f"{errors} error(s) found in table metadata")
    log.info(f"Validated metadata for {len(bcdata.table_metadata)} tables")


if __name__ == "__main__":
    main()
