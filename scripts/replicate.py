"""Replicate the tables listed in data/replication.toml to a GeoParquet mirror.

For each table that is due (per its schedule and the last check recorded in the
destination's state.json):

1. download the whole table with bcdata.to_parquet to a temp file
2. compare its content_hash, and its catalogue metadata (description, comments,
   column descriptions, catalogue record), with the file at the destination
   (footer only)
3. unchanged: record the check in state.json
4. only the catalogue metadata changed: add a "metadata" row to the table's change
   log and upload the new file (no diff: the records are the same)
5. changed (or not yet replicated): diff the previous and new files with
   fit_changedetector, add a row to the table's change log, upload the new file,
   then record the check in state.json

    python scripts/replicate.py --dest s3://bucket/prefix [--tables a,b] [--force]

The destination is --dest, or the BCDATA_REPLICATION_DEST environment variable: an
object store URL (s3://bucket/prefix, az://container/prefix, gs://bucket/prefix) or
a local folder. Files are transferred with obstore. Credentials and endpoints come
from the standard environment variables of each service, eg AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY and AWS_ENDPOINT_URL for S3 (plus AWS_ALLOW_HTTP=true for a
plain http endpoint), or AZURE_STORAGE_ACCOUNT_NAME and AZURE_STORAGE_ACCOUNT_KEY
for Azure.

Destination layout, a folder per table:

    <dest>/<table>/<table>.parquet            latest copy of the table
    <dest>/<table>/changes.parquet            change log: one row per change, with
                                              counts, file summaries, schema changes
                                              and the keys of the changed records
                                              (list columns, so reading only the
                                              summary columns doesn't read them)
    <dest>/<table>/changes.json               the change log without the keys, as
                                              readable JSON (rebuilt from
                                              changes.parquet on each change)
    <dest>/<table>/changes/<timestamp>.parquet  with keep_change_records: the changed
                                              records as full rows, plus
                                              _change_type and _commit_timestamp (as
                                              in a Delta Lake change data feed)
    <dest>/state.json                         last check of each table

The change log is rewritten (downloaded, appended to and uploaded) on each change:
S3 has no append.

Mirror files are written by bcdata.to_parquet unchanged. Keys generated for tables
without a primary key (hash_columns) exist only in the diff and the change records.

Exit codes:
    0  every due table was checked
    1  one or more tables failed (listed in the log, and in --report if given)
"""

import argparse
import io
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

import bcdata
from bcdata.parquet import DownloadCheckError

try:
    import obstore
    from obstore.store import LocalStore, from_url
except ImportError:  # validate_tables.py reads the config without the replicate group
    obstore = None

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

FIELDS = {
    "schedule",
    "keep_change_records",
    "hash_columns",
    "ignore_columns",
    "allow_duplicates",
    "rekey",
}
GEOMETRY_COLUMN = "geometry"
# name of the generated key column, in the diff and change records only
HASH_KEY = "_bcdata_hash_key"


class ConfigError(ValueError):
    """replication.toml is invalid"""


class DuplicateKeyError(ValueError):
    """A generated key is not unique, and the table does not allow duplicates"""


class RekeyError(ValueError):
    """A table's key changed since its last change log row, and rekey is not set"""


class CatalogueMissingError(RuntimeError):
    """A changed download has no catalogue metadata, but the mirror file has"""


class ConditionalWriteError(RuntimeError):
    """The destination store doesn't support conditional writes"""


