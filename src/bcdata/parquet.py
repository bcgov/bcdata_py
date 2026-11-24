"""Write a DataBC WFS layer to a GeoParquet 2.0 file.

The file uses the native Parquet GEOMETRY logical type (CRS embedded as PROJJSON)
and also carries GeoParquet "geo" key-value metadata, so that readers predating
native geospatial types (eg geopandas < 1.2, GDAL < 3.12) can still read it.

Writing is done in two passes so memory use stays bounded for large layers:

1. WFS pages are streamed to a staging parquet file in a temp folder. Numeric
   columns are staged as float64 (WFS and BCDC schemas do not distinguish
   integers from floats), and a Hilbert curve key is computed for each feature.
2. The download is checked (feature count, primary key uniqueness), then DuckDB
   sorts the staging file by Hilbert key (spilling to disk if needed), numeric
   columns holding only integer values are cast to int64, and the result is
   written in fixed size row groups. Spatial sorting keeps row group bounding
   boxes tight, so readers can skip row groups when filtering by area.

Output order is deterministic: ties on the Hilbert key (eg features without
geometry) are broken by the order features were received from WFS, and WFS
requests are always sorted (by the table's sort key, as for paged requests),
even when a download fits in a single request.

The "bcdata" metadata records two hashes, so a fresh download can be compared
with an earlier file by reading only the file footers:

- schema_hash: column names and types
- content_hash: the schema plus every value of every row, in output order

Both depend only on the data, not on how the file is written (row group size,
compression, arrow memory layout) or on other metadata such as the download time.
Geometries are hashed as the WKB shapely produces, so an upgrade that changed WKB
output would change every content_hash once.
The "bcdata" metadata is added to the file footer once every row is written, so
read it with pyarrow.parquet.read_metadata (not read_schema).
"""

import hashlib
import json
import logging
import os
import secrets
import shutil
import tempfile
from datetime import UTC, datetime

import duckdb
import geoarrow.pyarrow as ga
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pyproj
import shapely
from geopandas import GeoSeries

import bcdata
from bcdata.wfs import BCWFS, GEOMETRY_COLUMN, Columns, parse_geometries

log = logging.getLogger(__name__)

# approximate extent of BC in BC Albers, used as the Hilbert curve domain.
# Features outside the extent are clamped to its edge (still sorted, just less
# finely) so a fixed domain gives the same keys regardless of layer extent.
BC_BOUNDS = (200000.0, 300000.0, 1900000.0, 1750000.0)
HILBERT_LEVEL = 16
NULL_HILBERT = 2 ** (2 * HILBERT_LEVEL)  # sorts null/empty geometries last

GEOPARQUET_VERSION = "2.0.0"
SRID = 3005

# shapely type id -> GeoParquet geometry_types name
GEOMETRY_TYPE_NAMES = {
    0: "Point",
    1: "LineString",
    2: "LineString",  # LinearRing
    3: "Polygon",
    4: "MultiPoint",
    5: "MultiLineString",
    6: "MultiPolygon",
    7: "GeometryCollection",
}

# internal staging columns
_HILBERT = "__bcdata_hilbert"
_SEQ = "__bcdata_seq"


class DownloadCheckError(RuntimeError):
    """The downloaded features failed a consistency check (missing/duplicated features)"""


def _quote(name):
    """Quote an identifier for DuckDB SQL"""
    return '"' + name.replace('"', '""') + '"'


def _geometry_summary(geoms, present):
    """Return (set of GeoParquet geometry type names, bounds) of the present geometries"""
    if not present.any():
        return set(), None
    g = geoms[present]
    type_ids = shapely.get_type_id(g).tolist()
    has_z = shapely.has_z(g).tolist()
    types = {GEOMETRY_TYPE_NAMES[t] + (" Z" if z else "") for t, z in set(zip(type_ids, has_z))}
    return types, shapely.total_bounds(g)


def _hilbert_keys(geoms, present):
    """Hilbert distance of each geometry's bbox midpoint over a fixed BC extent"""
    keys = np.full(len(geoms), NULL_HILBERT, dtype=np.int64)
    if present.any():
        b = shapely.bounds(geoms[present])
        x = np.clip((b[:, 0] + b[:, 2]) / 2, BC_BOUNDS[0], BC_BOUNDS[2])
        y = np.clip((b[:, 1] + b[:, 3]) / 2, BC_BOUNDS[1], BC_BOUNDS[3])
        keys[present] = GeoSeries(shapely.points(x, y)).hilbert_distance(
            total_bounds=BC_BOUNDS, level=HILBERT_LEVEL
        )
    return keys


