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
   DuckDB, add a row to the table's change log, upload the new file,
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
        tail = bytes(obstore.get_range(self.store, key, start=size - 8, end=size))
        if size < 12 or tail[4:] != b"PAR1":
            raise ValueError(f"{self.url(*parts)} is not a parquet file")
        length = int.from_bytes(tail[:4], "little")
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
        the file was written in the meantime, eg by an overlapping run. Stores that
        don't support conditional writes (obstore's LocalStore) are written to
        unconditionally.
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
        except NotImplementedError:
            log.debug(f"{self.url(*parts)}: store has no conditional writes, overwriting")
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

# columns never compared: BCGW's objectid is regenerated when a table is reloaded.
# Compared anyway if it is the key or a hash_columns column
IMPLICIT_IGNORE = ("objectid",)


def _type_name(arrow_type):
    if isinstance(arrow_type, pyarrow.ExtensionType):
        return "geometry"
    return str(arrow_type)


def _is_numeric(arrow_type):
    return (
        pyarrow.types.is_integer(arrow_type)
        or pyarrow.types.is_floating(arrow_type)
        or pyarrow.types.is_decimal(arrow_type)
    )


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def schema_changes(path_a, path_b):
    """Columns added, removed and retyped between two parquet files"""
    a = {f.name: _type_name(f.type) for f in pq.read_schema(path_a)}
    b = {f.name: _type_name(f.type) for f in pq.read_schema(path_b)}
    return {
        "added": [c for c in b if c not in a],
        "removed": [c for c in a if c not in b],
        "retyped": {c: [a[c], b[c]] for c in a if c in b and a[c] != b[c]},
    }


def _hash_value_sql(name, arrow_type):
    """SQL giving a column's value as text for hashing, the same whatever type
    bcdata inferred: a whole number is written as an integer (1, not 1.0) whether
    the column is int64 or double. Geometries are their WKB, as hex."""
    c = _quote(name)
    if name == GEOMETRY_COLUMN or pyarrow.types.is_binary(arrow_type):
        return f"hex({c}::BLOB)"
    if pyarrow.types.is_integer(arrow_type):
        return f"{c}::VARCHAR"
    if _is_numeric(arrow_type):
        c = f"{c}::DOUBLE"
        return (
            f"CASE WHEN {c} = round({c}) AND abs({c}) < 1e18 "
            f"THEN {c}::BIGINT::VARCHAR ELSE {c}::VARCHAR END"
        )
    return f"{c}::VARCHAR"


def hash_key_sql(path, hash_columns):
    """SQL expression generating the key of each record of a parquet file: the
    sha256 of its hash_columns values, as a JSON list of text (nulls included).

    Changing how keys are generated changes every generated key: the tables with
    hash_columns then need rekey.
    """
    schema = pq.read_schema(path)
    missing = [c for c in hash_columns if c not in schema.names]
    if missing:
        raise ValueError(f"hash_columns not present in data: {missing}")
    values = ", ".join(_hash_value_sql(c, schema.field(c).type) for c in hash_columns)
    return f"sha256(to_json([{values}]))"


def count_duplicates(path, hash_columns):
    """Number of records whose generated key repeats an earlier record's"""
    with duckdb.connect() as con:
        return con.sql(
            f"SELECT count(*) - count(DISTINCT {hash_key_sql(path, hash_columns)}) "
            "FROM read_parquet(?)",
            params=[str(path)],
        ).fetchall()[0][0]


def _comparable(type_a, type_b):
    if _is_numeric(type_a) and _is_numeric(type_b):
        return True  # compared as numbers: int64 1 equals double 1.0
    return _type_name(type_a) == _type_name(type_b)


