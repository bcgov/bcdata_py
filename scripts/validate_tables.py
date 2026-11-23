"""Validate the bundled table metadata (src/bcdata/tables.toml) against the live services.

Checks that every table exists in the WFS, that each primary_key column is present
in the table schema, and that each bcdc_object_name lets the BC Data Catalogue
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

    available = {t.lower() for t in bcdata.list_tables()}
    missing = sorted(set(bcdata.table_metadata) - available)
    for table in missing:
        log.error(f"Table not available via WFS: {table}")

    errors = len(missing)
    for table, fields in bcdata.table_metadata.items():
        if table in missing:
            continue
        schema = bcdata.get_table_definition(table)["schema"]
        columns = {c["column_name"].lower() for c in schema}
        if "bcdc_object_name" in fields and not schema:
            log.error(f"{table}: no schema found via bcdc_object_name {fields['bcdc_object_name']}")
            errors += 1
        if "primary_key" in fields and fields["primary_key"] not in columns:
            log.error(f"{table}: primary_key {fields['primary_key']} not found in table")
            errors += 1

    if errors:
        sys.exit(f"{errors} error(s) found in table metadata")
    log.info(f"Validated metadata for {len(bcdata.table_metadata)} tables")


if __name__ == "__main__":
    main()
