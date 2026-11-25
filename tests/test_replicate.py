"""Tests for scripts/replicate.py, against a local destination folder.

Downloads are faked: each test provides the GeoDataFrame a download returns, written
with the "bcdata" footer metadata that bcdata.to_parquet adds.
"""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import geopandas as gpd
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
    TABLE: {"schedule": "weekly"},
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


@pytest.fixture
def dest(tmp_path):
    return replicate.Destination(str(tmp_path / "mirror"))


@pytest.fixture
def download():
    return FakeDownload()


def run(dest, download, now=NOW, **kwargs):
    return replicate.run(
        dest, CONFIG, download=download, table_metadata=TABLE_METADATA, now=now, **kwargs
    )


def changes(dest, table):
    root = Path(dest.path("changes", table))
    return [json.loads((d / "change.json").read_text()) for d in sorted(root.iterdir())]


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
    assert record["key"] == {
        "column": "id",
        "generated": False,
        "hash_columns": None,
        "allow_duplicates": False,
    }
    counts = record["counts"]
    assert (counts["insert"], counts["delete"], counts["update"]) == (1, 1, 1)
    assert counts["unchanged"] == 1
    folder = max(Path(dest.path("changes", TABLE)).iterdir())
    assert record["files"] == ["changes.parquet"]
    rows = gpd.read_parquet(folder / "changes.parquet")
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
    assert len(gpd.read_parquet(dest.path(f"{TABLE}.parquet"))) == 3


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
    assert record["key"]["generated"]
    counts = record["counts"]
    # a unchanged (only its regenerated id differs), b->x and c moved: deleted + new
    assert counts["unchanged"] == 1
    assert (counts["insert"], counts["delete"]) == (2, 2)
    # the mirror file has no generated key
    assert replicate.HASH_KEY not in gpd.read_parquet(dest.path(f"{HASHED}.parquet")).columns


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
    assert changes(dest, HASHED)[-1]["counts"]["update"] == 2


def test_generated_key_null_geometry(dest, download):
    download.data = {HASHED: gdf([1, 2], ["a", "b"], geoms=[Point(1, 1), None])}
    run(dest, download, tables=[HASHED])
    download.data[HASHED] = gdf([1, 2], ["a", "c"], geoms=[Point(1, 1), None])
    run(dest, download, now=NOW + timedelta(days=7), tables=[HASHED])
    record = changes(dest, HASHED)[-1]
    assert record["dropped_null_geometry"] == {"previous": 1, "current": 1}


def test_int_float_flip(dest, download):
    # bcdata writes a numeric column as int64 only when every value is whole
    download.data = {TABLE: gdf([1, 2], ["a", "b"], values=[1, 2])}
    run(dest, download, tables=[TABLE])
    download.data[TABLE] = gdf([1, 2], ["a", "b"], values=[1.0, 2.5])
    assert run(dest, download, now=NOW + timedelta(days=7), tables=[TABLE]) == {}
    record = changes(dest, TABLE)[-1]
    assert record["schema_changes"]["retyped"] == {"value": ["int64", "double"]}
    assert record["counts"]["update"] == 1
    assert record["counts"]["unchanged"] == 1


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
    folder = max(Path(dest.path("changes", TABLE)).iterdir())
    rows = gpd.read_parquet(folder / "changes.parquet").set_index("_change_type")
    assert rows.loc["update_preimage", "geometry"] == Point(1, 1)
    assert rows.loc["update_postimage", "geometry"] == Point(2, 2)


def test_duplicates_allowed(dest, download):
    # two records with the same hashed values can't be paired between files:
    # the first is compared, the second only counted
    config = {HASHED: {**CONFIG[HASHED], "allow_duplicates": True}}
    kwargs = {"download": download, "table_metadata": TABLE_METADATA, "tables": [HASHED]}
    download.data = {HASHED: gdf([1, 2, 3], ["a", "a", "b"], geoms=[Point(1, 1)] * 3)}
    assert replicate.run(dest, config, now=NOW, **kwargs) == {}
    download.data[HASHED] = gdf([1, 2, 3], ["a", "a", "c"], geoms=[Point(1, 1)] * 3)
    assert replicate.run(dest, config, now=NOW + timedelta(days=7), **kwargs) == {}
    record = changes(dest, HASHED)[-1]
    assert record["key"]["allow_duplicates"]
    assert record["counts"] == {
        "insert": 1,
        "delete": 1,
        "update": 0,
        "unchanged": 1,
        "duplicate": 2,
    }
    # duplicates are counted, not written as changes
    folder = max(Path(dest.path("changes", HASHED)).iterdir())
    rows = gpd.read_parquet(folder / "changes.parquet")
    assert sorted(rows["_change_type"]) == ["delete", "insert"]


def test_duplicates_not_allowed(dest, download):
    # by default a generated key that isn't unique fails the table - on any
    # download, including the first
    download.data = {HASHED: gdf([1, 2], ["a", "a"], geoms=[Point(1, 1)] * 2)}
    failures = run(dest, download, tables=[HASHED])
    assert "DuplicateKeyError" in failures[HASHED]
    assert "allow_duplicates" in failures[HASHED]
    assert not dest.exists(f"{HASHED}.parquet")


def test_hash_changed_data_unchanged(dest, download):
    # eg a bcdata upgrade changes how files are written: every hash changes once
    download.data = {TABLE: gdf([1], ["a"])}
    run(dest, download, tables=[TABLE])
    path = dest.path(f"{TABLE}.parquet")
    t = pq.read_table(path)
    meta = pq.read_metadata(path).metadata
    bcdata_meta = json.loads(meta[b"bcdata"])
    bcdata_meta["content_hash"] = "sha256:other"
    pq.write_table(t.replace_schema_metadata({**meta, b"bcdata": json.dumps(bcdata_meta)}), path)
    run(dest, download, now=NOW + timedelta(days=7), tables=[TABLE])
    record = changes(dest, TABLE)[-1]
    assert record["kind"] == "changed"
    assert not record["data_changed"]
    assert record["files"] == []
