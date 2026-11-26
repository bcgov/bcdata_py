import copy
import errno
import functools
import json
import logging
import math
import os
import socket
import sys
import tempfile
import threading
import warnings
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import requests
import shapely
import stamina
from shapely.geometry.linestring import LineString
from shapely.geometry.multilinestring import MultiLineString
from shapely.geometry.multipoint import MultiPoint
from shapely.geometry.multipolygon import MultiPolygon
from shapely.geometry.point import Point
from shapely.geometry.polygon import Polygon

import bcdata

if not sys.warnoptions:
    warnings.simplefilter("ignore")

log = logging.getLogger(__name__)


def promote_gdf_to_multi(df):
    """Promote all features to multipart"""
    df.geometry = [
        MultiPoint([feature]) if isinstance(feature, Point) else feature for feature in df.geometry
    ]
    df.geometry = [
        MultiLineString([feature]) if isinstance(feature, LineString) else feature
        for feature in df.geometry
    ]
    df.geometry = [
        MultiPolygon([feature]) if isinstance(feature, Polygon) else feature
        for feature in df.geometry
    ]
    return df


# GeoJSON single part geometry types -> their multipart types
MULTI_TYPES = {"Point": "MultiPoint", "LineString": "MultiLineString", "Polygon": "MultiPolygon"}


def promote_geojson_to_multi(geometry):
    """Return a GeoJSON geometry as multipart (null and multipart geometries unchanged)"""
    if geometry and geometry["type"] in MULTI_TYPES:
        return {
            **geometry,
            "type": MULTI_TYPES[geometry["type"]],
            "coordinates": [geometry["coordinates"]],
        }
    return geometry


class ServiceException(Exception):
    pass


# status codes worth retrying - 5xx are presumed transient service errors,
# 429 is DataBC's WFS telling us we're rate limited
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# How requests to DataBC services are retried (stamina.retry arguments).
# openmaps.gov.bc.ca has been seen to refuse connections from GitHub-hosted runners
# for 30s or more at a time (#264). With stamina's default waits (0.1s doubling to
# 5s) every attempt fails within a few seconds of such a refusal, so waits here grow
# from 5s to a minute, keeping retries going for several minutes: 5, 10, 20, 40, 60,
# 60, 60 seconds between eight attempts (plus up to 1s jitter each), capped at 10
# minutes in total - enough for several attempts at requests that take ~60s to fail
# with a 504.
RETRY_POLICY = {"attempts": 8, "timeout": 600, "wait_initial": 5, "wait_max": 60}

# (connect, read) timeouts for requests to DataBC services, in seconds.
# Connections from GitHub-hosted runners are also silently dropped at times (#264).
# With no connect timeout the OS gives up only after ~127s (Linux resends the
# connection request at 1, 3, 7, 15, 31 and 63s), so each dropped attempt costs
# ~2 minutes. 30s still allows 5 resends - a 10s connect timeout tripped on slow
# runner handshakes (#246), which are likely early resends being dropped - and then
# fails, so the request is retried on a fresh connection. The read timeout catches
# a connected server that stops responding.
REQUEST_TIMEOUT = (30, 65)

# one requests.Session per thread (sessions are not guaranteed to be thread safe)
_sessions = threading.local()

# tables described per DescribeFeatureType request, to keep request URLs short
SCHEMAS_PER_REQUEST = 50

# table schemas requested by this process, {table: schema}, shared by all BCWFS
# instances. Schemas are not cached on disk, so one is never older than the process
# using it, and a change to how schemas are parsed never meets a schema parsed by an
# earlier version of bcdata.
_schemas = {}


def _session():
    """Return this thread's requests.Session for DataBC requests.

    A session keeps connections open and reuses them between requests (HTTP
    keep-alive), instead of opening a new connection - and TLS handshake - for
    every request. Connections from GitHub-hosted runners are blocked for minutes
    at a time (#264); opening fewer connections may make that less likely.
    """
    session = getattr(_sessions, "session", None)
    if session is None:
        session = _sessions.session = requests.Session()
    return session


