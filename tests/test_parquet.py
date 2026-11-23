import json

import duckdb
import geopandas as gpd
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

import bcdata
from bcdata.cli import cli

AIRPORTS_PACKAGE = "bc-airports"
AIRPORTS_TABLE = "WHSE_IMAGERY_AND_BASE_MAPS.GSR_AIRPORTS_SVW"
ASSESSMENTS_TABLE = "WHSE_FISH.PSCIS_ASSESSMENT_SVW"
TERRACE_QUERY = "AIRPORT_NAME='Terrace (Northwest Regional) Airport'"


@pytest.fixture(scope="module")
def airports(tmp_path_factory):
    out = tmp_path_factory.mktemp("pq") / "airports.parquet"
    bcdata.to_parquet(AIRPORTS_PACKAGE, str(out))
    return out


def test_geoparquet_metadata(airports):
    md = pq.read_metadata(airports)
    geo = json.loads(md.metadata[b"geo"])
    assert geo["version"] == "2.0.0"
    assert geo["primary_column"] == "geometry"
    col = geo["columns"]["geometry"]
    assert col["encoding"] == "WKB"
    assert col["geometry_types"] == ["Point"]
    assert col["crs"]["id"] == {"authority": "EPSG", "code": 3005}
    assert len(col["bbox"]) == 4


def test_native_geometry_type(airports):
    md = pq.read_metadata(airports)
    schema = pq.read_schema(airports)
    i = schema.get_field_index("geometry")
    assert str(md.schema.column(i).logical_type).startswith("Geometry")
    # row group bbox statistics are written for the native type
    assert md.row_group(0).column(i).geo_statistics.xmin is not None


def test_provenance_metadata(airports):
    meta = json.loads(pq.read_metadata(airports).metadata[b"bcdata"])
    assert meta["source"] == AIRPORTS_TABLE
    assert meta["feature_count"] == bcdata.get_count(AIRPORTS_TABLE)
    assert meta["columns"]["AIRPORT_NAME"]
    assert "NUMBER_OF_RUNWAYS" in meta["integer_columns_inferred"]
    assert "SEQUENCE_ID" in meta["integer_columns_inferred"]
    assert "ELEVATION" not in meta["integer_columns_inferred"]


def test_column_types(airports):
    schema = pq.read_schema(airports)
    assert str(schema.field("NUMBER_OF_RUNWAYS").type) == "int64"
    assert str(schema.field("ELEVATION").type) == "double"
    assert str(schema.field("DATE_UPDATED").type) == "date32[day]"
    assert str(schema.field("AIRPORT_NAME").type) == "string"


def test_readers(airports):
    df = gpd.read_parquet(airports)
    assert df.crs.to_epsg() == 3005
    assert len(df) == bcdata.get_count(AIRPORTS_TABLE)
    n = duckdb.sql(f"SELECT count(*) FROM '{airports}' WHERE geometry IS NOT NULL").fetchall()
    assert n[0][0] == len(df)


def test_query_lowercase(tmp_path):
    out = tmp_path / "terrace.parquet"
    bcdata.to_parquet(AIRPORTS_TABLE, str(out), query=TERRACE_QUERY, lowercase=True)
    df = gpd.read_parquet(out)
    assert len(df) == 1
    assert df["airport_name"].iloc[0] == "Terrace (Northwest Regional) Airport"


def test_paged_row_groups(tmp_path):
    """multiple WFS pages are combined, sorted and split into row groups"""
    out = tmp_path / "pscis.parquet"
    bcdata.to_parquet(ASSESSMENTS_TABLE, str(out), count=12000, row_group_size=5000)
    md = pq.read_metadata(out)
    assert md.num_rows == 12000
    assert [md.row_group(i).num_rows for i in range(md.num_row_groups)] == [5000, 5000, 2000]
    df = gpd.read_parquet(out)
    assert df["ASSESSMENT_ID"].is_unique