def _schema_hash(fields):
    """sha256 of output column names and types"""
    canonical = [
        [f.name, f"geometry(wkb,EPSG:{SRID})" if f.name == GEOMETRY_COLUMN else str(f.type)]
        for f in fields
    ]
    return "sha256:" + hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


class _ContentHasher:
    """Hash every value of every column, independent of how rows are chunked.

    Each column is reduced to canonical byte streams - validity (one byte per
    row), then either fixed width values or variable width lengths and data -
    and each stream is hashed incrementally, so the result is the same however
    the rows are split into tables/row groups, and however arrow lays them out
    in memory. Null slots are filled with a fixed value so their (undefined)
    contents do not matter.
    """

    def __init__(self, schema_hash, fields):
        self.schema_hash = schema_hash
        self.names = [f.name for f in fields]
        self.streams = {}

    def _update(self, name, kind, data):
        key = (name, kind)
        if key not in self.streams:
            self.streams[key] = hashlib.sha256()
        self.streams[key].update(data)

    def update(self, table):
        for name in self.names:
            arr = table.column(name).combine_chunks()
            if isinstance(arr.type, pa.ExtensionType):
                arr = arr.storage
            self._update(name, "valid", arr.is_valid().to_numpy(zero_copy_only=False).tobytes())
            t = arr.type
            if pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_string_view(t):
                arr = arr.cast(pa.large_string()).fill_null("")
            elif pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_binary_view(t):
                arr = arr.cast(pa.large_binary()).fill_null(b"")
            elif pa.types.is_date32(t):
                arr = arr.cast(pa.int32()).fill_null(0)
            elif pa.types.is_integer(t) or pa.types.is_floating(t):
                arr = arr.fill_null(0)
            else:
                raise TypeError(f"Cannot hash column {name} of type {t}")
            if pa.types.is_large_string(arr.type) or pa.types.is_large_binary(arr.type):
                # variable width: hash lengths and the data they cover
                buffers = arr.buffers()
                offsets = np.frombuffer(buffers[1], dtype=np.int64)[
                    arr.offset : arr.offset + len(arr) + 1
                ]
                self._update(name, "lengths", np.diff(offsets).tobytes())
                if buffers[2] is not None and len(arr):
                    self._update(name, "data", memoryview(buffers[2])[offsets[0] : offsets[-1]])
            else:
                self._update(name, "values", arr.to_numpy(zero_copy_only=False).tobytes())

    def hexdigest(self):
        h = hashlib.sha256(self.schema_hash.encode())
        for name in self.names:
            for kind in ("valid", "values", "lengths", "data"):
                h.update(f"{name}:{kind}:".encode())
                if (name, kind) in self.streams:
                    h.update(self.streams[(name, kind)].digest())
        return "sha256:" + h.hexdigest()


def _geo_metadata(pyproj_crs_json, geometry_types, bbox):
    column = {
        "encoding": "WKB",
        "crs": pyproj_crs_json,
        "geometry_types": sorted(geometry_types),
    }
    # bbox is optional, but must have 4 (or 6) values when present - omit it
    # when there are no geometries (eg an empty layer)
    if bbox is not None:
        column["bbox"] = list(bbox)
    return {
        "version": GEOPARQUET_VERSION,
        "primary_column": GEOMETRY_COLUMN,
        "columns": {GEOMETRY_COLUMN: column},
        "creator": {"library": "bcdata", "version": bcdata.__version__},
    }


def _check_download(con, staging_path, table, n_features, n_expected, primary_key):
    """Fail if features are missing or duplicated

    Paging against a live table can skip or repeat features if the table is
    edited during the download. These checks only look at what was received, so
    they hold regardless of how requests are paged.
    """
    if n_features != n_expected:
        raise DownloadCheckError(
            f"{table}: expected {n_expected} features, downloaded {n_features}. "
            "The source may have been edited during download - retry"
        )
    if primary_key and n_features:
        pk = _quote(primary_key)
        n_dupes = con.sql(
            f"SELECT count({pk}) - count(DISTINCT {pk}) FROM read_parquet(?)",
            params=[staging_path],
        ).fetchall()[0][0]
        if n_dupes:
            raise DownloadCheckError(
                f"{table}: {n_dupes} duplicated values in primary key {primary_key}. "
                "The source may have been edited during download - retry"
            )