def _rate_limit_backoff(response):
    """Return the server-specified backoff (seconds) for a 429 response, if given.

    Checks the standard Retry-After header as well as RateLimit-Reset, which
    is what DataBC's Kong gateway sends. Returns None if neither is present
    or parseable, in which case the caller should fall back to the default
    exponential backoff.
    """
    for header in ("Retry-After", "RateLimit-Reset"):
        value = response.headers.get(header)
        if value is not None:
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _is_retryable(exc):
    """Decide whether a request exception is worth retrying.

    Used as the ``on=`` hook for @stamina.retry - see
    https://stamina.hynek.me/en/stable/tutorial.html

    Returning a float instead of True overrides the default backoff, which
    is used to honour a 429 response's server-specified retry delay.
    """
    if _network_unavailable(exc):
        # fail fast: retrying for minutes will not help a machine with no network
        log.error(f"No network (DNS lookup failed or network unreachable), not retrying: {exc!r}")
        return False
    retry = _retry_decision(exc)
    if retry and not isinstance(exc, requests.HTTPError):
        # stamina's own retry log does not say what failed (eg a refused connection).
        # HTTP errors are already logged where the response is checked
        log.warning(f"Request to DataBC failed: {exc!r}")
    return retry


def _network_unavailable(exc):
    """Return True if exc shows this machine has no usable network.

    That is, a DNS lookup failed (socket.gaierror) or the OS reports that no
    network is reachable (ENETUNREACH) - as opposed to a server refusing or not
    answering connections, which is worth retrying (#264). "No route to host"
    (EHOSTUNREACH) is not included, as a firewall blocking a client can cause it.
    The causes are found by walking the exception chain, eg ConnectionError ->
    MaxRetryError -> NameResolutionError -> gaierror.
    """
    seen = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, socket.gaierror):
            return True
        if isinstance(e, OSError) and e.errno == errno.ENETUNREACH:
            return True
        stack += [getattr(e, "reason", None), e.__cause__, e.__context__]
        stack += [a for a in getattr(e, "args", ()) if isinstance(a, BaseException)]
    return False


def _retry_decision(exc):
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        if response is None or response.status_code not in RETRYABLE_STATUS_CODES:
            return False
        if response.status_code == 429:
            backoff = _rate_limit_backoff(response)
            if backoff is not None:
                return backoff
        return True
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


XSD = "{http://www.w3.org/2001/XMLSchema}"
WFS_NS = "{http://www.opengis.net/wfs/2.0}"
OWS_NS = "{http://www.opengis.net/ows/1.1}"

# GML property types of geometry columns, and the geometry type each is reported as.
# The names are those owslib gave (bcdata used it to parse schemas), kept
# because they are user facing: `bcdata info` prints the schema, and the README shows
# "geometry": "GeometryCollection".
GEOMETRY_TYPES = {
    "PointPropertyType": "Point",
    "PolygonPropertyType": "Polygon",
    "LineStringPropertyType": "LineString",
    "MultiPointPropertyType": "MultiPoint",
    "MultiLineStringPropertyType": "MultiLineString",
    "MultiPolygonPropertyType": "MultiPolygon",
    "MultiGeometryPropertyType": "MultiGeometry",
    "GeometryPropertyType": "GeometryCollection",
    "SurfacePropertyType": "3D Polygon",
    "MultiSurfacePropertyType": "3D MultiPolygon",
}


@functools.lru_cache(maxsize=1)
def _parse_table_names(capabilities):
    """Parse table names from capabilities xml.

    Parsing is slow (~50ms) and validate_name/list_tables are called several times
    per request, so the result is cached for as long as the capabilities xml is unchanged.
    """
    root = ET.fromstring(capabilities)
    return tuple(
        name.text.removeprefix("pub:")
        for name in root.iterfind(f"{WFS_NS}FeatureTypeList/{WFS_NS}FeatureType/{WFS_NS}Name")
    )


