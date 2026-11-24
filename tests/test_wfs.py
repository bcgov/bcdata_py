import pytest
import requests
import requests_mock
import stamina
from geopandas.geodataframe import GeoDataFrame

import bcdata

AIRPORTS_PACKAGE = "bc-airports"
AIRPORTS_TABLE = "WHSE_IMAGERY_AND_BASE_MAPS.GSR_AIRPORTS_SVW"
UTMZONES_KEY = "utm-zones-of-british-columbia"
BEC_KEY = "biogeoclimatic-ecosystem-classification-bec-map"
ASSESSMENTS_TABLE = "whse_fish.pscis_assessment_svw"
GLACIERS_TABLE = "whse_basemapping.fwa_glaciers_poly"
STREAMS_TABLE = "whse_basemapping.fwa_stream_networks_sp"
WELLS_TABLE = "whse_water_management.gw_water_wells_wrbc_svw"


def _wfs_without_network():
    """A BCWFS for testing request methods, created without requesting capabilities"""
    WFS = bcdata.wfs.BCWFS.__new__(bcdata.wfs.BCWFS)
    WFS.wfs_url = "https://openmaps.gov.bc.ca/geo/pub/wfs"
    WFS.ows_url = "https://openmaps.gov.bc.ca/geo/pub/ows?service=WFS&request=Getcapabilities"
    WFS.request_headers = {"User-Agent": "bcdata.py tests"}
    return WFS


@pytest.fixture
def no_retries():
    """Deactivate retries for tests that mock failing responses, so they do not wait.

    Only these tests: live tests run with retries active, as bcdata is used.
    """
    stamina.set_active(False)
    yield
    stamina.set_active(True)


def test_http_error_502(no_retries):
    # request a feature page directly, so the test does not depend on cached
    # capabilities (BCWFS() would otherwise request them, and get the mocked error)
    WFS = _wfs_without_network()
    with requests_mock.mock() as m:
        m.get(requests_mock.ANY, status_code=502)
        with pytest.raises(requests.HTTPError):
            WFS._request(WFS.wfs_url)


def test_http_error_404(no_retries):
    WFS = _wfs_without_network()
    with requests_mock.mock() as m:
        m.get(requests_mock.ANY, status_code=404)
        with pytest.raises(bcdata.wfs.ServiceException):
            WFS._request(WFS.wfs_url)


def _http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(response=response)


def test_is_retryable_5xx_and_429():
    """5xx (presumed transient service errors) and 429 (rate limited) are retried"""
    for code in (429, 500, 502, 503, 504):
        assert bcdata.wfs._is_retryable(_http_error(code)) is True


def test_is_retryable_4xx_not_retried():
    """4xx client errors (other than 429) are not retried"""
    for code in (400, 401, 404):
        assert bcdata.wfs._is_retryable(_http_error(code)) is False


def test_is_retryable_connection_and_timeout_errors():
    """Connection/timeout failures are also worth retrying, not just bad HTTP responses"""
    assert bcdata.wfs._is_retryable(requests.ConnectionError()) is True
    assert bcdata.wfs._is_retryable(requests.Timeout()) is True


def test_is_retryable_429_without_headers_uses_default_backoff():
    assert bcdata.wfs._is_retryable(_http_error(429)) is True


def test_is_retryable_429_honours_retry_after_header():
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "12"
    backoff = bcdata.wfs._is_retryable(requests.HTTPError(response=response))
    assert backoff == 12.0


def test_is_retryable_429_honours_ratelimit_reset_header():
    """DataBC's Kong gateway sends RateLimit-Reset rather than Retry-After"""
    response = requests.Response()
    response.status_code = 429
    response.headers["RateLimit-Reset"] = "5"
    backoff = bcdata.wfs._is_retryable(requests.HTTPError(response=response))
    assert backoff == 5.0


def test_rate_limit_backoff_unparseable_header_falls_back_to_none():
    response = requests.Response()
    response.headers["Retry-After"] = "Wed, 21 Oct 2026 07:28:00 GMT"
    assert bcdata.wfs._rate_limit_backoff(response) is None


