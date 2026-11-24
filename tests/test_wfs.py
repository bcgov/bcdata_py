import logging
from datetime import date

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
    WFS.ows_url = "https://openmaps.gov.bc.ca/geo/pub/ows"
    WFS.request_headers = {"User-Agent": "bcdata.py tests"}
    return WFS


@pytest.fixture
def quiet_logs(caplog):
    """Silence bcdata and stamina logging in tests that simulate failed requests.

    CI prints warnings live (log_cli) to show real retries (#264); without this,
    simulated failures log the same "Request to DataBC failed" lines.
    """
    caplog.set_level(logging.CRITICAL + 1, logger="bcdata")
    caplog.set_level(logging.CRITICAL + 1, logger="stamina")


@pytest.fixture
def no_retries():
    """Deactivate retries for tests that mock failing responses, so they do not wait.

    Only these tests: live tests run with retries active, as bcdata is used.
    """
    stamina.set_active(False)
    yield
    stamina.set_active(True)


def test_http_error_502(no_retries, quiet_logs):
    # request a feature page directly, so the test does not depend on cached
    # capabilities (BCWFS() would otherwise request them, and get the mocked error)
    WFS = _wfs_without_network()
    with requests_mock.mock() as m:
        m.get(requests_mock.ANY, status_code=502)
        with pytest.raises(requests.HTTPError):
            WFS._request(WFS.wfs_url)


def test_http_error_404(no_retries, quiet_logs):
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


def test_is_retryable_connection_and_timeout_errors(quiet_logs):
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


def test_get_data_paged_count(small_pages):
    # 3 requests: two full pages and a partial one
    count = small_pages * 2 + 5
    data = bcdata.get_data(WELLS_TABLE, count=count)
    assert len(data["features"]) == count
    # pages are sorted, so they do not overlap
    ids = [f["properties"]["OBJECTID"] for f in data["features"]]
    assert len(set(ids)) == count


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


def test_request_retries_refused_connection(quiet_logs):
    WFS = _wfs_without_network()
    _assert_retries_refused_connection(lambda: WFS._request(WFS.wfs_url))


def test_request_schemas_retries_refused_connection(quiet_logs):
    WFS = _wfs_without_network()
    _assert_retries_refused_connection(lambda: WFS._request_schemas([AIRPORTS_TABLE]))


def test_request_capabilities_retries_refused_connection(quiet_logs):
    WFS = _wfs_without_network()
    _assert_retries_refused_connection(WFS._request_capabilities)


def test_bcdc_requests_retry_refused_connection(quiet_logs):
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


def test_no_network_is_not_retried(quiet_logs):
    """fail fast when DNS lookup fails or no network is reachable"""
    import errno
    import socket

    dns = _connection_error(socket.gaierror(-2, "Name or service not known"))
    unreachable = _connection_error(OSError(errno.ENETUNREACH, "Network is unreachable"))
    assert bcdata.wfs._is_retryable(dns) is False
    assert bcdata.wfs._is_retryable(unreachable) is False


def test_refused_connection_is_retried(quiet_logs):
    import errno

    refused = _connection_error(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))
    assert bcdata.wfs._is_retryable(refused) is True


def test_request_fails_fast_without_network(quiet_logs):
    import socket

    WFS = _wfs_without_network()
    dns = _connection_error(socket.gaierror(-2, "Name or service not known"))
    with requests_mock.mock() as m, stamina.set_testing(True, attempts=3):
        m.get(requests_mock.ANY, exc=dns)
        with pytest.raises(requests.ConnectionError):
            WFS._request(WFS.wfs_url)
        assert m.call_count == 1


