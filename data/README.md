# data

BC Data Catalogue (BCDC) and WFS do not identify primary keys, and the two services sometimes
return different names for the same dataset. Metadata files in this folder are maintained to
fix these issues.

## table_metadata.toml

Per-table metadata, bundled with the bcdata package and loaded on import
(`bcdata.table_metadata`). Each entry is a table name with one or more of these fields:

- `primary_key`: column that is unique, never empty, and stable across source refreshes, and
  therefore safe to use as a record identifier. Also used to sort paged requests.
- `sort_key`: column that is unique and never empty within one download, but may not be stable
  across refreshes. Used only to sort paged requests.
- `bcdc_object_name`: the table's object name in BCDC, where it differs from the WFS name.

The comments at the top of the file give the full rules. After editing, check the entries
against the live services:

    python scripts/validate_tables.py

This script is run on pull requests that change the file, plus on a scheduled basis to check
for changes to the services.

Note that `src/bcdata/table_metadata.toml` is a symlink to this file so that the file gets
included in the package. Edit the file here.

## primary_keys.json (deprecated)

The `{table: primary_key}` lookup used by bcdata v0.10.0 to v0.18.0, which download it from
this path on the `main` branch:

- v0.12.1 to v0.18.0 download it every time `bcdata` is imported, and the import fails if the
  download fails.
- v0.10.0 to v0.11.0 download it only when running `bc2pg`. If the download fails, they log a
  warning and load the table without a primary key.

**Do not move, rename or delete this file**, or installs of older versions will break.

All of these versions download it from the repository's old location,
`https://raw.githubusercontent.com/smnorris/bcdata/main/data/primary_keys.json`, which works
only because GitHub redirects the old repository name to this one.

If the redirect stops working or if `data/primary_keys.json` needs to be removed from main,
a repo containing only `data/primary_keys.json` on `main` (plus a README description linking
here) could be added. Old installs would then fetch that file from that repo directly.

Default to just retaining the file while the redirect works - all old issue / PR / pypi links
are also redirected. It isn't worth breaking these linkages unless necessary.