def _parse_schema(complex_type):
    """Return the schema of a table from the xsd:complexType describing it.

    {"properties": {column: xsd type}, "required": [non-nillable columns],
     "geometry": geometry type, "geometry_column": column}
    """
    schema = {"properties": {}, "required": [], "geometry": None}
    for element in complex_type.iter(XSD + "element"):
        name = element.get("name")
        data_type = element.get("type")
        if name is None or data_type is None:
            # DataBC types every column inline. A column declared another way (ref=,
            # or a nested xsd:simpleType) would otherwise be silently left out
            column = name or element.get("ref") or ET.tostring(element, encoding="unicode")
            raise ServiceException(f"Cannot parse type of column {column}")
        data_type = data_type.split(":")[-1]
        if data_type in GEOMETRY_TYPES:
            schema["geometry"] = GEOMETRY_TYPES[data_type]
            schema["geometry_column"] = name
        else:
            schema["properties"][name] = data_type
        if element.get("nillable", "false") == "false":
            schema["required"].append(name)
    return schema


# Types of WFS feature values, set from the table schema. Shared by to_parquet and
# get_data(as_gdf=True), so a table's columns get the same types whichever is used,
# rather than types guessed from the values in each page.

GEOMETRY_COLUMN = "geometry"

# WFS DescribeFeatureType types that are not written (binary CAD annotations etc)
SKIP_TYPES = ["hexBinary", "base64Binary"]


def parse_date(value):
    # WFS returns dates as eg '2025-08-20Z'
    if value is None:
        return None
    return date.fromisoformat(value[:10])


def parse_geometries(features):
    """Return (shapely geometries, mask of geometries that are present and not empty)"""
    geoms = np.array(
        [
            shapely.from_geojson(json.dumps(f["geometry"])) if f["geometry"] else None
            for f in features
        ],
        dtype=object,
    )
    present = ~(shapely.is_missing(geoms) | shapely.is_empty(geoms))
    return geoms, present