class ConcurrentWriteError(RuntimeError):
    """A file changed between reading and rewriting it, eg by an overlapping run"""


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
        for flag in ("keep_change_records", "rekey"):
            if not isinstance(fields.get(flag, False), bool):
                raise ConfigError(f"{table}: {flag} must be true or false")
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
    """The mirror location: an object store URL (s3://..., az://..., gs://...) or a
    local folder. Paths within it are given as parts, eg (table, "changes.parquet")

    store: an obstore store to use instead of one built from uri (eg a MemoryStore)
    """

    def __init__(self, uri, store=None):
        if obstore is None:
            raise ImportError("obstore is required: uv sync --group replicate")
        self.uri = uri.rstrip("/")
        if store is None:
            if self.uri.startswith("file://"):
                store = from_url(self.uri, mkdir=True)
            elif "://" in self.uri:
                store = from_url(self.uri)
            else:
                store = LocalStore(os.path.abspath(self.uri), mkdir=True)
        self.store = store

    @staticmethod
    def key(*parts):
        return "/".join(parts)

    def url(self, *parts):
        return "/".join([self.uri, *parts])

    def exists(self, *parts):
        try:
            obstore.head(self.store, self.key(*parts))
        except FileNotFoundError:
            return False
        return True

    def read_bcdata_metadata(self, *parts):
        """bcdata footer metadata of a parquet file, or None if there is no file.

        Reads only the footer: a parquet file ends with the footer, its length (4
        bytes, little endian) and the magic bytes PAR1.
        """
        key = self.key(*parts)
        try:
            size = obstore.head(self.store, key)["size"]
        except FileNotFoundError:
            return None
        if size < 12:  # the magic bytes at both ends, and the footer length
            raise ValueError(f"{self.url(*parts)} is not a parquet file")
        tail = bytes(obstore.get_range(self.store, key, start=size - 8, end=size))
        length = int.from_bytes(tail[:4], "little")
        if tail[4:] != b"PAR1" or length > size - 12:
            raise ValueError(f"{self.url(*parts)} is not a parquet file")
        footer = bytes(obstore.get_range(self.store, key, start=size - 8 - length, end=size - 8))
        return read_bcdata_metadata(io.BytesIO(b"PAR1" + footer + tail))

    def download(self, local_path, *parts):
        """Download a file, returning its ETag"""
        result = obstore.get(self.store, self.key(*parts))
        with open(local_path, "wb") as f:
            f.writelines(result)
        return result.meta.get("e_tag")

    def upload(self, local_path, *parts):
        # multipart for files over 5 MB
        obstore.put(self.store, self.key(*parts), Path(local_path))

    def replace(self, local_path, *parts, e_tag=None):
        """Upload a file read earlier, only if it hasn't changed since.

        e_tag is the file's ETag when it was read, or None if it didn't exist (then
        the upload only succeeds if it still doesn't). Raises ConcurrentWriteError if
        the file was written in the meantime, eg by an overlapping run.

        A local folder (obstore's LocalStore has no conditional writes) is written to
        unconditionally. Any other store without them raises ConditionalWriteError:
        overwriting would let overlapping runs drop each other's change log rows.
        """
        key = self.key(*parts)
        mode = {"e_tag": e_tag} if e_tag else "create"
        try:
            # a conditional put is never multipart
            obstore.put(self.store, key, Path(local_path), mode=mode)
        except (obstore.exceptions.PreconditionError, obstore.exceptions.AlreadyExistsError) as e:
            raise ConcurrentWriteError(
                f"{self.url(*parts)} changed since it was read, eg by another run"
            ) from e
        except NotImplementedError as e:
            if not isinstance(self.store, LocalStore):
                raise ConditionalWriteError(
                    f"{self.url(*parts)}: the store doesn't support conditional writes, "
                    "so overlapping runs could drop each other's changes. Not written"
                ) from e
            log.debug(f"{self.url(*parts)}: local folder, overwriting")
            obstore.put(self.store, key, Path(local_path))

    def delete(self, *parts):
        obstore.delete(self.store, self.key(*parts))

    def read_bytes(self, *parts):
        return bytes(obstore.get(self.store, self.key(*parts)).bytes())

    def read_json(self, *parts):
        try:
            return json.loads(self.read_bytes(*parts).decode("utf-8"))
        except FileNotFoundError:
            return {}

    def write_json(self, data, *parts, sort_keys=True):
        text = json.dumps(data, indent=2, sort_keys=sort_keys) + "\n"
        obstore.put(self.store, self.key(*parts), text.encode("utf-8"))