def test_requests_use_connect_timeout(quiet_logs):
    """a dropped connection fails after REQUEST_TIMEOUT[0] seconds, not the OS's ~2 minutes"""
    assert bcdata.wfs.REQUEST_TIMEOUT[0] is not None
    WFS = _wfs_without_network()
    with requests_mock.mock() as m:
        m.get(requests_mock.ANY, json={})
        WFS._request(WFS.wfs_url)
        bcdata.bcdc._package_show(AIRPORTS_PACKAGE)
    assert [r.timeout for r in m.request_history] == [bcdata.wfs.REQUEST_TIMEOUT] * 2


def test_requests_reuse_connections(monkeypatch):
    """requests share a keep-alive connection instead of opening one each (#264)"""
    import http.server
    import threading

    connections = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep connections open between requests

        def setup(self):
            connections.append(self.client_address)  # once per connection
            super().setup()

        def do_GET(self):
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # do not route the local request through a proxy, if one is configured
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    try:
        WFS = _wfs_without_network()
        url = f"http://127.0.0.1:{server.server_address[1]}/wfs"
        for _ in range(3):
            WFS._request(url)
    finally:
        server.shutdown()
        server.server_close()
    assert len(connections) == 1


def test_one_session_per_thread():
    import threading

    sessions = []
    threads = [
        threading.Thread(target=lambda: sessions.append(bcdata.wfs._session())) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bcdata.wfs._session() is bcdata.wfs._session()
    assert len({id(s) for s in sessions + [bcdata.wfs._session()]}) == 3


DESCRIBE_TWO_TABLES = """<?xml version="1.0" encoding="UTF-8"?>
<xsd:schema xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:pub="http://delivery.openmaps.gov.bc.ca/geo/"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema" elementFormDefault="qualified"
  targetNamespace="http://delivery.openmaps.gov.bc.ca/geo/">
  <xsd:complexType name="A.POINTSType"><xsd:complexContent>
    <xsd:extension base="gml:AbstractFeatureType"><xsd:sequence>
      <xsd:element maxOccurs="1" minOccurs="1" name="ID" nillable="false" type="xsd:decimal"/>
      <xsd:element maxOccurs="1" minOccurs="0" name="SHAPE" nillable="true" type="gml:PointPropertyType"/>
    </xsd:sequence></xsd:extension></xsd:complexContent></xsd:complexType>
  <xsd:element name="A.POINTS" substitutionGroup="gml:AbstractFeature" type="pub:A.POINTSType"/>
  <xsd:complexType name="B.AREASType"><xsd:complexContent>
    <xsd:extension base="gml:AbstractFeatureType"><xsd:sequence>
      <xsd:element maxOccurs="1" minOccurs="0" name="NAME" nillable="true" type="xsd:string"/>
      <xsd:element maxOccurs="1" minOccurs="0" name="GEOMETRY" nillable="true" type="gml:GeometryPropertyType"/>
    </xsd:sequence></xsd:extension></xsd:complexContent></xsd:complexType>
  <xsd:element name="B.AREAS" substitutionGroup="gml:AbstractFeature" type="pub:B.AREASType"/>
</xsd:schema>"""


def test_request_schemas_describes_tables_in_one_request():
    WFS = _wfs_without_network()
    with requests_mock.mock() as m:
        m.get(requests_mock.ANY, text=DESCRIBE_TWO_TABLES)
        schemas = WFS._request_schemas(["A.POINTS", "B.AREAS"])
    assert m.call_count == 1
    assert m.last_request.qs["typenames"] == ["a.points,b.areas"]
    assert schemas["A.POINTS"] == {
        "properties": {"ID": "decimal"},
        "required": ["ID"],
        "geometry": "Point",
        "geometry_column": "SHAPE",
    }
    assert schemas["B.AREAS"]["properties"] == {"NAME": "string"}
    assert schemas["B.AREAS"]["geometry_column"] == "GEOMETRY"


def test_request_schemas_missing_table():
    WFS = _wfs_without_network()
    with requests_mock.mock() as m:
        m.get(requests_mock.ANY, text=DESCRIBE_TWO_TABLES)
        with pytest.raises(bcdata.wfs.ServiceException, match="C.OTHER"):
            WFS._request_schemas(["A.POINTS", "C.OTHER"])


def test_get_schemas_batches_requests(tmp_path, monkeypatch):
    WFS = _wfs_without_network()
    WFS.cache_path, WFS.refresh, WFS.cache_refresh_days = str(tmp_path), False, 30
    batches = []

    def fake_request_schemas(tables):
        batches.append(tables)
        return {t: {"properties": {}} for t in tables}

    monkeypatch.setattr(WFS, "_request_schemas", fake_request_schemas)
    monkeypatch.setattr(bcdata.wfs, "SCHEMAS_PER_REQUEST", 2)
    tables = ["A.ONE", "A.TWO", "A.THREE"]
    assert set(WFS._get_schemas(tables)) == set(tables)
    assert batches == [["A.ONE", "A.TWO"], ["A.THREE"]]
    # cached schemas are not requested again
    WFS._get_schemas(tables)
    assert len(batches) == 2


def test_request_schemas_untyped_column():
    WFS = _wfs_without_network()
    untyped = DESCRIBE_TWO_TABLES.replace(
        'name="NAME" nillable="true" type="xsd:string"/>',
        'name="NAME" nillable="true"><xsd:simpleType><xsd:restriction base="xsd:string">'
        '<xsd:maxLength value="50"/></xsd:restriction></xsd:simpleType></xsd:element>',
    )
    assert untyped != DESCRIBE_TWO_TABLES
    with requests_mock.mock() as m:
        m.get(requests_mock.ANY, text=untyped)
        with pytest.raises(bcdata.wfs.ServiceException, match="column NAME"):
            WFS._request_schemas(["A.POINTS", "B.AREAS"])


EXCEPTION_REPORT = """<?xml version="1.0" encoding="UTF-8"?>
<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0">
  <ows:Exception exceptionCode="InvalidParameterValue" locator="typeName">
    <ows:ExceptionText>Feature type pub:C.OTHER unknown</ows:ExceptionText>
  </ows:Exception>
</ows:ExceptionReport>"""


def test_request_schemas_exception_report():
    """an ExceptionReport returned with status 200 raises the service's message"""
    WFS = _wfs_without_network()
    with requests_mock.mock() as m:
        m.get(requests_mock.ANY, text=EXCEPTION_REPORT)
        with pytest.raises(bcdata.wfs.ServiceException, match="C.OTHER unknown"):
            WFS._request_schemas(["C.OTHER"])


def _point_feature(**properties):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [1, 2]},
        "properties": properties,
    }


