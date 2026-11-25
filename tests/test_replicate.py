"""Tests for scripts/replicate.py, against a local destination folder and an
in-memory object store (obstore MemoryStore, which like S3 or Azure has no folders).

Downloads are faked: each test provides the GeoDataFrame a download returns, written
with the "bcdata" footer metadata that bcdata.to_parquet adds.
"""

import importlib.util
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import geopandas as gpd
import obstore
import pyarrow
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point

pytest.importorskip("fit_changedetector")

_spec = importlib.util.spec_from_file_location(
    "replicate", Path(__file__).resolve().parent.parent / "scripts" / "replicate.py"
)
replicate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replicate)

TABLE = "whse_test.keyed"
HASHED = "whse_test.hashed"
TABLE_METADATA = {TABLE: {"primary_key": "id"}, HASHED: {"sort_key": "id"}}
CONFIG = {
    TABLE: {"schedule": "weekly", "keep_change_records": True},
    HASHED: {"schedule": "weekly", "hash_columns": ["name", "geometry"], "ignore_columns": ["id"]},
}
NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)


def gdf(ids, names, geoms=None, values=None):
    data = {"id": ids, "name": names}
    if values is not None:
        data["value"] = values
    if geoms is None:
        geoms = [Point(1000000 + i, 1000000) for i in ids]
    return gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:3005")


class FakeDownload:
    """Stands in for bcdata.to_parquet, writing the GeoDataFrame set for each table"""

    def __init__(self):
        self.data = {}

    def __call__(self, table, out_path):
        df = self.data[table]
        df.to_parquet(out_path)
        meta = pq.read_metadata(out_path).metadata
        table_ = pq.read_table(out_path)
        content_hash = "sha256:" + str(hash(df.to_json()))
        bcdata_meta = {
            "content_hash": content_hash,
            "schema_hash": "sha256:schema",
            "feature_count": len(df),
            "downloaded": NOW.isoformat(),
            "bcdata_version": "test",
        }
        pq.write_table(
            table_.replace_schema_metadata({**meta, b"bcdata": json.dumps(bcdata_meta)}),
            out_path,
        )
        return out_path


@pytest.fixture(params=["local", "memory"])
def dest(request, tmp_path):
    if request.param == "memory":
        return replicate.Destination("memory://mirror", store=obstore.store.MemoryStore())
    return replicate.Destination(str(tmp_path / "mirror"))


@pytest.fixture
def download():
    return FakeDownload()


def run(dest, download, now=NOW, **kwargs):
    return replicate.run(
        dest, CONFIG, download=download, table_metadata=TABLE_METADATA, now=now, **kwargs
    )


def read_table(dest, *parts):
    return pq.read_table(io.BytesIO(dest.read_bytes(*parts)))


def read_gdf(dest, *parts):
    return gpd.read_parquet(io.BytesIO(dest.read_bytes(*parts)))


def listed(dest, *parts):
    """Paths of the files under a folder of the destination"""
    prefix = dest.key(*parts)
    return [o["path"] for o in obstore.list(dest.store, prefix=prefix).collect()]


def changes(dest, table):
    """Rows of a table's change log"""
    return read_table(dest, "changes", f"{table}.parquet").to_pylist()


def records(dest, record):
    """The changed records file of a change log row"""
    return read_gdf(dest, *record["records_file"].split("/"))


# ---- configuration ----


def test_bundled_config_is_valid():
    config = replicate.load_config()
    assert "whse_fish.pscis_assessment_svw" in config


@pytest.mark.parametrize(
    "toml, message",
    [
        ('["whse_test.keyed"]\nschedule = "hourly"', "schedule must be one of"),
        ('["whse_test.keyed"]\nschedule = "weekly"\nhash_columns = ["a"]', "remove hash_columns"),
        ('["whse_test.hashed"]\nschedule = "weekly"', "add hash_columns"),
        ('["whse_test.hashed"]\nschedule = "weekly"\nhash_columns = []', "list of column names"),
        ('["whse_test.hashed"]\nschedule = "weekly"\nhash_columns = ["A"]', "lowercase"),
        ('["whse_test.keyed"]\nschedule = "weekly"\nquery = "x"', "Unknown field"),
        ('["whse_test.keyed"]\nschedule = "weekly"\nkeep_change_records = "yes"', "true or false"),
        ('["whse_test.keyed"]\nschedule = "weekly"\nrekey = 1', "true or false"),
        ('["WHSE_TEST.KEYED"]\nschedule = "weekly"', "lowercase"),
        (
            '["whse_test.keyed"]\nschedule = "weekly"\nallow_duplicates = true',
            "only with hash_columns",
        ),
        (
            '["whse_test.hashed"]\nschedule = "weekly"\nhash_columns = ["a"]\nallow_duplicates = 1',
            "true or false",
        ),
        ('["whse_test.keyed"]\nschedule = "weekly"\nignore_columns = ["id"]', "primary key"),
        (
            '["whse_test.hashed"]\nschedule = "weekly"\nhash_columns = ["a"]\nignore_columns = ["a"]',
            "both hash_columns and ignore_columns",
        ),
    ],
)
def test_config_errors(toml, message):
    with pytest.raises(replicate.ConfigError, match=message):
        replicate.load_config(toml, table_metadata=TABLE_METADATA)