def read_bcdata_metadata(source):
    """The "bcdata" footer metadata written by bcdata.to_parquet"""
    metadata = pq.read_metadata(source).metadata or {}
    if b"bcdata" not in metadata:
        raise ValueError("parquet file has no bcdata metadata")
    return json.loads(metadata[b"bcdata"])


def mirror_parts(table):
    """Path of a table's mirror file in the destination"""
    return (table, f"{table}.parquet")


def change_log_parts(table):
    return (table, "changes.parquet")


def change_summary_parts(table):
    return (table, "changes.json")


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


def _hash_text(series):
    """A numeric column as text for hashing, the same whatever type bcdata inferred.

    fit_changedetector hashes str() of each value, after pandas has converted the
    row to a common type, so int64 1 and double 1.0 would hash differently, and an
    integer could hash as "1.0" next to a double. Whole numbers are written as
    integers ("1"), others as Python writes floats ("2.5").
    """
    if pandas.api.types.is_bool_dtype(series):
        return series.astype("string")
    if pandas.api.types.is_integer_dtype(series):
        return series.astype("Int64").astype("string")
    values = series.astype("Float64")
    whole = ((values == values.round()) & (values.abs() < 1e18)).fillna(False)
    text = values.astype("string")
    text[whole] = values[whole].astype("Int64").astype("string")
    return text


def add_key(fcd, df, hash_columns, allow_duplicates):
    """Add the generated key column, returning (df, number of records dropped)"""
    missing = [c for c in hash_columns if c not in df.columns]
    if missing:
        raise ValueError(f"hash_columns not present in data: {missing}")
    hash_geometry = GEOMETRY_COLUMN in hash_columns
    n_dropped = int(df.geometry.isna().sum()) if hash_geometry else 0
    # numeric columns are hashed as text copies (see _hash_text), so the compared
    # and written columns keep their types
    numeric = {
        c: f"_bcdata_hash_{c}"
        for c in hash_columns
        if c != GEOMETRY_COLUMN and pandas.api.types.is_numeric_dtype(df[c])
    }
    df = df.assign(**{copy: _hash_text(df[c]) for c, copy in numeric.items()})
    df = fcd.add_hash_key(
        df,
        HASH_KEY,
        [numeric.get(c, c) for c in hash_columns],
        # fcd can't hash a missing geometry: drop (and count) those records.
        # See bcgov/FIT_changedetector#141
        drop_null_geometry=True if hash_geometry else None,
        allow_duplicates=allow_duplicates,
    )
    return df.drop(columns=list(numeric.values())), n_dropped


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


def _key_list(values):
    """Sorted key values as Python values"""
    return [v.item() if hasattr(v, "item") else v for v in sorted(values)]