def test_is_retryable_other_exceptions_not_retried():
    assert bcdata.wfs._is_retryable(ValueError("nope")) is False


def test_validate_table_lowercase():
    table = bcdata.validate_name(AIRPORTS_TABLE.lower())
    assert table == AIRPORTS_TABLE


def test_get_table_name_urlparse():
    table = bcdata.get_table_name("natural-resource-nr-district")
    assert table == "WHSE_ADMIN_BOUNDARIES.ADM_NR_DISTRICTS_SPG"


def test_get_count():
    table = bcdata.get_table_name(UTMZONES_KEY)
    assert bcdata.get_count(table) == 6


def test_get_count_filtered():
    assert bcdata.get_count(UTMZONES_KEY, query="UTM_ZONE=10") == 1


def test_get_count_bounds():
    assert bcdata.get_count(AIRPORTS_TABLE, bounds=[1188000, 377051, 1207437, 390361]) == 8


def test_get_sortkey_known():
    assert bcdata.get_sortkey(ASSESSMENTS_TABLE) == "STREAM_CROSSING_ID"


def test_get_sortkey_unknown():
    assert bcdata.get_sortkey(AIRPORTS_TABLE) == "SEQUENCE_ID"


def test_get_data_asgdf():
    gdf = bcdata.get_data(UTMZONES_KEY, query="UTM_ZONE=10", as_gdf=True)
    assert type(gdf) is GeoDataFrame


def test_get_data_asgdf_crs():
    gdf = bcdata.get_data(UTMZONES_KEY, query="UTM_ZONE=10", as_gdf=True)
    assert gdf.crs == "EPSG:3005"


def test_get_null_gdf():
    gdf = bcdata.get_data(UTMZONES_KEY, query="UTM_ZONE=9999", as_gdf=True)
    assert type(gdf) is GeoDataFrame


def test_get_null_geojson():
    data = bcdata.get_data(UTMZONES_KEY, query="UTM_ZONE=9999")
    assert data == {"type": "FeatureCollection", "features": []}


def test_get_data_small():
    data = bcdata.get_data(AIRPORTS_TABLE)
    assert data["type"] == "FeatureCollection"


def test_get_data_count():
    data = bcdata.get_data(AIRPORTS_TABLE, count=100)
    assert len(data["features"]) == 100


def test_get_data_lowercase():
    data = bcdata.get_data(AIRPORTS_TABLE, lowercase=True, count=1)
    assert "airport_name" in data["features"][0]["properties"]


def test_get_data_crs():
    data = bcdata.get_data(AIRPORTS_TABLE, count=1)
    assert data["crs"]["properties"]["name"] == "urn:ogc:def:crs:EPSG::3005"


# this presumes the page size will always be less than the total number of wells
def test_get_data_paged_count():
    wfs = bcdata.wfs.BCWFS()
    count = wfs.pagesize + 100
    data = bcdata.get_data(WELLS_TABLE, count=count)
    assert len(data["features"]) == count


def test_get_data_sortby():
    data = bcdata.get_data(AIRPORTS_TABLE, count=1, sortby="AIRPORT_NAME")
    assert data["features"][0]["properties"]["AIRPORT_NAME"] == "100 Mile House Airport"


def test_cql_filter():
    data = bcdata.get_data(
        AIRPORTS_TABLE,
        query="AIRPORT_NAME='Terrace (Northwest Regional) Airport'",
    )
    assert len(data["features"]) == 1
    assert (
        data["features"][0]["properties"]["AIRPORT_NAME"] == "Terrace (Northwest Regional) Airport"
    )


def test_bounds_filter():
    data = bcdata.get_data(AIRPORTS_TABLE, bounds=[1188000, 377051, 1207437, 390361])
    assert len(data["features"]) == 8