def test_is_due():
    fields = {"schedule": "weekly"}
    assert replicate.is_due(fields, None, NOW)
    checked = {"checked": (NOW - timedelta(days=3)).isoformat()}
    assert not replicate.is_due(fields, checked, NOW)
    # checked a little after the start of last week's run: still due this week
    checked = {"checked": (NOW - timedelta(days=7) + timedelta(minutes=20)).isoformat()}
    assert replicate.is_due(fields, checked, NOW)


# ---- replication ----


def test_initial_unchanged_changed(dest, download):
    download.data = {
        TABLE: gdf([1, 2, 3], ["a", "b", "c"]),
        HASHED: gdf([1, 2], ["a", "b"]),
    }
    assert run(dest, download) == {}
    assert dest.exists(f"{TABLE}.parquet")
    state = dest.read_json("state.json")
    assert state[TABLE]["status"] == "initial"
    assert changes(dest, TABLE)[0]["kind"] == "initial"

    # not due again within the week
    later = NOW + timedelta(days=1)
    assert run(dest, download, now=later) == {}
    assert dest.read_json("state.json")[TABLE]["checked"] == state[TABLE]["checked"]

    # due, same data: no new change record
    later = NOW + timedelta(days=7)
    assert run(dest, download, now=later) == {}
    assert dest.read_json("state.json")[TABLE]["status"] == "unchanged"
    assert len(changes(dest, TABLE)) == 1

    # changed: 2 modified, 3 deleted, 4 added
    download.data[TABLE] = gdf([1, 2, 4], ["a", "B", "d"])
    later = NOW + timedelta(days=14)
    assert run(dest, download, now=later) == {}
    record = changes(dest, TABLE)[-1]
    assert record["kind"] == "changed"
    assert record["data_changed"]
    assert record["_commit_timestamp"] == later
    assert (record["key_column"], record["key_generated"]) == ("id", False)
    assert record["hash_columns"] is None
    assert not record["allow_duplicates"]
    assert (record["previous_feature_count"], record["current_feature_count"]) == (3, 3)
    assert (record["inserted"], record["deleted"], record["updated"]) == (1, 1, 1)
    assert record["unchanged"] == 1
    assert record["inserted_keys"] == [4]
    assert record["deleted_keys"] == [3]
    assert record["updated_keys"] == [2]
    assert record["records_file"] == f"changes/{TABLE}/20261208T060000Z.parquet"
    rows = records(dest, record)
    assert list(rows.columns) == ["id", "name", "geometry", "_change_type", "_commit_timestamp"]
    # ordered by type, then key, preimage before postimage
    assert list(zip(rows["_change_type"], rows["id"], rows["name"])) == [
        ("insert", 4, "d"),
        ("delete", 3, "c"),
        ("update_preimage", 2, "b"),
        ("update_postimage", 2, "B"),
    ]
    assert (rows["_commit_timestamp"] == later).all()
    # the mirror is the new download
    assert len(read_gdf(dest, f"{TABLE}.parquet")) == 3