def diff_files(path_a, path_b, keys, timestamp=None, records=True):
    """Diff two mirror files, returning (summary dict, changed records or None).

    The summary has the change log's counts and keys columns. Changed records are
    only assembled if records is true.

    keys is the table's KeyConfig. Changed records are full rows from the two files,
    laid out as a Delta Lake change data feed: the table's columns plus _change_type
    and _commit_timestamp. An inserted record is its current row (insert), a deleted
    record its previous row (delete), and an updated record both (update_preimage,
    update_postimage).

    With allow_duplicates, records sharing a generated key with an earlier record in
    the same file can't be paired between files. As in fit_changedetector, the first
    record with each key is compared, and the others are counted and their keys
    listed (duplicate_keys_previous / duplicate_keys_current), not compared.
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

    # records (not rows): an updated record is counted once. Generated keys are
    # fit_changedetector hashes of hash_columns
    summary = {
        "inserted": len(new),
        "deleted": len(deleted),
        "updated": len(modified),
        "unchanged": len(diff["UNCHANGED"]),
        "duplicates": int(dup_a.sum() + dup_b.sum()),
        "inserted_keys": _key_list(new),
        "deleted_keys": _key_list(deleted),
        "updated_keys": _key_list(modified),
        "data_changed": bool(new or deleted or modified),
        "ignored_columns": ignored,
        "dropped_null_geometry_previous": dropped["previous"],
        "dropped_null_geometry_current": dropped["current"],
        "fit_changedetector_version": fcd.__version__,
    }
    if keys.allow_duplicates:
        # one key per record not compared (only the first record with a key is),
        # by file, so the two lists add up to duplicates
        summary["duplicate_keys_previous"] = _key_list(df_a.loc[dup_a, key])
        summary["duplicate_keys_current"] = _key_list(df_b.loc[dup_b, key])
    return summary, changes


# ---------------------------------------------------------------------------
# replication
# ---------------------------------------------------------------------------


def change_log_schema(key_type):
    """Schema of a table's change log, given the arrow type of its key"""
    string, count, flag = pyarrow.string(), pyarrow.int64(), pyarrow.bool_()
    keys, strings = pyarrow.list_(key_type), pyarrow.list_(string)
    return pyarrow.schema(
        [
            ("_commit_timestamp", pyarrow.timestamp("s", tz="UTC")),
            ("kind", string),  # initial | changed | metadata
            ("data_changed", flag),
            # catalogue metadata fields (METADATA_FIELDS) that differ from the
            # previous file. Null for an initial load
            ("metadata_changed", strings),
            # the previous and new files (bcdata footer metadata)
            ("previous_content_hash", string),
            ("current_content_hash", string),
            ("previous_schema_hash", string),
            ("current_schema_hash", string),
            ("previous_feature_count", count),
            ("current_feature_count", count),
            ("previous_downloaded", string),
            ("current_downloaded", string),
            # records, not rows. Null for an initial load (nothing to compare with)
            ("inserted", count),
            ("deleted", count),
            ("updated", count),
            ("unchanged", count),
            ("duplicates", count),
            ("inserted_keys", keys),
            ("deleted_keys", keys),
            ("updated_keys", keys),
            ("duplicate_keys_previous", keys),  # allow_duplicates only
            ("duplicate_keys_current", keys),
            ("columns_added", strings),
            ("columns_removed", strings),
            ("columns_retyped", string),  # JSON: {column: [previous type, new type]}
            # how records were matched
            ("key_column", string),
            ("key_generated", flag),
            ("hash_columns", strings),
            ("allow_duplicates", flag),
            # the key (key_column / hash_columns) changed since the previous row,
            # accepted with rekey. Keys before and after don't refer to the same
            # records
            ("rekeyed", flag),
            ("ignored_columns", strings),
            ("dropped_null_geometry_previous", count),
            ("dropped_null_geometry_current", count),
            # path of the changed records, relative to the table's folder
            # (keep_change_records only)
            ("records_file", string),
            ("bcdata_version", string),
            ("fit_changedetector_version", string),
        ]
    )


# bcdata footer fields describing the table, from the BC Data Catalogue. They
# aren't part of content_hash, so an edit to the catalogue record alone is found
# by comparing them
METADATA_FIELDS = ("description", "comments", "columns", "catalogue")


def metadata_changes(previous_meta, new_meta):
    """Names of the METADATA_FIELDS that differ between two files' footers"""
    return [f for f in METADATA_FIELDS if previous_meta.get(f) != new_meta.get(f)]


def key_type(path, keys):
    """Arrow type of a table's key, in the change log"""
    if keys.hash_columns:
        return pyarrow.string()
    return pq.read_schema(path).field(keys.column).type


# change log columns holding key values
KEY_LIST_COLUMNS = (
    "inserted_keys",
    "deleted_keys",
    "updated_keys",
    "duplicate_keys_previous",
    "duplicate_keys_current",
)


def _keys_as_strings(log_table):
    string_keys = pyarrow.list_(pyarrow.string())
    for name in KEY_LIST_COLUMNS:
        i = log_table.schema.get_field_index(name)
        if i >= 0:
            log_table = log_table.set_column(i, name, log_table[name].cast(string_keys))
    return log_table


