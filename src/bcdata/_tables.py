"""Load the table metadata file bundled with bcdata (tables.toml)."""

import tomllib
from importlib.resources import files

# fields a table entry may have - see the comments at the top of tables.toml
FIELDS = {"primary_key", "bcdc_object_name"}


def load_table_metadata(text=None):
    """Parse and validate table metadata, returning {lowercase table name: {field: value}}.

    Reads the bundled tables.toml unless toml text is provided.
    """
    if text is None:
        text = files("bcdata").joinpath("tables.toml").read_text(encoding="utf-8")
    tables = tomllib.loads(text).get("tables", {})
    for table, fields in tables.items():
        if table != table.lower():
            raise ValueError(f"Table names in table metadata must be lowercase: {table}")
        unknown = set(fields) - FIELDS
        if unknown:
            raise ValueError(f"Unknown field(s) for {table} in table metadata: {sorted(unknown)}")
    return tables