def test_generated_key(dest, download):
    download.data = {HASHED: gdf([1, 2, 3], ["a", "b", "c"])}
    run(dest, download, tables=[HASHED])
    # ids regenerated, one record renamed (hash changes: deleted + new), one moved
    download.data[HASHED] = gdf(
        [11, 12, 13],
        ["a", "x", "c"],
        geoms=[Point(1000001, 1000000), Point(1000002, 1000000), Point(5, 5)],
    )
    assert run(dest, download, now=NOW + timedelta(days=7), tables=[HASHED]) == {}
    record = changes(dest, HASHED)[-1]
    assert record["key_generated"]
    assert record["key_column"] == replicate.HASH_KEY
    assert record["hash_columns"] == ["name", "geometry"]
    # a unchanged (only its regenerated id differs), b->x and c moved: deleted + new
    assert record["unchanged"] == 1
    assert (record["inserted"], record["deleted"]) == (2, 2)
    # the mirror file has no generated key
    assert replicate.HASH_KEY not in read_gdf(dest, f"{HASHED}.parquet").columns
    # keep_change_records defaults to false: keys (hashes) recorded, but no changed records
    assert [len(record[f"{t}_keys"]) for t in ("inserted", "deleted", "updated")] == [2, 2, 0]
    assert all(isinstance(k, str) for k in record["inserted_keys"])
    assert record["records_file"] is None
    assert listed(dest, "changes", HASHED) == []
    assert record["duplicate_keys_previous"] is None
    # the log has a row for each change, the initial load first
    assert [r["kind"] for r in changes(dest, HASHED)] == ["initial", "changed"]
    assert changes(dest, HASHED)[0]["inserted_keys"] is None


def test_regenerated_id_not_ignored_by_default(dest, download):
    # without ignore_columns, a regenerated id is a real attribute change
    config = {HASHED: {"schedule": "weekly", "hash_columns": ["name", "geometry"]}}
    download.data = {HASHED: gdf([1, 2], ["a", "b"])}
    kwargs = {"download": download, "table_metadata": TABLE_METADATA}
    replicate.run(dest, config, now=NOW, **kwargs)
    download.data[HASHED] = gdf(
        [11, 12], ["a", "b"], geoms=[Point(1000001, 1000000), Point(1000002, 1000000)]
    )
    replicate.run(dest, config, now=NOW + timedelta(days=7), **kwargs)
    assert changes(dest, HASHED)[-1]["updated"] == 2


def test_generated_key_null_geometry(dest, download):
    download.data = {HASHED: gdf([1, 2], ["a", "b"], geoms=[Point(1, 1), None])}
    run(dest, download, tables=[HASHED])
    download.data[HASHED] = gdf([1, 2], ["a", "c"], geoms=[Point(1, 1), None])
    run(dest, download, now=NOW + timedelta(days=7), tables=[HASHED])
    record = changes(dest, HASHED)[-1]
    assert record["dropped_null_geometry_previous"] == 1
    assert record["dropped_null_geometry_current"] == 1


def test_int_float_flip(dest, download):
    # bcdata writes a numeric column as int64 only when every value is whole
    download.data = {TABLE: gdf([1, 2], ["a", "b"], values=[1, 2])}
    run(dest, download, tables=[TABLE])
    download.data[TABLE] = gdf([1, 2], ["a", "b"], values=[1.0, 2.5])
    assert run(dest, download, now=NOW + timedelta(days=7), tables=[TABLE]) == {}
    record = changes(dest, TABLE)[-1]
    assert json.loads(record["columns_retyped"]) == {"value": ["int64", "double"]}
    assert record["columns_added"] == record["columns_removed"] == []
    assert record["updated"] == 1
    assert record["unchanged"] == 1


def test_failure_does_not_stop_other_tables(dest, download):
    download.data = {HASHED: gdf([1], ["a"])}  # no data for TABLE: its download fails
    failures = run(dest, download)
    assert list(failures) == [TABLE]
    state = dest.read_json("state.json")
    assert HASHED in state
    assert TABLE not in state


def test_download_check_error_retried(dest, download):
    calls = []

    def flaky(table, out_path):
        calls.append(table)
        if len(calls) == 1:
            raise replicate.DownloadCheckError("edited during download")
        return download(table, out_path)

    download.data = {TABLE: gdf([1], ["a"])}
    assert run(dest, flaky, tables=[TABLE]) == {}
    assert len(calls) == 2


def test_unknown_table(dest, download):
    with pytest.raises(replicate.ConfigError, match="Not in replication.toml"):
        run(dest, download, tables=["whse_test.other"])


def test_modified_geometry_keeps_previous(dest, download):
    download.data = {TABLE: gdf([1], ["a"], geoms=[Point(1, 1)])}
    run(dest, download, tables=[TABLE])
    download.data[TABLE] = gdf([1], ["a"], geoms=[Point(2, 2)])
    run(dest, download, now=NOW + timedelta(days=7), tables=[TABLE])
    rows = records(dest, changes(dest, TABLE)[-1]).set_index("_change_type")
    assert rows.loc["update_preimage", "geometry"] == Point(1, 1)
    assert rows.loc["update_postimage", "geometry"] == Point(2, 2)