class Columns:
    """Map WFS schema properties to arrow types, and work out which are integers.

    Call arrays() with the properties of each page of features, then fields() for
    the types to give the combined pages.
    """

    def __init__(self, wfs_schema, lowercase=True):
        self.source = []  # WFS property names, in schema order
        self.types = {}  # WFS property name -> WFS type
        for name, wfs_type in wfs_schema["properties"].items():
            if wfs_type in SKIP_TYPES:
                log.info(f"Skipping column {name} of unsupported type {wfs_type}")
                continue
            self.source.append(name)
            self.types[name] = wfs_type
        # WFS property name -> output column name
        self.names = {c: c.lower() if lowercase else c for c in self.source}
        if GEOMETRY_COLUMN in self.names.values():
            raise ValueError(f"Source has a column named {GEOMETRY_COLUMN}, cannot write")
        # WFS/BCDC do not distinguish integers from decimals, so integer columns
        # are inferred: numeric columns where every value seen is a whole number...
        self.integral = {c for c in self.source if self.types[c] == "decimal"}
        # ...and at least one value has been seen (with no values, nothing
        # supports an integer type - eg an empty layer - so stay float64)
        self.has_values = set()
        # numeric columns holding whole numbers too large for float64 to represent
        # exactly (>= 2**53) - these are written as float64, so values are rounded
        self.large_integers = set()

    def arrow_type(self, name):
        wfs_type = self.types[name]
        if wfs_type == "decimal":
            return pa.float64()
        if wfs_type == "date":
            return pa.date32()
        if wfs_type != "string":
            log.debug(f"Unrecognized WFS type {wfs_type} for {name}, writing as string")
        return pa.string()

    def page_fields(self):
        """Fields of the arrays returned by arrays(): numeric columns are float64"""
        return [pa.field(self.names[c], self.arrow_type(c)) for c in self.source]

    def integer_columns(self):
        """Output names of numeric columns holding only whole numbers, in every page seen"""
        return {self.names[c] for c in self.integral & self.has_values}

    def fields(self):
        """Fields of the combined pages: integer_columns() are int64"""
        integers = self.integer_columns()
        return [f.with_type(pa.int64()) if f.name in integers else f for f in self.page_fields()]

    def arrays(self, properties):
        """Return an arrow array for each column, from a page of features' properties"""
        arrays = []
        for c in self.source:
            values = [p.get(c) for p in properties]
            wfs_type = self.types[c]
            if wfs_type == "decimal":
                if c not in self.has_values and any(v is not None for v in values):
                    self.has_values.add(c)
                if c not in self.large_integers and any(
                    isinstance(v, int) and not isinstance(v, bool) and abs(v) >= 2**53
                    for v in values
                ):
                    self.large_integers.add(c)
                if c in self.integral and not all(
                    v is None or (isinstance(v, int) and abs(v) < 2**53) for v in values
                ):
                    self.integral.discard(c)
            elif wfs_type == "date":
                values = [parse_date(v) for v in values]
            else:
                # string (or unrecognized) - tolerate non-string JSON values
                values = [v if v is None or isinstance(v, str) else json.dumps(v) for v in values]
            arrays.append(pa.array(values, type=self.arrow_type(c)))
        return arrays

    def json_properties(self, properties):
        """Return one feature's properties typed from the schema, for writing as JSON.

        Dates are YYYY-MM-DD and strings are strings, as in arrays(). Numbers are left
        as the WFS sends them (whole numbers as integers), as which columns are integers
        is only known once every feature has been seen.
        """
        typed = {}
        for c in self.source:
            value = properties.get(c)
            wfs_type = self.types[c]
            if wfs_type == "date":
                value = parse_date(value)
                value = value.isoformat() if value else None
            elif wfs_type != "decimal" and value is not None and not isinstance(value, str):
                value = json.dumps(value)
            typed[self.names[c]] = value
        return typed

    def log_large_integers(self, table):
        if self.large_integers:
            log.warning(
                f"{table}: column(s) {sorted(self.names[c] for c in self.large_integers)} "
                "hold whole numbers of 2**53 or more, which are written as float64 and so "
                "are rounded"
            )


