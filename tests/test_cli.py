import json
import os

from click.testing import CliRunner

import bcdata
from bcdata.cli import cli
from bcdata.database import Database

DB_URL = os.environ.get("BCDATA_TEST_DATABASE_URL")
DB_CONNECTION = Database(url=DB_URL)
AIRPORTS_PACKAGE = "bc-airports"
AIRPORTS_TABLE = "WHSE_IMAGERY_AND_BASE_MAPS.GSR_AIRPORTS_SVW"
TERRACE_QUERY = "AIRPORT_NAME='Terrace (Northwest Regional) Airport'"
VICTORIA_QUERY = "Victoria Harbour (Camel Point) Heliport"
BBOX_LL = "-123.396104,48.404465,-123.342588,48.425401"
RIVERS_TABLE = "whse_basemapping.fwa_rivers_poly"
ASSESSMENTS_TABLE = "whse_fish.pscis_assessment_svw"


def test_info_table():
    runner = CliRunner()
    expected_count = bcdata.get_count(AIRPORTS_TABLE)
    result = runner.invoke(cli, ["info", AIRPORTS_TABLE])
    assert result.exit_code == 0
    assert f'name": "{AIRPORTS_TABLE}"' in result.output
    assert f'"count": {expected_count}' in result.output


def test_info_package():
    runner = CliRunner()
    expected_count = bcdata.get_count(AIRPORTS_TABLE)
    result = runner.invoke(cli, ["info", AIRPORTS_PACKAGE])
    assert result.exit_code == 0
    assert f'name": "{AIRPORTS_TABLE}"' in result.output
    assert f'"count": {expected_count}' in result.output


def test_list():
    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert AIRPORTS_TABLE in result.output


def test_cat():
    runner = CliRunner()
    expected_count = bcdata.get_count(AIRPORTS_TABLE)
    result = runner.invoke(cli, ["cat", AIRPORTS_TABLE])
    assert result.exit_code == 0
    assert len(result.output.split("\n")) == expected_count + 1


def test_cat_query():
    runner = CliRunner()
    result = runner.invoke(cli, ["cat", AIRPORTS_TABLE, "--query", TERRACE_QUERY])
    assert result.exit_code == 0
    assert len(result.output.split("\n")) == 2


def test_cat_bounds_ll():
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "cat",
            AIRPORTS_TABLE,
            "--bounds",
            BBOX_LL,
            "--bounds_crs",
            "EPSG:4326",
        ],
    )
    assert result.exit_code == 0
    assert len(result.output.split("\n")) == 4


def test_dump():
    runner = CliRunner()
    result = runner.invoke(cli, ["dump", AIRPORTS_TABLE, "--count", 1])
    assert result.exit_code == 0
    assert json.loads(result.output)["type"] == "FeatureCollection"
    assert len(json.loads(result.output)["features"]) == 1


def test_dump_typed():
    """--typed writes dates as YYYY-MM-DD and whole numbers as integers"""
    runner = CliRunner()
    args = ["dump", ASSESSMENTS_TABLE, "--count", "20", "--sortby", "ASSESSMENT_ID"]
    args += ["--query", "ASSESSMENT_DATE IS NOT NULL"]
    raw = json.loads(runner.invoke(cli, args).output)["features"][0]["properties"]
    result = runner.invoke(cli, args + ["--typed"])
    assert result.exit_code == 0
    typed = json.loads(result.output)["features"][0]["properties"]
    assert raw["ASSESSMENT_DATE"].endswith("Z")
    assert typed["ASSESSMENT_DATE"] == raw["ASSESSMENT_DATE"][:10]
    assert isinstance(typed["ASSESSMENT_ID"], int)


def test_bc2pg():
    runner = CliRunner()
    result = runner.invoke(cli, ["bc2pg", AIRPORTS_TABLE, "--db_url", DB_URL])
    assert result.exit_code == 0
    assert AIRPORTS_TABLE.lower() in DB_CONNECTION.tables
    DB_CONNECTION.execute("drop table " + AIRPORTS_TABLE.lower())


def test_bc2pg_refresh():
    runner = CliRunner()
    r1 = runner.invoke(
        cli,
        [
            "bc2pg",
            AIRPORTS_TABLE,
            "--db_url",
            DB_URL,
            "--query",
            "AIRPORT_NAME='Terrace (Northwest Regional) Airport'",
        ],
    )
    r2 = runner.invoke(
        cli,
        [
            "bc2pg",
            AIRPORTS_TABLE,
            "--db_url",
            DB_URL,
            "--refresh",
            "--query",
            "AIRPORT_NAME='Victoria International Airport'",
        ],
    )
    q = DB_CONNECTION.query("select * from whse_imagery_and_base_maps.gsr_airports_svw")
    assert r1.exit_code == 0
    assert r2.exit_code == 0
    assert AIRPORTS_TABLE.lower() in DB_CONNECTION.tables
    assert len(q) == 1
    DB_CONNECTION.execute("drop table " + AIRPORTS_TABLE.lower())


def test_bc2pg_refresh_fails_table_does_not_exist():
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "bc2pg",
            AIRPORTS_TABLE,
            "--db_url",
            DB_URL,
            "--count",
            "1",
            "--refresh",
        ],
    )
    assert result.exit_code == 1


def test_dump_bounds_wrong_length():
    runner = CliRunner()
    for bounds in ["1 2 3", "1 2 3 4 5"]:
        result = runner.invoke(cli, ["dump", AIRPORTS_TABLE, "--bounds", bounds])
        assert result.exit_code == 2
        assert "not a valid bounding box" in result.output