def test_duplicates_allowed(dest, download):
    # two records with the same hashed values can't be paired between files:
    # the first is compared, the second only counted
    config = {HASHED: {**CONFIG[HASHED], "allow_duplicates": True, "keep_change_records": True}}
    kwargs = {"download": download, "table_metadata": TABLE_METADATA, "tables": [HASHED]}
    download.data = {HASHED: gdf([1, 2, 3], ["a", "a", "b"], geoms=[Point(1, 1)] * 3)}
    assert replicate.run(dest, config, now=NOW, **kwargs) == {}
    download.data[HASHED] = gdf([1, 2, 3], ["a", "a", "c"], geoms=[Point(1, 1)] * 3)
    assert replicate.run(dest, config, now=NOW + timedelta(days=7), **kwargs) == {}
    record = changes(dest, HASHED)[-1]
    assert record["allow_duplicates"]
    counts = ("inserted", "deleted", "updated", "unchanged", "duplicates")
    assert [record[c] for c in counts] == [1, 1, 0, 1, 2]
    # each file's uncompared duplicate is listed by its key (the hash of "a")
    assert len(record["duplicate_keys_previous"]) == len(record["duplicate_keys_current"]) == 1
    assert record["duplicate_keys_previous"] == record["duplicate_keys_current"]
    # duplicates are counted, not written as changes
    rows = records(dest, record)
    assert sorted(rows["_change_type"]) == ["delete", "insert"]


def test_duplicates_not_allowed(dest, download):
    # by default a generated key that isn't unique fails the table - on any
    # download, including the first
    download.data = {HASHED: gdf([1, 2], ["a", "a"], geoms=[Point(1, 1)] * 2)}
    failures = run(dest, download, tables=[HASHED])
    assert "DuplicateKeyError" in failures[HASHED]
    assert "allow_duplicates" in failures[HASHED]
    assert not dest.exists(f"{HASHED}.parquet")


def test_key_type_change_keeps_log(dest, download):
    # switching a table from its integer primary key to hash_columns: the earlier
    # rows' keys are converted to strings, and the history stays in one log
    download.data = {TABLE: gdf([1, 2], ["a", "b"])}
    run(dest, download, tables=[TABLE])
    download.data[TABLE] = gdf([1, 2], ["a", "c"])
    run(dest, download, now=NOW + timedelta(days=7), tables=[TABLE])
    assert changes(dest, TABLE)[-1]["updated_keys"] == [2]

    config = {TABLE: {"schedule": "weekly", "hash_columns": ["name"], "rekey": True}}
    kwargs = {"download": download, "table_metadata": {TABLE: {}}, "tables": [TABLE]}
    download.data[TABLE] = gdf([1, 2], ["a", "d"])
    assert replicate.run(dest, config, now=NOW + timedelta(days=14), **kwargs) == {}
    rows = changes(dest, TABLE)
    assert [r["key_column"] for r in rows] == ["id", "id", replicate.HASH_KEY]
    assert [r["rekeyed"] for r in rows] == [False, False, True]
    assert rows[1]["updated_keys"] == ["2"]
    assert len(rows[2]["inserted_keys"]) == len(rows[2]["deleted_keys"]) == 1
    log_schema = read_table(dest, "changes", f"{TABLE}.parquet").schema
    assert log_schema.field("updated_keys").type == pyarrow.list_(pyarrow.string())


def test_rekey(dest, download, caplog):
    # editing hash_columns changes every generated key: refused without rekey
    download.data = {HASHED: gdf([1, 2], ["a", "b"])}
    run(dest, download, tables=[HASHED])
    edited = {HASHED: {**CONFIG[HASHED], "hash_columns": ["name"]}}
    kwargs = {"download": download, "table_metadata": TABLE_METADATA, "tables": [HASHED]}
    download.data[HASHED] = gdf([1, 2], ["a", "c"])
    failures = replicate.run(dest, edited, now=NOW + timedelta(days=7), **kwargs)
    assert "RekeyError" in failures[HASHED]
    assert "rekey = true" in failures[HASHED]
    assert len(changes(dest, HASHED)) == 1
    assert read_gdf(dest, f"{HASHED}.parquet")["name"].tolist() == ["a", "b"]

    # accepted with rekey: the diff uses the new key for both files
    edited[HASHED]["rekey"] = True
    assert replicate.run(dest, edited, now=NOW + timedelta(days=7), **kwargs) == {}
    row = changes(dest, HASHED)[-1]
    assert row["rekeyed"]
    assert row["hash_columns"] == ["name"]
    assert (row["inserted"], row["deleted"], row["unchanged"]) == (1, 1, 1)

    # left set once the key is logged: a warning, not a rekey
    download.data[HASHED] = gdf([1, 2], ["a", "d"])
    assert replicate.run(dest, edited, now=NOW + timedelta(days=14), **kwargs) == {}
    assert not changes(dest, HASHED)[-1]["rekeyed"]
    assert "rekey is set but the key is unchanged" in caplog.text