class BCWFS:
    """Wrapper around web feature service"""

    def __init__(self, refresh=False):
        self.wfs_url = "https://openmaps.gov.bc.ca/geo/pub/wfs"
        self.ows_url = "https://openmaps.gov.bc.ca/geo/pub/ows"

        # point to cache path
        if "BCDATA_CACHE" in os.environ:
            self.cache_path = os.environ["BCDATA_CACHE"]
        else:
            self.cache_path = os.path.join(str(Path.home()), ".bcdata")
        # if a file exists in the path provided AND the file name is .bcdata, delete it
        p = Path(self.cache_path)
        if p.is_file():
            if p.name == ".bcdata":
                p.unlink()
            # if the file is named something else, prompt user to delete it
            else:
                raise RuntimeError(
                    f"Cache file exists, delete before using bcdata: {self.cache_path}"
                )
        # create cache folder if it does not exist
        p.mkdir(parents=True, exist_ok=True)
        self.refresh = refresh
        self.cache_refresh_days = 30
        self.request_headers = {"User-Agent": f"bcdata.py ({bcdata.__version__})"}
        self.capabilities = self.get_capabilities()
        # get pagesize from xml using the xpath from https://github.com/bcgov/bcdata/
        countdefault = ET.fromstring(self.capabilities).find(
            f".//{OWS_NS}Constraint[@name='CountDefault']/{OWS_NS}DefaultValue"
        )
        self.pagesize = int(countdefault.text)

    def _write_cache_file(self, cache_file, text):
        """Write text to a cache file atomically (temp file in cache dir, then replace)"""
        fd, tmp = tempfile.mkstemp(dir=self.cache_path, prefix=f".{cache_file}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, os.path.join(self.cache_path, cache_file))
        except BaseException:
            os.unlink(tmp)
            raise

    def _cache_is_stale(self, cache_file):
        """Return True if a cache file should be requested again.

        That is, if refresh was asked for, or the file is missing, empty, or more
        than cache_refresh_days old.
        """
        if self.refresh:
            return True
        path = os.path.join(self.cache_path, cache_file)
        if not os.path.exists(path):
            return True
        mod_date = datetime.fromtimestamp(os.path.getmtime(path), tz=UTC)
        return (
            mod_date < (datetime.now(tz=UTC) - timedelta(days=self.cache_refresh_days))
            or os.stat(path).st_size == 0
        )

    def _request_xml(self, request, **params):
        """Make a WFS request that returns an xml document (GetCapabilities,
        DescribeFeatureType), and return the parsed document and its raw bytes.

        The service can report errors as an ExceptionReport document rather than
        with an error status, so that is raised as a ServiceException.
        """
        params = {"service": "WFS", "version": "2.0.0", "request": request, **params}
        r = self._request(self.ows_url, params=params, silent=True)
        root = ET.fromstring(r.content)
        if root.tag.endswith("ExceptionReport"):
            raise ServiceException(r.text)
        return root, r.content

    def _request_schemas(self, tables):
        """Request the schemas of several tables in one DescribeFeatureType request.

        Returns {table: schema}, see _parse_schema.
        """
        root, _ = self._request_xml("DescribeFeatureType", typeNames=",".join(tables))
        # each table is a top level element, typed by a complexType listing its columns
        types = {e.get("name"): e.get("type") for e in root.findall(XSD + "element")}
        complex_types = {c.get("name"): c for c in root.findall(XSD + "complexType")}
        schemas = {}
        for table in tables:
            if table not in types:
                raise ServiceException(f"DescribeFeatureType returned no schema for {table}")
            schemas[table] = _parse_schema(complex_types[types[table].split(":")[-1]])
        return schemas

    def _request_capabilities(self):
        """Request the capabilities document, which lists every table."""
        _, content = self._request_xml("GetCapabilities")
        # the document is utf-8 (and declares it), whatever the response headers say
        return content.decode("utf-8")

    @stamina.retry(on=_is_retryable, **RETRY_POLICY)
    def _request(self, url, params=None, silent=False):
        """Submit a request to DataBC WFS and return the raw response.

        Failures are retried as set by RETRY_POLICY: connection refusals can
        last 30s or more (#264), and the WFS gateway itself can take up to ~60s
        to fail with a 504, so retries continue for several minutes.

        Timeouts are set by REQUEST_TIMEOUT (see the notes there on why the
        connect timeout is 30s).
        """
        r = _session().get(
            url, params=params, headers=self.request_headers, timeout=REQUEST_TIMEOUT
        )
        if not silent:
            log.info(r.url)
        else:
            log.debug(r.url)
        if r.status_code in [400, 401, 404]:
            log.error(f"HTTP error {r.status_code}")
            log.error(f"Response headers: {r.headers}")
            log.error(f"Response text: {r.text}")
            raise ServiceException(r.text)  # presumed request error
        if r.status_code == 429:
            backoff = _rate_limit_backoff(r)
            if backoff is not None:
                log.warning(
                    f"Rate limited by DataBC WFS, backing off for {backoff}s before retrying"
                )
            else:
                log.warning("Rate limited by DataBC WFS, retrying")
        elif r.status_code in RETRYABLE_STATUS_CODES:
            log.warning(f"HTTP error: {r.status_code}, retrying")
            log.warning(f"Response headers: {r.headers}")
            log.warning(f"Response text: {r.text}")
        r.raise_for_status()
        return r

    def _request_count(self, table, query=None, bounds=None, bounds_crs=None, geom_column=None):
        payload = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeName": table,
            "resultType": "hits",
            "outputFormat": "json",
        }
        if query or bounds:
            payload["CQL_FILTER"] = self.build_bounds_filter(
                query=query,
                bounds=bounds,
                bounds_crs=bounds_crs,
                geom_column=geom_column,
            )
        r = self._request(self.wfs_url, params=payload, silent=True)
        return int(ET.fromstring(r.text).attrib["numberMatched"])

    def _request_featurecollection(self, url, silent=False):
        """Submit a getfeature request to DataBC WFS and return feature collection"""
        return self._request(url, silent=silent).json()

    def build_bounds_filter(self, query, bounds, bounds_crs, geom_column):
        """The bbox param shortcut is mutually exclusive with CQL_FILTER,
        combine query and bounds into a single CQL_FILTER expression
        """
        # return query untouched if no bounds provided
        if not bounds:
            if query:
                cql_filter = query
            else:
                cql_filter = None
        # parse the bounds into a bbox
        elif bounds:
            b0, b1, b2, b3 = [str(b) for b in bounds]
            bnd_query = f"bbox({geom_column}, {b0}, {b1}, {b2}, {b3}, '{bounds_crs}')"
            if query:
                cql_filter = f"({query}) AND {bnd_query}"
            else:
                cql_filter = bnd_query
        return cql_filter

    def get_capabilities(self):
        """
        Request server capabilities (layer definitions).
        Cache the response as a utf-8 file for cache_refresh_days, caching to one of:
          - $BCDATA_CACHE environment variable
          - default (~/.bcdata)
        """
        if not self._cache_is_stale("capabilities.xml"):
            try:
                with open(os.path.join(self.cache_path, "capabilities.xml"), encoding="utf-8") as f:
                    return f.read()
            except UnicodeDecodeError:
                # written by an earlier bcdata in the platform's default encoding
                # (eg cp1252 on Windows), request it again
                log.info("Cached capabilities.xml is not utf-8, requesting it again")
        capabilities = self._request_capabilities()
        self._write_cache_file("capabilities.xml", capabilities)
        return capabilities

    def get_count(self, dataset, query=None, bounds=None, bounds_crs="EPSG:3005", geom_column=None):
        """Ask DataBC WFS how many features there are in a table/query/bounds"""
        table = self.validate_name(dataset)
        if not geom_column:
            geom_column = self.get_schema(table)["geometry_column"]
        count = self._request_count(
            table,
            query=query,
            bounds=bounds,
            bounds_crs=bounds_crs,
            geom_column=geom_column,
        )
        return count

    def _get_schemas(self, tables):
        """Return {table: schema} for several tables, requesting them in batches.

        Describing up to SCHEMAS_PER_REQUEST tables per request means checking many
        tables makes a few requests rather than one per table. Each table is requested
        once per process (or again if refresh was asked for), see _schemas.
        """
        missing = [t for t in dict.fromkeys(tables) if self.refresh or t not in _schemas]
        for i in range(0, len(missing), SCHEMAS_PER_REQUEST):
            _schemas.update(self._request_schemas(missing[i : i + SCHEMAS_PER_REQUEST]))
        # copies, so a caller changing a schema does not change it for later callers
        return {table: copy.deepcopy(_schemas[table]) for table in tables}

    def get_schema(self, table):
        return self._get_schemas([table])[table]

    def get_sortkey(self, table):
        """Check data for unique columns available for sorting paged requests"""
        columns = list(self.get_schema(table)["properties"].keys())
        # use sort key if one is recorded in the bundled table metadata
        sort_key = bcdata.table_metadata.get(table.lower(), {}).get("sort_key")
        if sort_key:
            return sort_key.upper()
        # otherwise use known primary key if it is present in the bundled table metadata
        elif table.lower() in bcdata.primary_keys:
            return bcdata.primary_keys[table.lower()].upper()
        # if pk not known, use OBJECTID as default sort key when present
        elif "OBJECTID" in columns:
            return "OBJECTID"
        # if OBJECTID is not present (several GSR tables), use SEQUENCE_ID
        elif "SEQUENCE_ID" in columns:
            return "SEQUENCE_ID"
        # otherwise, presume first column is best value to sort by
        # (in some cases this will be incorrect)
        else:
            log.warning(
                f"Reliable sort key for {table} cannot be determined, defaulting to first column {columns[0]}"
            )
            return columns[0]

    def list_tables(self):
        """read and parse capabilities xml, which lists all tables available"""
        return list(_parse_table_names(self.capabilities))

    def validate_name(self, dataset):
        """Check wfs/cache and the bcdc api to see if dataset name is valid"""
        if dataset.upper() in self.list_tables():
            return dataset.upper()
        else:
            return bcdata.get_table_name(dataset.upper())

    def define_requests(
        self,
        dataset,
        query=None,
        bounds=None,
        bounds_crs="EPSG:3005",
        count=None,
        sortby=None,
        check_count=True,
    ):
        """Translate provided parameters into a list of WFS request URLs required
        to download the dataset as specified

        References:
        - http://www.opengeospatial.org/standards/wfs
        - http://docs.geoserver.org/stable/en/user/services/wfs/vendor.html
        - http://docs.geoserver.org/latest/en/user/tutorials/cql/cql_tutorial.html
        """
        # validate the table name
        table = self.validate_name(dataset)

        # get name of the geometry column
        schema = self.get_schema(table)
        geom_column = schema["geometry_column"]

        # find out how many records are in the table
        if count is None and check_count is False:
            raise ValueError(
                "{count: Null, check_count=False} is invalid, either provide record count or let bcdata request it"
            )
        elif (
            count is None and check_count is True
        ):  # if not provided a count, get one if not told otherwise
            count = self.get_count(
                table,
                query=query,
                bounds=bounds,
                bounds_crs=bounds_crs,
                geom_column=geom_column,
            )
        elif (
            count is not None and check_count is True
        ):  # if provided a count that is bigger than actual number of records, automatically correct count
            n = self.get_count(
                table,
                query=query,
                bounds=bounds,
                bounds_crs=bounds_crs,
                geom_column=geom_column,
            )
            count = min(count, n)

        log.info(f"Total features requested: {count}")

        # for datasets with >10k records, generate a list of urls based on number of features in the dataset.
        chunks = math.ceil(count / self.pagesize)

        # if making several requests, we need to sort by something
        if chunks > 1 and not sortby:
            sortby = self.get_sortkey(table)

        # build the request parameters for each chunk
        urls = []
        for i in range(chunks):
            request = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeName": table,
                "outputFormat": "json",
                "SRSNAME": "EPSG:3005",  # just in case (this should always be the default)
            }
            if sortby:
                request["sortby"] = sortby.upper()
            if query or bounds:
                request["CQL_FILTER"] = self.build_bounds_filter(
                    query=query,
                    bounds=bounds,
                    bounds_crs=bounds_crs,
                    geom_column=geom_column,
                )
            if chunks == 1:
                request["count"] = count
            if chunks > 1:
                request["startIndex"] = i * self.pagesize
                if count < (request["startIndex"] + self.pagesize):
                    request["count"] = count - request["startIndex"]
                else:
                    request["count"] = self.pagesize
            urls.append(self.wfs_url + "?" + urlencode(request, doseq=True))
        return urls

    def request_features(
        self,
        url,
        as_gdf=False,
        lowercase=False,
        promote_to_multi=False,
    ):
        """Make a request to WFS and return data as a FeatureCollection or a GeoDataFrame

        The FeatureCollection is as DataBC sends it (eg dates are strings like
        '2025-08-20Z'), other than property names being lowercased or geometries
        promoted to multipart, if asked for, and feature ids being dropped.
        """
        featurecollection = self._request_featurecollection(url)
        if not as_gdf:
            for feature in featurecollection["features"]:
                # DataBC's feature ids are either a table's primary key (already a
                # property) or, for views, generated anew for each request
                feature.pop("id", None)
                if lowercase:
                    feature["properties"] = {k.lower(): v for k, v in feature["properties"].items()}
                if promote_to_multi:
                    feature["geometry"] = promote_geojson_to_multi(feature["geometry"])
            return featurecollection

        if len(featurecollection["features"]) > 0:
            gdf = gpd.GeoDataFrame.from_features(featurecollection)
            gdf = gdf.set_crs("EPSG:3005")
            if gdf.geometry.name != "geometry":
                gdf = gdf.rename_geometry("geometry")
            if lowercase:
                gdf.columns = [c.lower() for c in gdf.columns]
            if promote_to_multi:
                gdf = promote_gdf_to_multi(gdf)
        else:
            gdf = gpd.GeoDataFrame(geometry=[])
        return gdf