def test_typed_gdf(monkeypatch):
    """column types come from the table schema, the same in every page - not guessed
    from each page (where a column with no values on a page would become object)"""
    WFS = _wfs_without_network()
    schema = {
        "properties": {"ID": "decimal", "SCORE": "decimal", "NAME": "string", "SURVEYED": "date"},
        "geometry_column": "SHAPE",
    }
    pages = {
        "page1": [_point_feature(ID=1, SCORE=None, NAME=None, SURVEYED="2025-08-20Z")],
        "page2": [_point_feature(ID=2, SCORE=3, NAME="a", SURVEYED=None)],
    }
    monkeypatch.setattr(WFS, "get_schema", lambda table: schema)
    monkeypatch.setattr(WFS, "_request_featurecollection", lambda url: {"features": pages[url]})
    gdf = bcdata.wfs._typed_gdf(WFS, "A.TABLE", ["page1", "page2"], lowercase=True)
    assert {c: str(t) for c, t in gdf.dtypes.items()} == {
        "id": "Int64",
        "score": "Int64",  # integers stay integers, with nulls
        "name": "str",
        "surveyed": "object",  # datetime.date values
        "geometry": "geometry",
    }
    assert gdf["surveyed"].tolist() == [date(2025, 8, 20), None]
    assert gdf.crs == "EPSG:3005"