def _records_geo_metadata(path_a, path_b):
    """GeoParquet metadata for the changed records, from the two files', or None"""
    geo = [pq.read_schema(p).metadata or {} for p in (path_a, path_b)]
    geo = [json.loads(m[b"geo"]) for m in geo if b"geo" in m]
    if not geo:
        return None
    metadata = geo[-1]
    column = dict(metadata["columns"][GEOMETRY_COLUMN])
    column.pop("bbox", None)  # a bbox and types describing the records' geometries
    types = [t for g in geo for t in g["columns"][GEOMETRY_COLUMN].get("geometry_types", [])]
    column["geometry_types"] = sorted(set(types))
    return {**metadata, "columns": {GEOMETRY_COLUMN: column}}


def diff_files(path_a, path_b, keys, timestamp=None, records=True):
    """Diff two mirror files with DuckDB, returning (summary dict, changed records
    as an arrow table, or None).

    The summary has the change log's counts and keys columns. Changed records are
    only assembled if records is true.

    keys is the table's KeyConfig. Records are matched by key and compared on the
    columns in both files, except ignore_columns, IMPLICIT_IGNORE and columns whose
    type changed in a way that can't be compared (integer to double can be).
    Geometries are compared exactly (as WKB).

    Changed records are full rows from the two files, laid out as a Delta Lake
    change data feed: the table's columns plus _change_type and _commit_timestamp.
    An inserted record is its current row (insert), a deleted record its previous
    row (delete), and an updated record both (update_preimage, update_postimage).

    With allow_duplicates, records sharing a generated key with an earlier record in
    the same file can't be paired between files. The first record with each key (in
    file order) is compared, and the others are counted and their keys listed
    (duplicate_keys_previous / duplicate_keys_current), not compared.
    """
    key = keys.column
    schema_a, schema_b = pq.read_schema(path_a), pq.read_schema(path_b)
    common = [c for c in schema_b.names if c in schema_a.names]
    incomparable = [
        c for c in common if not _comparable(schema_a.field(c).type, schema_b.field(c).type)
    ]
    if keys.hash_columns:
        bad = sorted(set(incomparable) & set(keys.hash_columns))
        if bad:
            raise ValueError(f"hash_columns changed type and can't be compared: {bad}")
    elif key in incomparable:
        raise ValueError(f"primary key {key} changed type and can't be compared")
    if key not in common and not keys.hash_columns:
        raise ValueError(f"primary key {key} not present in both files")

    keep = {key, *(keys.hash_columns or [])}
    implicit = [c for c in IMPLICIT_IGNORE if c not in keep]
    ignored = [
        c
        for c in dict.fromkeys([*incomparable, *keys.ignore_columns, *implicit])
        if c != key and (c in schema_a.names or c in schema_b.names)
    ]
    compared = [c for c in common if c not in ignored and c != key]

    con = duckdb.connect(config={"temp_directory": os.path.join(os.path.dirname(path_b), "duckdb")})
    with con:
        for name, path, schema in (("a", path_a, schema_a), ("b", path_b, schema_b)):
            select = "*"
            if GEOMETRY_COLUMN in schema.names:
                g = _quote(GEOMETRY_COLUMN)
                select = f"* REPLACE ({g}::BLOB AS {g})"
            if keys.hash_columns:
                select += f", {hash_key_sql(path, keys.hash_columns)} AS {_quote(HASH_KEY)}"
            # _rn: 1 for the first record with each key, in file order
            con.execute(
                f"""CREATE TEMP TABLE {name} AS
                SELECT * EXCLUDE (file_row_number), row_number() OVER (
                    PARTITION BY {_quote(key)} ORDER BY file_row_number
                ) AS _rn
                FROM (SELECT {select} FROM read_parquet(?, file_row_number = true))""",
                [str(path)],
            )
        k = _quote(key)
        modified = " OR ".join(f"a.{_quote(c)} IS DISTINCT FROM b.{_quote(c)}" for c in compared)
        con.execute(
            f"""CREATE TEMP TABLE diff AS
            SELECT a.{k} AS key_a, b.{k} AS key_b,
                CASE WHEN a.{k} IS NULL THEN 'insert'
                     WHEN b.{k} IS NULL THEN 'delete'
                     WHEN {modified or "false"} THEN 'update'
                     ELSE 'unchanged' END AS change
            FROM (SELECT * FROM a WHERE _rn = 1) a
            FULL JOIN (SELECT * FROM b WHERE _rn = 1) b ON a.{k} = b.{k}"""
        )

        def key_list(sql):
            return con.sql(f"SELECT list({k} ORDER BY {k}) FROM ({sql})").fetchall()[0][0] or []

        new = key_list(f"SELECT key_b AS {k} FROM diff WHERE change = 'insert'")
        deleted = key_list(f"SELECT key_a AS {k} FROM diff WHERE change = 'delete'")
        updated = key_list(f"SELECT key_a AS {k} FROM diff WHERE change = 'update'")
        n_unchanged = con.sql("SELECT count(*) FROM diff WHERE change = 'unchanged'").fetchall()
        dup_a = key_list(f"SELECT {k} FROM a WHERE _rn > 1")
        dup_b = key_list(f"SELECT {k} FROM b WHERE _rn > 1")

        changes = None
        if records and (new or deleted or updated):
            # columns whose type changed in a way that can't be compared are written
            # as text, so the previous and current rows fit one schema
            as_text = ", ".join(f"{_quote(c)}::VARCHAR AS {_quote(c)}" for c in incomparable)
            replace = f" REPLACE ({as_text})" if as_text else ""

            def part(table, change, key_side, change_type, group, image):
                return f"""SELECT *{replace}, '{change_type}' AS _change_type,
                    {group} AS _group, {image} AS _image
                FROM {table} WHERE _rn = 1 AND {k} IN (
                    SELECT {key_side} FROM diff WHERE change = '{change}')"""

            # current columns first (the first part is from the current file), then
            # any only in the previous file
            parts = [
                part("b", "insert", "key_b", "insert", 0, 0),
                part("a", "delete", "key_a", "delete", 1, 0),
                part("a", "update", "key_a", "update_preimage", 2, 0),
                part("b", "update", "key_b", "update_postimage", 2, 1),
            ]
            # ordered by type, then key, preimage first
            relation = con.sql(
                f"""SELECT * EXCLUDE (_rn, _group, _image), ?::TIMESTAMPTZ AS _commit_timestamp
                FROM ({" UNION ALL BY NAME ".join(parts)})
                ORDER BY _group, {k}, _image""",
                params=[timestamp or datetime.now(tz=UTC)],
            )
            changes = relation.to_arrow_table()
            geo = _records_geo_metadata(path_a, path_b)
            if geo:
                changes = changes.replace_schema_metadata({"geo": json.dumps(geo)})

    # records (not rows): an updated record is counted once
    summary = {
        "inserted": len(new),
        "deleted": len(deleted),
        "updated": len(updated),
        "unchanged": n_unchanged[0][0],
        "duplicates": len(dup_a) + len(dup_b),
        "inserted_keys": new,
        "deleted_keys": deleted,
        "updated_keys": updated,
        "data_changed": bool(new or deleted or updated),
        "ignored_columns": ignored,
    }
    if keys.allow_duplicates:
        # one key per record not compared (only the first record with a key is),
        # by file, so the two lists add up to duplicates
        summary["duplicate_keys_previous"] = dup_a
        summary["duplicate_keys_current"] = dup_b
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
            # path of the changed records, relative to the table's folder
            # (keep_change_records only)
            ("records_file", string),
            ("bcdata_version", string),
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
            pq.write_table(changes, records_local)
        log_local = os.path.join(tmpdir, "change_log.parquet")
        schema = change_log_schema(key_type(new_path, keys))
        log_table = append_change_log(change_log, row, schema, log_local)
        summary = change_summary(table, log_table)
        if records_local:
            dest.upload(records_local, *records_parts)
        try:
            dest.replace(log_local, *change_log_parts(table), e_tag=log_e_tag)
        except ConcurrentWriteError:
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
