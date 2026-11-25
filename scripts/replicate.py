"""Replicate the tables listed in data/replication.toml to a GeoParquet mirror.

For each table that is due (per its schedule and the last check recorded in the
destination's state.json):

1. download the whole table with bcdata.to_parquet to a temp file
2. compare its content_hash with the file at the destination (footer only)
3. unchanged: record the check in state.json
4. changed (or not yet replicated): diff the previous and new files with
   fit_changedetector, write a change record, upload the new file, then record
   the check in state.json

    python scripts/replicate.py --dest s3://bucket/prefix [--tables a,b] [--force]

Every change record has counts and the keys of the inserted, deleted and updated
records (change.json). The changed records themselves (changes.parquet) are kept
only for tables with keep_change_records = true.

The destination is --dest, or the BCDATA_REPLICATION_DEST environment variable. It
can be any URI pyarrow understands (s3://..., or a local folder for testing).
Credentials and the S3 endpoint come from the standard AWS environment variables
(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL...).

Destination layout:

    <dest>/<table>.parquet                       latest copy of each table
    <dest>/changes/<table>/<timestamp>/change.json   summary of one change: counts
                                                  and keys of changed records
    <dest>/changes/<table>/<timestamp>/changes.parquet  with keep_change_records:
                                                  changed records as full rows,
                                                  plus _change_type and
                                                  _commit_timestamp (as in a Delta
                                                  Lake change data feed)
    <dest>/state.json                            last check of each table

Mirror files are written by bcdata.to_parquet unchanged. Keys generated for tables
without a primary key (hash_columns) exist only in the diff and the change records.

Exit codes:
    0  every due table was checked
    1  one or more tables failed (listed in the log, and in --report if given)
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import geopandas
import pandas
import pyarrow
import pyarrow.parquet as pq
from pyarrow import fs

import bcdata
from bcdata.parquet import DownloadCheckError

LOG_FORMAT = "%(asctime)s:%(levelname)s:%(name)s: %(message)s"
log = logging.getLogger("replicate")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "replication.toml"
DEST_ENV = "BCDATA_REPLICATION_DEST"

SCHEDULES = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
    "quarterly": timedelta(days=91),
}
# scheduled runs start at about the same time each day, so a table checked a little
# after the start of one run is still due at the start of the run one interval later
SCHEDULE_TOLERANCE = timedelta(hours=12)

FIELDS = {"schedule", "keep_change_records", "hash_columns", "ignore_columns", "allow_duplicates"}
GEOMETRY_COLUMN = "geometry"
# name of the generated key column, in the diff and change records only
HASH_KEY = "_bcdata_hash_key"


class ConfigError(ValueError):
    """replication.toml is invalid"""


class DuplicateKeyError(ValueError):
    """A generated key is not unique, and the table does not allow duplicates"""


@dataclass(frozen=True)
class KeyConfig:
    """How records of a table are matched between files"""

    column: str  # key column: the primary key, or HASH_KEY when generated
    hash_columns: list | None = None  # columns hashed to generate the key
    ignore_columns: list = field(default_factory=list)  # left out of the comparison
    allow_duplicates: bool = False  # generated keys only


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _check_columns(table, name, columns):
    if (
        not isinstance(columns, list)
        or not columns
        or not all(isinstance(c, str) and c.strip() for c in columns)
    ):
        raise ConfigError(f"{table}: {name} must be a list of column names")
    if any(c != c.lower() for c in columns):
        raise ConfigError(f"{table}: {name} must be lowercase")
    if len(set(columns)) != len(columns):
        raise ConfigError(f"{table}: {name} has duplicates")


def load_config(text=None, table_metadata=None):
    """Parse and validate replication config, returning {table: {field: value}}.

    Reads data/replication.toml unless toml text is provided. Keys come from
    table_metadata (default: bcdata.table_metadata): each table needs either a
    primary_key there or hash_columns here, not both.
    """
    if text is None:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    if table_metadata is None:
        table_metadata = bcdata.table_metadata
    tables = tomllib.loads(text)
    for table, fields in tables.items():
        if table != table.lower():
            raise ConfigError(f"Table names must be lowercase: {table}")
        if not isinstance(fields, dict):
            raise ConfigError(f'{table} must be a table of fields, eg ["{table}"]')
        unknown = set(fields) - FIELDS
        if unknown:
            raise ConfigError(f"Unknown field(s) for {table}: {sorted(unknown)}")
        if fields.get("schedule") not in SCHEDULES:
            raise ConfigError(
                f"{table}: schedule must be one of {list(SCHEDULES)}, "
                f"got {fields.get('schedule')!r}"
            )
        if not isinstance(fields.get("keep_change_records", False), bool):
            raise ConfigError(f"{table}: keep_change_records must be true or false")
        primary_key = table_metadata.get(table, {}).get("primary_key")
        hash_columns = fields.get("hash_columns")
        if hash_columns is not None:
            if primary_key:
                raise ConfigError(
                    f"{table}: has primary_key {primary_key} in table_metadata.toml, "
                    "remove hash_columns"
                )
            _check_columns(table, "hash_columns", hash_columns)
        elif not primary_key:
            raise ConfigError(
                f"{table}: no primary_key in table_metadata.toml, add hash_columns "
                "to generate a key for change detection"
            )
        ignore_columns = fields.get("ignore_columns")
        if ignore_columns is not None:
            _check_columns(table, "ignore_columns", ignore_columns)
            if primary_key in ignore_columns:
                raise ConfigError(f"{table}: ignore_columns can't include the primary key")
            both = sorted(set(ignore_columns) & set(hash_columns or []))
            if both:
                raise ConfigError(
                    f"{table}: columns in both hash_columns and ignore_columns: {both}"
                )
            if GEOMETRY_COLUMN in ignore_columns:
                raise ConfigError(f"{table}: ignore_columns can't include the geometry")
        if "allow_duplicates" in fields:
            if not isinstance(fields["allow_duplicates"], bool):
                raise ConfigError(f"{table}: allow_duplicates must be true or false")
            if hash_columns is None:
                # a duplicated primary key already fails the download
                raise ConfigError(f"{table}: allow_duplicates applies only with hash_columns")
    return tables


def key_config(table, fields, table_metadata=None):
    """Return the KeyConfig of a configured table"""
    if table_metadata is None:
        table_metadata = bcdata.table_metadata
    ignore_columns = list(fields.get("ignore_columns", []))
    if fields.get("hash_columns"):
        return KeyConfig(
            HASH_KEY,
            list(fields["hash_columns"]),
            ignore_columns,
            fields.get("allow_duplicates", False),
        )
    return KeyConfig(table_metadata[table]["primary_key"], None, ignore_columns)


def is_due(fields, state_entry, now):
    """True if the table's last successful check is older than its schedule"""
    if not state_entry or not state_entry.get("checked"):
        return True
    checked = datetime.fromisoformat(state_entry["checked"])
    return now >= checked + SCHEDULES[fields["schedule"]] - SCHEDULE_TOLERANCE


