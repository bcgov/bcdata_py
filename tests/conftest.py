import functools

import pytest

import bcdata


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