def _write_output(local_path, out_path):
    """Move finished file to a local path or upload to a URI (s3://...)"""
    if "://" in out_path:
        from pyarrow import fs

        filesystem, path = fs.FileSystem.from_uri(out_path)
        # object stores (s3 etc) have no directories to create - and creating the
        # "parent" of a top level key would be an attempt to create a bucket
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent and filesystem.type_name == "local":
            filesystem.create_dir(parent, recursive=True)
        fs.copy_files(local_path, path, destination_filesystem=filesystem)
    else:
        parent = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(parent, exist_ok=True)
        # copy next to destination then rename, so the destination is never partial.
        # Create the temp file with mode 0666 so the OS applies the umask, as for any
        # normally created file (mkstemp would create it readable only by its owner,
        # and changing the umask to read it is not thread safe)
        while True:
            tmp = os.path.join(parent, f".{os.path.basename(out_path)}.{secrets.token_hex(8)}")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
                break
            except FileExistsError:
                continue
        os.close(fd)
        try:
            shutil.copyfile(local_path, tmp)
            os.replace(tmp, out_path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise


def to_parquet(
    dataset,
    out_path=None,
    query=None,
    bounds=None,
    bounds_crs="EPSG:3005",
    count=None,
    sortby=None,
    row_group_size=50000,
    compression="zstd",
    compression_level=None,
    check=True,
):
    """Download a DataBC WFS layer and write it to a GeoParquet 2.0 file.

    Parameters:
        dataset: BC Data Catalogue package name or WFS object/table name
        out_path: local path or URI such as s3://bucket/key.parquet (credentials
            are read from the standard AWS environment variables/config).
            Defaults to <schema>.<table>.parquet (lowercase) in the current folder
        query: CQL/ECQL filter
        bounds: (xmin, ymin, xmax, ymax) filter, in bounds_crs
        bounds_crs: CRS of bounds
        count: maximum number of features to download
        sortby: sort field for requests (defaults to the table's sort key, see
            BCWFS.get_sortkey). Requests are always sorted, even when the download
            fits in one request, so features arrive in the same order every time.
        row_group_size: maximum rows per parquet row group
        compression, compression_level: parquet compression
        check: fail with DownloadCheckError if the number of features received
            differs from the count reported by WFS, or the primary key (when
            known) has duplicates - either indicates features were missed or
            repeated during paging. Nothing is written if a check fails.

    Returns the output path.
    """
    if row_group_size < 1:
        raise ValueError(f"row_group_size must be at least 1, got {row_group_size}")

    WFS = BCWFS()
    table = WFS.validate_name(dataset)
    if not out_path:
        out_path = table.lower() + ".parquet"
    wfs_schema = WFS.get_schema(table)
    columns = Columns(wfs_schema)

    # count expected features independently of how requests are paged
    n_expected = WFS.get_count(
        table,
        query=query,
        bounds=bounds,
        bounds_crs=bounds_crs,
        geom_column=wfs_schema["geometry_column"],
    )
    if count is not None:
        n_expected = min(count, n_expected)

    # define_requests only sorts downloads that need several requests. Sort single
    # request downloads too: unsorted, the server may return features in a different
    # order each time, which changes the order of Hilbert ties in the output and so
    # content_hash, although the data has not changed.
    if not sortby:
        sortby = WFS.get_sortkey(table)
    # pass the count requested above, so it is not requested again (and the requests
    # and the check use the same number)
    urls = WFS.define_requests(
        table,
        query=query,
        bounds=bounds,
        bounds_crs=bounds_crs,
        count=n_expected,
        sortby=sortby,
        check_count=False,
    )

    # catalogue metadata is nice to have, do not fail the download without it
    try:
        table_definition = bcdata.get_table_definition(table)
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not retrieve BCDC table definition for {table}: {e}")
        table_definition = {"description": None, "comments": None, "schema": []}
    primary_key = bcdata.primary_keys.get(table.lower())
    primary_key = columns.names.get(primary_key.upper()) if primary_key else None

    geometry_types = set()
    bbox = None
    n_features = 0

    with tempfile.TemporaryDirectory(prefix="bcdata_") as tmpdir:
        # ---- pass 1: stream WFS pages to staging file ----
        staging_path = os.path.join(tmpdir, "staging.parquet")
        staging_schema = pa.schema(
            columns.page_fields()
            + [
                pa.field(GEOMETRY_COLUMN, pa.binary()),
                pa.field(_HILBERT, pa.int64()),
                pa.field(_SEQ, pa.int64()),
            ]
        )
        with pq.ParquetWriter(staging_path, staging_schema, compression="lz4") as writer:
            for url in urls:
                features = WFS._request_featurecollection(url)["features"]
                if not features:
                    continue
                geoms, present = parse_geometries(features)
                page_types, page_bounds = _geometry_summary(geoms, present)
                geometry_types |= page_types
                if page_bounds is not None:
                    bbox = (
                        page_bounds
                        if bbox is None
                        else np.concatenate(
                            [
                                np.minimum(bbox[:2], page_bounds[:2]),
                                np.maximum(bbox[2:], page_bounds[2:]),
                            ]
                        )
                    )
                arrays = columns.arrays([f["properties"] for f in features])
                arrays.append(pa.array(shapely.to_wkb(geoms).tolist(), type=pa.binary()))
                arrays.append(pa.array(_hilbert_keys(geoms, present), type=pa.int64()))
                arrays.append(pa.array(np.arange(n_features, n_features + len(features))))
                writer.write_batch(pa.RecordBatch.from_arrays(arrays, schema=staging_schema))
                n_features += len(features)
        log.info(f"Downloaded {n_features} features from {table}")
        columns.log_large_integers(table)

        with duckdb.connect(config={"temp_directory": os.path.join(tmpdir, "duckdb")}) as con:
            if check:
                _check_download(con, staging_path, table, n_features, n_expected, primary_key)

            # ---- pass 2: sort, finalize types, write GeoParquet ----
            crs_json = pyproj.CRS.from_epsg(SRID).to_json_dict()
            geom_type = ga.wkb().with_crs(json.dumps(crs_json))
            integer_columns = columns.integer_columns()
            final_fields = columns.fields() + [pa.field(GEOMETRY_COLUMN, geom_type)]

            column_comments = {
                columns.names[c["column_name"]]: c.get("column_comments")
                for c in table_definition["schema"]
                if c.get("column_name") in columns.names
            }
            provenance = {
                "source": table,
                "source_url": WFS.wfs_url,
                "query": query,
                "bounds": list(bounds) if bounds else None,
                "bounds_crs": bounds_crs if bounds else None,
                "count": count,
                "feature_count": n_features,
                "downloaded": datetime.now(tz=UTC).isoformat(timespec="seconds"),
                "bcdata_version": bcdata.__version__,
                "primary_key": primary_key,
                "description": table_definition.get("description"),
                "comments": table_definition.get("comments"),
                "columns": column_comments,
                # source schemas only declare "number" - these columns were written as
                # int64 because all of their values in this download are whole numbers
                "integer_columns_inferred": [
                    f.name for f in final_fields if f.name in integer_columns
                ],
            }
            final_schema = pa.schema(final_fields).with_metadata(
                {
                    "geo": json.dumps(
                        _geo_metadata(
                            crs_json, geometry_types, None if bbox is None else bbox.tolist()
                        )
                    )
                }
            )
            provenance["schema_hash"] = _schema_hash(final_fields)
            hasher = _ContentHasher(provenance["schema_hash"], final_fields)

            select = ", ".join(_quote(f.name) for f in final_fields)
            relation = con.sql(
                f"SELECT {select} FROM read_parquet(?) ORDER BY {_HILBERT}, {_SEQ}",
                params=[staging_path],
            )
            # duckdb >= 2.0: to_arrow_reader, 1.x: fetch_arrow_reader
            if hasattr(relation, "to_arrow_reader"):
                reader = relation.to_arrow_reader(row_group_size)
            else:
                reader = relation.fetch_arrow_reader(row_group_size)

            def finalize(batch):
                arrays = []
                for field in final_fields:
                    arr = batch.column(field.name)
                    if field.name == GEOMETRY_COLUMN:
                        arr = pa.ExtensionArray.from_storage(geom_type, arr)
                    elif arr.type != field.type:
                        arr = arr.cast(field.type)
                    arrays.append(arr)
                return pa.Table.from_arrays(arrays, schema=final_schema)

            final_path = os.path.join(tmpdir, "final.parquet")
            with pq.ParquetWriter(
                final_path,
                final_schema,
                compression=compression,
                compression_level=compression_level,
            ) as writer:

                def write(table):
                    hasher.update(table)
                    writer.write_table(table)

                # duckdb does not honour the requested batch size, accumulate
                # batches so that each write is one full row group
                pending, pending_rows = [], 0
                for batch in reader:
                    pending.append(finalize(batch))
                    pending_rows += batch.num_rows
                    if pending_rows >= row_group_size:
                        chunk = pa.concat_tables(pending).combine_chunks()
                        while chunk.num_rows >= row_group_size:
                            write(chunk.slice(0, row_group_size))
                            chunk = chunk.slice(row_group_size)
                        pending, pending_rows = [chunk], chunk.num_rows
                if pending_rows:
                    write(pa.concat_tables(pending).combine_chunks())
                # provenance includes the content hash, so it is only known once every
                # row is written - add it to the file footer rather than the schema
                provenance["content_hash"] = hasher.hexdigest()
                writer.add_key_value_metadata({"bcdata": json.dumps(provenance)})

        _write_output(final_path, out_path)
    log.info(f"Wrote {n_features} features to {out_path}")
    return out_path