def test_cql_bounds_filter():
    data = bcdata.get_data(
        AIRPORTS_TABLE,
        query="AIRPORT_NAME='Victoria International Airport'",
        bounds=[1167680.0, 367958.0, 1205720.0, 432374.0],
        bounds_crs="EPSG:3005",
    )
    assert len(data["features"]) == 1
    assert data["features"][0]["properties"]["AIRPORT_NAME"] == "Victoria International Airport"


def test_get_data_count_zero():
    # count=0 is a valid request for no features, not "count not provided"
    assert bcdata.wfs.BCWFS().define_requests(AIRPORTS_TABLE, count=0) == []
    assert len(bcdata.get_data(AIRPORTS_TABLE, count=0, as_gdf=True)) == 0


def test_get_count_uses_provided_geom_column(monkeypatch):
    WFS = bcdata.wfs.BCWFS()
    calls = {}

    def fake_request_count(table, **kwargs):
        calls.update(kwargs)
        return 1

    monkeypatch.setattr(WFS, "_request_count", fake_request_count)
    WFS.get_count(AIRPORTS_TABLE, geom_column="MY_GEOM")
    assert calls["geom_column"] == "MY_GEOM"


def test_retry_policy_spans_minutes():
    """refusals lasting 30s or more must not exhaust retries (#264)"""
    p = bcdata.wfs.RETRY_POLICY
    waits = [min(p["wait_initial"] * 2**i, p["wait_max"]) for i in range(p["attempts"] - 1)]
    assert sum(waits) >= 180
    assert p["timeout"] >= sum(waits)


def _assert_retries_refused_connection(call):
    """call fails after retrying a refused connection, without real waits"""
    attempts = 3
    with requests_mock.mock() as m, stamina.set_testing(True, attempts=attempts):
        m.get(requests_mock.ANY, exc=requests.exceptions.ConnectionError("refused"))
        with pytest.raises(requests.ConnectionError):
            call()
        assert m.call_count == attempts


def test_request_retries_refused_connection():
    WFS = _wfs_without_network()
    _assert_retries_refused_connection(lambda: WFS._request(WFS.wfs_url))


def test_request_schema_retries_refused_connection():
    WFS = _wfs_without_network()
    _assert_retries_refused_connection(lambda: WFS._request_schema(AIRPORTS_TABLE))


def test_request_capabilities_retries_refused_connection():
    WFS = _wfs_without_network()
    _assert_retries_refused_connection(WFS._request_capabilities)


def test_bcdc_requests_retry_refused_connection():
    _assert_retries_refused_connection(lambda: bcdata.bcdc._package_show(AIRPORTS_PACKAGE))
    _assert_retries_refused_connection(lambda: bcdata.bcdc._table_definition(AIRPORTS_TABLE))


def _connection_error(cause):
    """A requests ConnectionError shaped like a real one, with cause at the root

    eg ConnectionError -> MaxRetryError -> NewConnectionError -> gaierror
    """
    import urllib3

    new_connection = urllib3.exceptions.NewConnectionError(None, f"failed: {cause}")
    new_connection.__cause__ = cause
    return requests.ConnectionError(urllib3.exceptions.MaxRetryError(None, "/", new_connection))


def test_no_network_is_not_retried():
    """fail fast when DNS lookup fails or no network is reachable"""
    import errno
    import socket

    dns = _connection_error(socket.gaierror(-2, "Name or service not known"))
    unreachable = _connection_error(OSError(errno.ENETUNREACH, "Network is unreachable"))
    assert bcdata.wfs._is_retryable(dns) is False
    assert bcdata.wfs._is_retryable(unreachable) is False


def test_refused_connection_is_retried():
    import errno

    refused = _connection_error(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))
    assert bcdata.wfs._is_retryable(refused) is True


def test_request_fails_fast_without_network():
    import socket

    WFS = _wfs_without_network()
    dns = _connection_error(socket.gaierror(-2, "Name or service not known"))
    with requests_mock.mock() as m, stamina.set_testing(True, attempts=3):
        m.get(requests_mock.ANY, exc=dns)
        with pytest.raises(requests.ConnectionError):
            WFS._request(WFS.wfs_url)
        assert m.call_count == 1