def read_change_log(dest, table, local_path):
    """The table's change log and its ETag, or (None, None) if it has none"""
    parts = change_log_parts(table)
    if not dest.exists(*parts):
        return None, None
    e_tag = dest.download(local_path, *parts)
    return pq.read_table(local_path), e_tag


def check_key(table, keys, change_log, rekey):
    """Compare the table's key with the last change log row's.

    Returns True if the key changed (and rekey allows it). Raises RekeyError if it
    changed and rekey is not set: keys before and after the change would not refer
    to the same records, eg after hash_columns is edited every record is reported
    as deleted and inserted.
    """
    if change_log is None or change_log.num_rows == 0:
        return False
    last = change_log.slice(change_log.num_rows - 1).to_pylist()[0]
    previous = (last["key_column"], last["hash_columns"])
    current = (keys.column, keys.hash_columns)
    if previous == current:
        if rekey:
            log.warning(f"{table}: rekey is set but the key is unchanged, remove it")
        return False

    def describe(column, hash_columns):
        return f"hash of {hash_columns}" if hash_columns else f"primary key {column}"

    if not rekey:
        raise RekeyError(
            f"{table}: key changed from {describe(*previous)} to {describe(*current)} "
            "since the last change. Keys logged before and after won't refer to the "
            "same records. Set rekey = true for this table to accept it (and remove it "
            "once the change is logged)"
        )
    log.warning(f"{table}: rekeyed from {describe(*previous)} to {describe(*current)}")
    return True


