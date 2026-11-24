# data

Table metadata that bcdata maintains itself, because the BC Data Catalogue (BCDC) and the
WFS do not provide it.

## table_metadata.toml

Per-table metadata, bundled with the bcdata package and loaded on import
(`bcdata.table_metadata`). Each entry is a table name with one or more of these fields:

- `primary_key`: column that is unique, never empty, and stable across source refreshes, so it
  is safe to use as a record identifier. Also used to sort paged requests.
- `sort_key`: column that is unique and never empty within one download, but may not be stable
  across refreshes. Used only to sort paged requests.
- `bcdc_object_name`: the table's object name in BCDC, where it differs from the WFS name.

The comments at the top of the file give the full rules. After editing, check the entries
against the live services:

    python scripts/validate_tables.py

This also runs on pull requests that change the file, and weekly.

`src/bcdata/table_metadata.toml` is a symlink to this file, so that it is included in the
package. Edit the file here.

## primary_keys.json (deprecated)

The `{table: primary_key}` lookup used by bcdata v0.18.0 and earlier. Those versions download
it from this path on the `main` branch every time `bcdata` is imported, and the import fails
if the download fails. **Do not move, rename or delete this file**, or installs of older
versions will break.

The file is frozen: bcdata no longer reads it, and new keys go in `table_metadata.toml` only.
The one exception is a key found to be wrong. Remove it here too, because older versions
use these keys as the primary key when loading tables with `bc2pg`, and a non-unique key
makes the load fail.