def test_failed_log_leaves_no_records_file(dest, download, monkeypatch):
    download.data = {TABLE: gdf([1], ["a"])}
    run(dest, download, tables=[TABLE])
    download.data[TABLE] = gdf([1], ["b"])

    def fail(*args):
        raise OSError("log failed")

    monkeypatch.setattr(replicate, "append_change_log", fail)
    assert "log failed" in run(dest, download, now=NOW + timedelta(days=7), tables=[TABLE])[TABLE]
    assert listed(dest, "changes", TABLE) == []
    assert len(changes(dest, TABLE)) == 1
    # the mirror is unchanged
    assert read_gdf(dest, f"{TABLE}.parquet")["name"].tolist() == ["a"]


@pytest.mark.parametrize("log_exists", [True, False])
def test_change_log_written_meanwhile(download, monkeypatch, log_exists):
    # another run writes the change log between this run reading and replacing it
    # (MemoryStore: obstore's LocalStore has no conditional writes)
    dest = replicate.Destination("memory://mirror", store=obstore.store.MemoryStore())
    download.data = {TABLE: gdf([1], ["a"])}
    run(dest, download, tables=[TABLE])
    if not log_exists:
        dest.delete("changes", f"{TABLE}.parquet")
    read_change_log = replicate.read_change_log

    def read_then_overwrite(dest_, table, local_path):
        result = read_change_log(dest_, table, local_path)
        obstore.put(dest_.store, f"changes/{table}.parquet", b"another run")
        return result

    monkeypatch.setattr(replicate, "read_change_log", read_then_overwrite)
    download.data[TABLE] = gdf([1], ["b"])
    failures = run(dest, download, now=NOW + timedelta(days=7), tables=[TABLE])
    assert "ConcurrentWriteError" in failures[TABLE]
    # the other run's log is kept, this run's records file removed, mirror unchanged
    assert dest.read_bytes("changes", f"{TABLE}.parquet") == b"another run"
    assert listed(dest, "changes", TABLE) == []
    assert read_gdf(dest, f"{TABLE}.parquet")["name"].tolist() == ["a"]


def test_hash_changed_data_unchanged(dest, download):
    # eg a bcdata upgrade changes how files are written: every hash changes once
    download.data = {TABLE: gdf([1], ["a"])}
    run(dest, download, tables=[TABLE])
    data = dest.read_bytes(f"{TABLE}.parquet")
    t = pq.read_table(io.BytesIO(data))
    meta = pq.read_metadata(io.BytesIO(data)).metadata
    bcdata_meta = json.loads(meta[b"bcdata"])
    bcdata_meta["content_hash"] = "sha256:other"
    buf = io.BytesIO()
    pq.write_table(t.replace_schema_metadata({**meta, b"bcdata": json.dumps(bcdata_meta)}), buf)
    obstore.put(dest.store, f"{TABLE}.parquet", buf.getvalue())
    run(dest, download, now=NOW + timedelta(days=7), tables=[TABLE])
    record = changes(dest, TABLE)[-1]
    assert record["kind"] == "changed"
    assert not record["data_changed"]
    assert record["records_file"] is None


def test_destination_footer_only(dest):
    # the footer is read with range requests, not a download of the whole file
    buf = io.BytesIO()
    pq.write_table(
        pyarrow.table({"a": range(1000)}).replace_schema_metadata({"bcdata": '{"x": 1}'}), buf
    )
    obstore.put(dest.store, "t.parquet", buf.getvalue())
    assert dest.read_bcdata_metadata("t.parquet") == {"x": 1}
    assert dest.read_bcdata_metadata("missing.parquet") is None
    obstore.put(dest.store, "not.parquet", b"not a parquet file")
    with pytest.raises(ValueError, match="not a parquet file"):
        dest.read_bcdata_metadata("not.parquet")