def append_change_log(change_log, row, schema, local_path):
    """Write the change log (or None) with a row added, to local_path.

    The log is uploaded by the caller (S3 has no append, so each change rewrites
    the whole file). Key columns of a different type in earlier rows are widened
    where possible (eg int32 to int64). Where they can't be (a rekey from an
    integer primary key to a generated string key, or back), the key columns of
    every row are converted to strings, so the history stays in one file. Each
    row's key_column says what its keys are.
    """
    log_table = pyarrow.Table.from_pylist([row], schema=schema)
    if change_log is not None:
        try:
            log_table = pyarrow.concat_tables([change_log, log_table], promote_options="permissive")
        except (pyarrow.ArrowTypeError, pyarrow.ArrowInvalid):
            log.warning("key type changed, change log keys converted to strings")
            log_table = pyarrow.concat_tables(
                [_keys_as_strings(change_log), _keys_as_strings(log_table)],
                promote_options="permissive",
            )
    pq.write_table(log_table, local_path)
    return log_table


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def change_summary(table, log_table):
    """The change log without its key columns, as a JSON-serializable dict"""
    columns = [c for c in log_table.column_names if c not in KEY_LIST_COLUMNS]
    changes = []
    for row in log_table.select(columns).to_pylist():
        row = {k: _json_value(v) for k, v in row.items()}
        if row.get("columns_retyped"):
            row["columns_retyped"] = json.loads(row["columns_retyped"])
        changes.append(row)
    return {"table": table, "changes": changes}


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
        previous_meta = dest.read_bcdata_metadata(*mirror_parts(table))
        entry = {
            "checked": now.isoformat(timespec="seconds"),
            "content_hash": new_meta["content_hash"],
            "feature_count": new_meta["feature_count"],
        }

        same_content = bool(previous_meta) and (
            previous_meta["content_hash"] == new_meta["content_hash"]
        )
        # bcdata.to_parquet still writes the file if the BC Data Catalogue lookup
        # fails, without the catalogue metadata. Don't replace a mirror file that has
        # it with one that doesn't, or log the gap as a metadata change
        if previous_meta and previous_meta.get("catalogue") and not new_meta.get("catalogue"):
            if same_content:
                log.warning(
                    f"{table}: unchanged, but no catalogue record in the download "
                    "(catalogue unavailable?), mirror metadata not compared"
                )
                entry["status"] = "unchanged"
                return entry
            raise CatalogueMissingError(
                f"{table}: changed, but the download has no catalogue record (catalogue "
                "unavailable, or the record removed), mirror not replaced"
            )
        changed_metadata = metadata_changes(previous_meta, new_meta) if previous_meta else None
        if same_content and not changed_metadata:
            log.info(f"{table}: unchanged ({new_meta['feature_count']} features)")
            entry["status"] = "unchanged"
            return entry

        # before diffing: a changed key needs rekey
        change_log, log_e_tag = read_change_log(
            dest, table, os.path.join(tmpdir, "change_log_previous.parquet")
        )
        rekeyed = check_key(table, keys, change_log, fields.get("rekey", False))

        row = {
            "_commit_timestamp": now,
            "rekeyed": rekeyed,
            "key_column": keys.column,
            "key_generated": bool(keys.hash_columns),
            "hash_columns": keys.hash_columns,
            "allow_duplicates": keys.allow_duplicates,
            "metadata_changed": changed_metadata,
            "bcdata_version": bcdata.__version__,
        }
        for prefix, meta in (("previous", previous_meta), ("current", new_meta)):
            for k in ("content_hash", "schema_hash", "feature_count", "downloaded"):
                row[f"{prefix}_{k}"] = (meta or {}).get(k)
        changes = None
        if previous_meta is None:
            log.info(f"{table}: initial load ({new_meta['feature_count']} features)")
            row["kind"] = "initial"
            row["data_changed"] = True
        elif same_content:
            # same records, new description: replace the file so its footer is
            # current. Nothing to diff
            log.info(f"{table}: metadata changed {changed_metadata}")
            row["kind"] = "metadata"
            row["data_changed"] = False
        else:
            previous_path = os.path.join(tmpdir, "previous.parquet")
            dest.download(previous_path, *mirror_parts(table))
            row["kind"] = "changed"
            columns = schema_changes(previous_path, new_path)
            summary, changes = diff_files(
                previous_path,
                new_path,
                keys,
                timestamp=now,
                records=fields.get("keep_change_records", False),
            )
            row.update(summary)
            row["columns_added"] = columns["added"]
            row["columns_removed"] = columns["removed"]
            row["columns_retyped"] = json.dumps(columns["retyped"]) if columns["retyped"] else None
            if columns["added"] or columns["removed"]:
                row["data_changed"] = True
            counts = {k: summary[k] for k in ("inserted", "deleted", "updated", "duplicates")}
            log.info(f"{table}: changed {counts}")

        # write everything locally before uploading anything, so a failure doesn't
        # leave a records file the log doesn't list. Then upload the records file,
        # the log, and the mirror file last, so every uploaded mirror file has a
        # change log row. The log is only replaced if it hasn't changed since it was
        # read: two overlapping runs would otherwise each drop the other's row. The
        # JSON summary is rebuilt from the log, so it is written unconditionally
        records_local = records_parts = None
        if changes is not None:
            row["records_file"] = f"changes/{timestamp}.parquet"
            records_parts = (table, "changes", f"{timestamp}.parquet")
            records_local = os.path.join(tmpdir, "records.parquet")
            changes.to_parquet(records_local, index=False)
        log_local = os.path.join(tmpdir, "change_log.parquet")
        schema = change_log_schema(key_type(new_path, keys))
        log_table = append_change_log(change_log, row, schema, log_local)
        summary = change_summary(table, log_table)
        if records_local:
            dest.upload(records_local, *records_parts)
        try:
            dest.replace(log_local, *change_log_parts(table), e_tag=log_e_tag)
        except (ConcurrentWriteError, ConditionalWriteError):
            if records_local:
                dest.delete(*records_parts)
            raise
        dest.write_json(summary, *change_summary_parts(table), sort_keys=False)
        dest.upload(new_path, *mirror_parts(table))
        entry["status"] = row["kind"]
        entry["changed"] = now.isoformat(timespec="seconds")
        return entry


def run(dest, config, tables=None, force=False, now=None, download=None, table_metadata=None):
    """Replicate due tables, returning {table: error message} for tables that failed"""
    # whole seconds, so the records file name and _commit_timestamp match
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