def test_cli(tmp_path):
    out = tmp_path / "terrace.parquet"
    runner = CliRunner()
    result = runner.invoke(
        cli, ["parquet", AIRPORTS_TABLE, "--query", TERRACE_QUERY, "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert len(gpd.read_parquet(out)) == 1


NO_MATCH_QUERY = "AIRPORT_NAME='no such airport'"


def test_no_features(tmp_path):
    """a filter matching nothing still writes a valid, empty file with the full schema"""
    out = tmp_path / "empty.parquet"
    bcdata.to_parquet(AIRPORTS_TABLE, str(out), query=NO_MATCH_QUERY)
    md = pq.read_metadata(out)
    assert md.num_rows == 0
    geo = json.loads(md.metadata[b"geo"])
    assert geo["columns"]["geometry"]["geometry_types"] == []
    meta = json.loads(md.metadata[b"bcdata"])
    assert meta["feature_count"] == 0
    assert meta["integer_columns_inferred"] == []
    schema = pq.read_schema(out)
    # with no values to infer from, numeric columns are not assumed to be integers
    assert str(schema.field("NUMBER_OF_RUNWAYS").type) == "double"
    assert str(schema.field("SEQUENCE_ID").type) == "double"
    assert "AIRPORT_NAME" in schema.names
    assert str(md.schema.column(schema.get_field_index("geometry")).logical_type).startswith(
        "Geometry"
    )
    df = gpd.read_parquet(out)
    assert len(df) == 0
    assert df.crs.to_epsg() == 3005
    assert duckdb.sql(f"SELECT count(*) FROM '{out}'").fetchall()[0][0] == 0


def test_integer_inference():
    """integer columns need at least one value, and only whole numbers"""

    from bcdata.parquet import _Columns

    schema = {
        "properties": {"ID": "decimal", "MEASURE": "decimal", "EMPTY": "decimal", "NAME": "string"}
    }
    columns = _Columns(schema, lowercase=False)
    columns.arrays(
        [
            {"ID": 1, "MEASURE": 2, "EMPTY": None, "NAME": "a"},
            {"ID": 2, "MEASURE": 2.5, "EMPTY": None, "NAME": "b"},
            {"ID": None, "MEASURE": None, "EMPTY": None, "NAME": None},
        ]
    )
    assert columns.integral & columns.has_values == {"ID"}


def test_string_values_coerced():
    """non-string JSON values in a string column are written as strings"""

    from bcdata.parquet import _Columns

    columns = _Columns({"properties": {"CODE": "string"}}, lowercase=False)
    (arr,) = columns.arrays([{"CODE": "a"}, {"CODE": 5}, {"CODE": None}, {"CODE": True}])
    assert arr.to_pylist() == ["a", "5", None, "true"]


def test_row_group_size_invalid(tmp_path):
    with pytest.raises(ValueError):
        bcdata.to_parquet(AIRPORTS_TABLE, str(tmp_path / "x.parquet"), row_group_size=0)
    result = CliRunner().invoke(cli, ["parquet", AIRPORTS_TABLE, "--row-group-size", "0"])
    assert result.exit_code == 2


def test_check_download(tmp_path):
    """count mismatches and duplicated primary keys fail the check"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from bcdata.parquet import DownloadCheckError, _check_download

    path = str(tmp_path / "staging.parquet")
    pq.write_table(pa.table({"ID": [1, 2, 2, None]}), path)
    with duckdb.connect() as con:
        with pytest.raises(DownloadCheckError, match="expected 5"):
            _check_download(con, path, "T", 4, 5, None)
        with pytest.raises(DownloadCheckError, match="1 duplicated"):
            _check_download(con, path, "T", 4, 4, "ID")
        # no primary key known - only the count is checked
        _check_download(con, path, "T", 4, 4, None)


def test_check_failure_writes_nothing(tmp_path, monkeypatch):
    """a failed check raises before anything is written, --no-check overrides"""
    from bcdata.parquet import DownloadCheckError
    from bcdata.wfs import BCWFS

    get_count = BCWFS.get_count
    # report one more feature than will be received, as if one was added mid-download
    monkeypatch.setattr(BCWFS, "get_count", lambda self, *a, **k: get_count(self, *a, **k) + 1)
    out = tmp_path / "terrace.parquet"
    with pytest.raises(DownloadCheckError):
        bcdata.to_parquet(AIRPORTS_TABLE, str(out), query=TERRACE_QUERY)
    assert list(tmp_path.iterdir()) == []

    runner = CliRunner()
    args = ["parquet", AIRPORTS_TABLE, "--query", TERRACE_QUERY, "-o", str(out)]
    result = runner.invoke(cli, args)
    assert isinstance(result.exception, DownloadCheckError)
    result = runner.invoke(cli, args + ["--no-check"])
    assert result.exit_code == 0, result.output
    assert len(gpd.read_parquet(out)) == 1


# --- schema and content hashes ---


def _hash_table():
    import geoarrow.pyarrow as ga
    import pyarrow as pa

    from bcdata.parquet import GEOMETRY_COLUMN

    geom = ga.wkb()
    n = 300
    schema = pa.schema(
        [
            pa.field("ID", pa.int64()),
            pa.field("NAME", pa.string()),
            pa.field("UPDATED", pa.date32()),
            pa.field("AREA", pa.float64()),
            pa.field(GEOMETRY_COLUMN, geom),
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array(range(n), pa.int64()),
            pa.array([None if i % 7 == 0 else f"name {i}" for i in range(n)]),
            pa.array([None if i % 5 == 0 else i for i in range(n)], pa.int32()).cast(pa.date32()),
            pa.array([i / 3 if i % 11 else None for i in range(n)]),
            pa.ExtensionArray.from_storage(
                geom, pa.array([bytes([i % 256]) * (i % 13) for i in range(n)], pa.binary())
            ),
        ],
        schema=schema,
    )
    return pa, schema, table


def _content_hash(chunks, schema):

    from bcdata.parquet import _ContentHasher, _schema_hash

    hasher = _ContentHasher(_schema_hash(schema), list(schema))
    for chunk in chunks:
        hasher.update(chunk)
    return hasher.hexdigest()


def test_content_hash_independent_of_chunking():
    _, schema, table = _hash_table()
    whole = _content_hash([table], schema)
    assert whole.startswith("sha256:")
    assert whole == _content_hash(
        [table.slice(0, 100), table.slice(100, 77), table.slice(177)], schema
    )
    assert whole == _content_hash([table.slice(i, 1) for i in range(len(table))], schema)


def test_content_hash_detects_changes():
    pa, schema, table = _hash_table()
    whole = _content_hash([table], schema)
    names = table.column("NAME").to_pylist()
    # null and empty string are different values
    empty = table.set_column(1, "NAME", pa.array(["" if v is None else v for v in names]))
    assert _content_hash([empty], schema) != whole
    # one changed value
    areas = table.column("AREA").to_pylist()
    areas[150] = 1.0
    assert _content_hash([table.set_column(3, "AREA", pa.array(areas))], schema) != whole
    # same rows in a different order
    assert _content_hash([table.take(list(range(len(table)))[::-1])], schema) != whole


def test_schema_hash():
    import pyarrow as pa

    from bcdata.parquet import _schema_hash

    _, schema, _ = _hash_table()
    assert _schema_hash(schema) == _schema_hash(pa.schema(list(schema)))
    assert _schema_hash(schema) != _schema_hash(
        pa.schema([pa.field("ID", pa.float64())] + list(schema)[1:])
    )
    assert _schema_hash(schema) != _schema_hash(pa.schema(list(schema)[:-2] + list(schema)[-1:]))


def test_hashes_in_metadata(airports, tmp_path):
    # live: hashes are recorded, and do not depend on how the file is encoded
    meta = json.loads(pq.read_metadata(airports).metadata[b"bcdata"])
    out = tmp_path / "airports_again.parquet"
    bcdata.to_parquet(AIRPORTS_PACKAGE, str(out), row_group_size=50, compression="snappy")
    again = json.loads(pq.read_metadata(out).metadata[b"bcdata"])
    assert meta["schema_hash"].startswith("sha256:")
    assert again["schema_hash"] == meta["schema_hash"]
    assert again["content_hash"] == meta["content_hash"]
    assert pq.read_metadata(out).num_row_groups > pq.read_metadata(airports).num_row_groups


def test_single_request_is_sorted(tmp_path, monkeypatch):
    # live: a download that fits in one request is still sorted, so features
    # arrive in the same order every time (keeping content_hash stable)
    from bcdata.wfs import BCWFS

    urls = []
    request = BCWFS._request_featurecollection

    def record(self, url, silent=False):
        urls.append(url)
        return request(self, url, silent=silent)

    monkeypatch.setattr(BCWFS, "_request_featurecollection", record)
    bcdata.to_parquet(AIRPORTS_TABLE, str(tmp_path / "airports.parquet"))
    assert len(urls) == 1
    assert "sortby=" in urls[0]