def get_data(
    dataset,
    query=None,
    bounds=None,
    bounds_crs="epsg:3005",
    count=None,
    sortby=None,
    as_gdf=False,
    lowercase=False,
    promote_to_multi=False,
):
    """Request features from DataBC WFS, returning GeoJSON featurecollection or geodataframe

    A geodataframe's column types are set from the table schema (see _typed_gdf).
    GeoJSON is returned as DataBC sends it, eg dates are strings like '2025-08-20Z'.
    """
    WFS = BCWFS()
    table = WFS.validate_name(dataset)
    urls = WFS.define_requests(
        table,
        query=query,
        bounds=bounds,
        bounds_crs=bounds_crs,
        count=count,
        sortby=sortby,
    )
    if as_gdf:
        return _typed_gdf(WFS, table, urls, lowercase=lowercase, promote_to_multi=promote_to_multi)
    data = {"type": "FeatureCollection", "features": []}
    for url in urls:
        featurecollection = WFS.request_features(
            url, lowercase=lowercase, promote_to_multi=promote_to_multi
        )
        # keep the crs, but not members that describe a single page (eg numberReturned)
        if featurecollection.get("crs"):
            data["crs"] = featurecollection["crs"]
        data["features"].extend(featurecollection["features"])
    return data


def _typed_gdf(WFS, table, urls, lowercase=False, promote_to_multi=False):
    """Request features and return them as a GeoDataFrame, with column types set from
    the table schema as to_parquet sets them (see Columns), rather than guessed
    by geopandas from the values in each page.

    Integer columns are pandas' nullable Int64, so they stay integers when they hold
    nulls (pandas would otherwise make them float64, as plain int64 cannot hold nulls).
    Dates are datetime.date values, as when reading a bcdata parquet file.
    """
    columns = Columns(WFS.get_schema(table), lowercase=lowercase)
    page_schema = pa.schema(columns.page_fields())
    pages, geoms = [], []
    for url in urls:
        features = WFS._request_featurecollection(url)["features"]
        if features:
            properties = [f["properties"] for f in features]
            pages.append(pa.RecordBatch.from_arrays(columns.arrays(properties), schema=page_schema))
            geoms.append(parse_geometries(features)[0])
    columns.log_large_integers(table)
    # integer columns are known once every page has been seen
    data = pa.Table.from_batches(pages, schema=page_schema).cast(pa.schema(columns.fields()))
    df = data.to_pandas(types_mapper={pa.int64(): pd.Int64Dtype()}.get)
    geometry = gpd.GeoSeries(
        np.concatenate(geoms) if geoms else [], name=GEOMETRY_COLUMN, crs="EPSG:3005"
    )
    gdf = gpd.GeoDataFrame(df, geometry=geometry)
    if promote_to_multi:
        gdf = promote_gdf_to_multi(gdf)
    return gdf


def get_count(dataset, query=None, bounds=None, bounds_crs="EPSG:3005"):
    WFS = BCWFS()
    return WFS.get_count(dataset, query=query, bounds=bounds, bounds_crs=bounds_crs)


def get_sortkey(dataset):
    WFS = BCWFS()
    table = WFS.validate_name(dataset)
    return WFS.get_sortkey(table)


def list_tables(refresh=False):
    WFS = BCWFS(refresh)
    return WFS.list_tables()


def validate_name(dataset):
    WFS = BCWFS()
    return WFS.validate_name(dataset)
