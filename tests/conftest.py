# pytest loads conftest.py automatically (the name is required). Fixtures defined
# here are available to every test in tests/ without importing them, and autouse
# fixtures run for every test without being requested.
import functools

import pytest

import bcdata
from bcdata.wfs import BCWFS


@pytest.fixture(autouse=True, scope="session")
def _cache_table_definition_lookups():
    """Reuse BCDC table_definition lookups across tests within a run.

    bcdata.bc2pg() calls bcdata.get_table_definition() on every invocation, and
    many tests in this suite call bc2pg() repeatedly against the same table
    (eg AIRPORTS_TABLE). The schema/comments returned don't change within a
    test run, so caching avoids redundant round trips to the BCDC API.
    """
    original = bcdata.get_table_definition
    bcdata.get_table_definition = functools.lru_cache(maxsize=None)(original)
    yield
    bcdata.get_table_definition = original


@pytest.fixture(autouse=True, scope="session")
def _cache_table_name_lookups():
    """Reuse BCDC package -> table name lookups across tests within a run.

    validate_name() asks BCDC for the table name of every package key it is given
    (eg "bc-airports"), once per get_data/get_count call. Tests of the lookup itself
    call bcdata.bcdc.get_table_name directly, so they still make the request.
    """
    original = bcdata.get_table_name
    bcdata.get_table_name = functools.lru_cache(maxsize=None)(original)
    yield
    bcdata.get_table_name = original


@pytest.fixture(autouse=True, scope="session")
def _cache_counts():
    """Reuse WFS feature counts across tests within a run.

    Every get_data/to_parquet/bc2pg call requests a count, and several tests also
    request one to compare against. Counts for the same table and filter don't
    change within a test run. Tests that patch get_count or an instance's
    _request_count still replace this cache.
    """
    original = BCWFS._request_count
    counts = {}

    def cached(self, table, query=None, bounds=None, bounds_crs=None, geom_column=None):
        key = (table, query, tuple(bounds) if bounds else None, bounds_crs, geom_column)
        if key not in counts:
            counts[key] = original(self, table, query, bounds, bounds_crs, geom_column)
        return counts[key]

    BCWFS._request_count = cached
    yield
    BCWFS._request_count = original


SMALL_PAGESIZE = 10


@pytest.fixture
def small_pages(monkeypatch):
    """Page WFS requests every SMALL_PAGESIZE features, instead of the service's 10,000.

    Paging can then be tested by downloading a few dozen features rather than
    more than 10,000.
    """
    init = BCWFS.__init__

    def small_init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self.pagesize = SMALL_PAGESIZE

    monkeypatch.setattr(BCWFS, "__init__", small_init)
    return SMALL_PAGESIZE