# ---------------------------------------------------------------------------
# destination
# ---------------------------------------------------------------------------


class Destination:
    """The mirror location: any pyarrow filesystem URI (s3://..., or a local folder)"""

    def __init__(self, uri):
        self.uri = uri.rstrip("/")
        self.fs, self.root = fs.FileSystem.from_uri(self.uri)
        self.root = self.root.rstrip("/")

    def path(self, *parts):
        return "/".join([self.root, *parts])

    def url(self, *parts):
        return "/".join([self.uri, *parts])

    def exists(self, *parts):
        return self.fs.get_file_info(self.path(*parts)).type == fs.FileType.File

    def read_bcdata_metadata(self, *parts):
        """bcdata footer metadata of a parquet file, or None if there is no file"""
        if not self.exists(*parts):
            return None
        with self.fs.open_input_file(self.path(*parts)) as f:
            return read_bcdata_metadata(f)

    def download(self, local_path, *parts):
        fs.copy_files(self.path(*parts), str(local_path), source_filesystem=self.fs)

    def upload(self, local_path, *parts):
        path = self.path(*parts)
        if self.fs.type_name == "local":
            self.fs.create_dir(path.rsplit("/", 1)[0], recursive=True)
        fs.copy_files(str(local_path), path, destination_filesystem=self.fs)

    def read_json(self, *parts):
        if not self.exists(*parts):
            return {}
        with self.fs.open_input_stream(self.path(*parts)) as f:
            return json.loads(f.read().decode("utf-8"))

    def write_json(self, data, *parts):
        path = self.path(*parts)
        if self.fs.type_name == "local":
            self.fs.create_dir(path.rsplit("/", 1)[0], recursive=True)
        with self.fs.open_output_stream(path) as f:
            f.write((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def read_bcdata_metadata(source):
    """The "bcdata" footer metadata written by bcdata.to_parquet"""
    metadata = pq.read_metadata(source).metadata or {}
    if b"bcdata" not in metadata:
        raise ValueError("parquet file has no bcdata metadata")
    return json.loads(metadata[b"bcdata"])


# ---------------------------------------------------------------------------
# change detection
# ---------------------------------------------------------------------------


def _type_name(arrow_type):
    if isinstance(arrow_type, pyarrow.ExtensionType):
        return "geometry"
    return str(arrow_type)


def schema_changes(path_a, path_b):
    """Columns added, removed and retyped between two parquet files"""
    a = {f.name: _type_name(f.type) for f in pq.read_schema(path_a)}
    b = {f.name: _type_name(f.type) for f in pq.read_schema(path_b)}
    return {
        "added": [c for c in b if c not in a],
        "removed": [c for c in a if c not in b],
        "retyped": {c: [a[c], b[c]] for c in a if c in b and a[c] != b[c]},
    }


def read_snapshot(path):
    """Read a mirror file for fit_changedetector.

    Integer columns are read as pandas nullable integers (geopandas would read an
    integer column with nulls as float64, so its type would depend on whether the
    download had nulls), and string columns as the "string" dtype.
    """
    df = geopandas.read_parquet(path)
    for f in pq.read_schema(path):
        if pyarrow.types.is_integer(f.type):
            df[f.name] = df[f.name].astype("Int64")
        elif pyarrow.types.is_string(f.type) or pyarrow.types.is_large_string(f.type):
            df[f.name] = df[f.name].astype("string")
    return df


def harmonize_types(df_a, df_b):
    """Make common columns comparable, returning the columns that can't be compared.

    bcdata infers integer vs float per download (a numeric column is written as
    int64 only if every value is whole), so a column can be int64 in one file and
    double in the next. Cast both to Float64 so values compare (and hash) the same.
    Columns whose types differ in any other way are returned, to be ignored.
    """
    incomparable = []
    for c in df_a.columns.intersection(df_b.columns):
        if c == GEOMETRY_COLUMN or df_a[c].dtype == df_b[c].dtype:
            continue
        if pandas.api.types.is_numeric_dtype(df_a[c]) and pandas.api.types.is_numeric_dtype(
            df_b[c]
        ):
            df_a[c] = df_a[c].astype("Float64")
            df_b[c] = df_b[c].astype("Float64")
        else:
            incomparable.append(c)
    return incomparable


def count_duplicates(path, hash_columns):
    """Number of records whose hash_columns values repeat an earlier record's.

    A quick check of every download (fit_changedetector only hashes when a file has
    changed). Values are compared as stored, so geometries that differ by less than
    fit_changedetector's hashing precision count as distinct here.
    """
    columns = ", ".join(f'"{c}"' for c in hash_columns)
    with duckdb.connect() as con:
        return con.sql(
            f"SELECT count(*) - count(DISTINCT ({columns})) FROM read_parquet(?)",
            params=[str(path)],
        ).fetchall()[0][0]


def add_key(fcd, df, hash_columns, allow_duplicates):
    """Add the generated key column, returning (df, number of records dropped)"""
    missing = [c for c in hash_columns if c not in df.columns]
    if missing:
        raise ValueError(f"hash_columns not present in data: {missing}")
    hash_geometry = GEOMETRY_COLUMN in hash_columns
    n_dropped = int(df.geometry.isna().sum()) if hash_geometry else 0
    df = fcd.add_hash_key(
        df,
        HASH_KEY,
        hash_columns,
        # fcd can't hash a missing geometry: drop (and count) those records.
        # See bcgov/FIT_changedetector#141
        drop_null_geometry=True if hash_geometry else None,
        allow_duplicates=allow_duplicates,
    )
    return df, n_dropped


# _change_type values, following the Delta Lake change data feed convention. The
# number orders the rows of a change file: by type, then key, then preimage first
CHANGE_TYPE_ORDER = {
    "insert": (0, 0),
    "delete": (1, 0),
    "update_preimage": (2, 0),
    "update_postimage": (2, 1),
}


def _change_rows(df, mask, change_type):
    rows = df[mask].copy()
    rows["_change_type"] = change_type
    return rows


def _json_keys(values):
    """Sorted key values as JSON-serializable Python values"""
    return [v.item() if hasattr(v, "item") else v for v in sorted(values)]


def diff_files(path_a, path_b, keys, timestamp=None, records=True):
    """Diff two mirror files, returning (summary dict, changed records or None).

    The summary has counts, and the keys of the inserted, deleted and updated
    records. Changed records are only assembled if records is true.

    keys is the table's KeyConfig. Changed records are full rows from the two files,
    laid out as a Delta Lake change data feed: the table's columns plus _change_type
    and _commit_timestamp. An inserted record is its current row (insert), a deleted
    record its previous row (delete), and an updated record both (update_preimage,
    update_postimage).

    With allow_duplicates, records sharing a generated key with an earlier record in
    the same file can't be paired between files. As in fit_changedetector, the first
    record with each key is compared, and the others are counted and their keys
    listed (summary keys.duplicate), not compared.
    """
    import fit_changedetector as fcd

    key, hash_columns, ignore_columns = keys.column, keys.hash_columns, keys.ignore_columns

    df_a = read_snapshot(path_a)
    df_b = read_snapshot(path_b)
    incomparable = harmonize_types(df_a, df_b)
    if hash_columns:
        bad = sorted(set(incomparable) & set(hash_columns))
        if bad:
            raise ValueError(f"hash_columns changed type and can't be compared: {bad}")
    dropped = {"previous": 0, "current": 0}
    if hash_columns:
        df_a, dropped["previous"] = add_key(fcd, df_a, hash_columns, keys.allow_duplicates)
        df_b, dropped["current"] = add_key(fcd, df_b, hash_columns, keys.allow_duplicates)
    elif key in incomparable:
        raise ValueError(f"primary key {key} changed type and can't be compared")

    ignored = [c for c in incomparable + list(ignore_columns) if c != key]
    ignored = [c for c in dict.fromkeys(ignored) if c in df_a.columns or c in df_b.columns]
    diff = fcd.gdf_diff(
        df_a,
        df_b,
        key,
        ignore_fields=ignored or None,
        allow_duplicates=keys.allow_duplicates,
        # the mirror is a faithful copy: single to multipart is a change
        promote_multi=False,
    )
    new = set(diff["NEW"][key])
    deleted = set(diff["DELETED"][key])
    modified = set()
    for t in ("MODIFIED_ATTR", "MODIFIED_GEOM", "MODIFIED_BOTH"):
        modified |= set(diff[t][key])
    dup_a = df_a[key].duplicated(keep="first")
    dup_b = df_b[key].duplicated(keep="first")

    changes = None
    parts = []
    if records:
        # columns whose type changed in a way that can't be compared are written as
        # text, so the previous and current rows fit one schema
        for c in incomparable:
            df_a[c] = df_a[c].astype("string")
            df_b[c] = df_b[c].astype("string")
        parts = [
            _change_rows(df_b, ~dup_b & df_b[key].isin(new), "insert"),
            _change_rows(df_a, ~dup_a & df_a[key].isin(deleted), "delete"),
            _change_rows(df_a, ~dup_a & df_a[key].isin(modified), "update_preimage"),
            _change_rows(df_b, ~dup_b & df_b[key].isin(modified), "update_postimage"),
        ]
        parts = [p for p in parts if len(p)]
    if parts:
        # current columns first, then any only in the previous file
        columns = list(
            dict.fromkeys([*df_b.columns, *df_a.columns, "_change_type", "_commit_timestamp"])
        )
        changes = geopandas.GeoDataFrame(
            pandas.concat(parts, ignore_index=True), geometry=GEOMETRY_COLUMN, crs=df_b.crs
        )
        changes["_commit_timestamp"] = pandas.Timestamp(timestamp or datetime.now(tz=UTC))
        changes = changes[[c for c in columns if c in changes.columns]]
        # keep the two rows of each updated record together, preimage first
        order = changes["_change_type"].map(CHANGE_TYPE_ORDER)
        changes = (
            changes.assign(_group=order.str[0], _image=order.str[1])
            .sort_values(["_group", key, "_image"], kind="stable")
            .drop(columns=["_group", "_image"])
        )

    # records (not rows): an updated record is counted once
    counts = {
        "insert": len(new),
        "delete": len(deleted),
        "update": len(modified),
        "unchanged": len(diff["UNCHANGED"]),
        "duplicate": int(dup_a.sum() + dup_b.sum()),
    }
    summary = {
        "counts": counts,
        # generated keys are fit_changedetector hashes of hash_columns
        "keys": {
            "insert": _json_keys(new),
            "delete": _json_keys(deleted),
            "update": _json_keys(modified),
        },
        "data_changed": bool(new or deleted or modified),
        "ignored_columns": ignored,
        "dropped_null_geometry": dropped,
        "fit_changedetector_version": fcd.__version__,
    }
    if keys.allow_duplicates:
        # one key per record not compared (only the first record with a key is),
        # by file, so the two lists add up to counts["duplicate"]
        summary["keys"]["duplicate"] = {
            "previous": _json_keys(df_a.loc[dup_a, key]),
            "current": _json_keys(df_b.loc[dup_b, key]),
        }
    return summary, changes


# ---------------------------------------------------------------------------
# replication
# ---------------------------------------------------------------------------


def _file_summary(meta):
    if not meta:
        return None
    return {
        k: meta.get(k)
        for k in ("content_hash", "schema_hash", "feature_count", "downloaded", "bcdata_version")
    }


def write_change_record(dest, table, timestamp, record, changes, tmpdir):
    """Write change.json and changes.parquet under changes/<table>/<timestamp>/"""
    record["files"] = []
    if changes is not None:
        local = os.path.join(tmpdir, "changes.parquet")
        changes.to_parquet(local, index=False)
        dest.upload(local, "changes", table, timestamp, "changes.parquet")
        record["files"].append("changes.parquet")
    dest.write_json(record, "changes", table, timestamp, "change.json")


def replicate_table(dest, table, fields, now, download=None, table_metadata=None):
    """Check one table and update the mirror, returning the new state entry"""
    download = download or bcdata.to_parquet
    keys = key_config(table, fields, table_metadata)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory(prefix="bcdata_replicate_") as tmpdir:
        new_path = os.path.join(tmpdir, "current.parquet")
        download(table, out_path=new_path)
        new_meta = read_bcdata_metadata(new_path)
        # check every download, not only changed ones, so a key that stops being
        # unique is reported the first time it happens
        if keys.hash_columns and not keys.allow_duplicates:
            n_duplicates = count_duplicates(new_path, keys.hash_columns)
            if n_duplicates:
                raise DuplicateKeyError(
                    f"{table}: {n_duplicates} records repeat the hash_columns values of "
                    "another record. Add columns to hash_columns, or set "
                    "allow_duplicates = true"
                )
        previous_meta = dest.read_bcdata_metadata(f"{table}.parquet")
        entry = {
            "checked": now.isoformat(timespec="seconds"),
            "content_hash": new_meta["content_hash"],
            "feature_count": new_meta["feature_count"],
        }

        if previous_meta and previous_meta["content_hash"] == new_meta["content_hash"]:
            log.info(f"{table}: unchanged ({new_meta['feature_count']} features)")
            entry["status"] = "unchanged"
            return entry

        record = {
            "table": table,
            "timestamp": now.isoformat(timespec="seconds"),
            "previous": _file_summary(previous_meta),
            "current": _file_summary(new_meta),
            "key": {
                "column": keys.column,
                "generated": bool(keys.hash_columns),
                "hash_columns": keys.hash_columns,
                "allow_duplicates": keys.allow_duplicates,
            },
            "bcdata_version": bcdata.__version__,
        }
        changes = None
        if previous_meta is None:
            log.info(f"{table}: initial load ({new_meta['feature_count']} features)")
            record["kind"] = "initial"
            record["data_changed"] = True
        else:
            previous_path = os.path.join(tmpdir, "previous.parquet")
            dest.download(previous_path, f"{table}.parquet")
            record["kind"] = "changed"
            record["schema_changes"] = schema_changes(previous_path, new_path)
            summary, changes = diff_files(
                previous_path,
                new_path,
                keys,
                timestamp=now,
                records=fields.get("keep_change_records", False),
            )
            record.update(summary)
            if record["schema_changes"]["added"] or record["schema_changes"]["removed"]:
                record["data_changed"] = True
            log.info(f"{table}: changed {summary['counts']}")

        # record the change before uploading, so every uploaded file has a record
        write_change_record(dest, table, timestamp, record, changes, tmpdir)
        dest.upload(new_path, f"{table}.parquet")
        entry["status"] = record["kind"]
        entry["changed"] = record["timestamp"]
        return entry


def run(dest, config, tables=None, force=False, now=None, download=None, table_metadata=None):
    """Replicate due tables, returning {table: error message} for tables that failed"""
    # whole seconds, so the folder name, change.json and _commit_timestamp all match
    now = (now or datetime.now(tz=UTC)).replace(microsecond=0)
    state = dest.read_json("state.json")
    selected = tables or sorted(config)
    unknown = sorted(set(selected) - set(config))
    if unknown:
        raise ConfigError(f"Not in replication.toml: {unknown}")
    due = [t for t in selected if force or is_due(config[t], state.get(t), now)]
    log.info(f"{len(due)} of {len(selected)} tables due")
    failures = {}
    for table in due:
        try:
            entry = _replicate_with_retry(dest, table, config[table], now, download, table_metadata)
        except Exception as e:
            # one failing table must not stop the others
            log.exception(f"{table}: failed")
            failures[table] = f"{type(e).__name__}: {e}"
            continue
        # save after every table, so progress survives a failed or cancelled run.
        # Merge, so an unchanged check keeps the time of the last change
        state[table] = {**state.get(table, {}), **entry}
        dest.write_json(state, "state.json")
    return failures


def _replicate_with_retry(dest, table, fields, now, download, table_metadata):
    """replicate_table, retried once if the download fails its consistency check"""
    try:
        return replicate_table(dest, table, fields, now, download, table_metadata)
    except DownloadCheckError as e:
        # the source was probably edited during the download
        log.warning(f"{table}: {e} - retrying")
        return replicate_table(dest, table, fields, now, download, table_metadata)


def write_report(failures, path):
    lines = [f"`scripts/replicate.py`: {len(failures)} table(s) failed", ""]
    lines += ["| Table | Error |", "|---|---|"]
    lines += [f"| `{t}` | {e.splitlines()[0] if e else ''} |" for t, e in sorted(failures.items())]
    Path(path).write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dest",
        default=os.environ.get(DEST_ENV),
        help=f"destination URI (default: ${DEST_ENV})",
    )
    parser.add_argument("--tables", help="comma separated tables to check (default: all)")
    parser.add_argument("--force", action="store_true", help="check tables even if not due")
    parser.add_argument("--report", help="write failures to this markdown file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FORMAT
    )
    if not args.dest:
        parser.error(f"no destination: use --dest or set ${DEST_ENV}")
    config = load_config()
    tables = [t.strip().lower() for t in args.tables.split(",")] if args.tables else None
    failures = run(Destination(args.dest), config, tables=tables, force=args.force)
    if failures:
        for table, error in sorted(failures.items()):
            log.error(f"{table}: {error}")
        if args.report:
            write_report(failures, args.report)
        sys.exit(1)


if __name__ == "__main__":
    main()
