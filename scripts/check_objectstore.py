"""Check that scripts/replicate.py can use an object store as its destination.

Writes a few files under a test prefix with replicate.py's Destination (obstore),
reads them back, then deletes them. Run it before the first replication to a new
store, eg the NRS objectstore (Dell ECS) or Azure Blob Storage:

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
    AWS_ENDPOINT_URL=https://nrs.objectstore.gov.bc.ca AWS_REGION=us-east-1 \\
    uv run --group replicate python scripts/check_objectstore.py s3://bchamp/bcdata/_test

Credentials and endpoints come from the environment, as for replicate.py. The
destination is the argument, or the BCDATA_REPLICATION_DEST environment variable.
Use a prefix that holds nothing else: files there are overwritten and deleted.

Exit codes:
    0  every required check passed
    1  one or more required checks failed
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import obstore
import pyarrow
import pyarrow.parquet as pq

_spec = importlib.util.spec_from_file_location(
    "replicate", Path(__file__).resolve().parent / "replicate.py"
)
replicate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replicate)


def write_parquet(path, rows):
    """A parquet file with a bcdata footer, returning its size"""
    table = pyarrow.table({"id": range(rows), "value": [f"value {i}" for i in range(rows)]})
    meta = {"bcdata": json.dumps({"content_hash": "sha256:test", "feature_count": rows})}
    pq.write_table(table.replace_schema_metadata(meta), path)
    return os.path.getsize(path)


def check(name, fn):
    """Run one check, print its result and return True if it passed"""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 - report any failure and go on to the next check
        print(f"FAIL  {name}: {type(e).__name__}: {e}")
        return False
    print(f"ok    {name}" + (f": {result}" if result is not None else ""))
    return True


def conditional_put(dest, key):
    """Whether the store refuses a write conditional on a stale ETag.

    replicate.py doesn't use conditional writes (yet): they would protect the change
    log's read-modify-write from overlapping runs.
    """
    etag = obstore.head(dest.store, key)["e_tag"]
    try:
        obstore.put(dest.store, key, b"{}", mode={"e_tag": etag})
    except NotImplementedError:
        return "not supported by this store"
    try:
        obstore.put(dest.store, key, b"{}", mode={"e_tag": etag})
    except obstore.exceptions.PreconditionError:
        return "supported (a write with a stale ETag was refused)"
    return "NOT enforced (a write with a stale ETag was accepted)"


def main(uri):
    dest = replicate.Destination(uri)
    print(f"checking {dest.uri}\n")
    created = []
    passed = []
    with tempfile.TemporaryDirectory() as tmp:
        small = os.path.join(tmp, "small.parquet")
        large = os.path.join(tmp, "large.parquet")
        small_size = write_parquet(small, 1000)
        large_size = write_parquet(large, 1_500_000)  # over 5 MB: multipart upload

        def write_json():
            dest.write_json({"check": True}, "state.json")
            created.append("state.json")

        def upload(local, *parts):
            dest.upload(local, *parts)
            created.append(dest.key(*parts))

        def download():
            out = os.path.join(tmp, "downloaded.parquet")
            dest.download(out, "changes", "large.parquet")
            if Path(out).read_bytes() != Path(large).read_bytes():
                raise ValueError("downloaded file differs from the uploaded one")
            return f"{os.path.getsize(out)} bytes, identical"

        passed += [
            check("write json", write_json),
            check("read json", lambda: dest.read_json("state.json")),
            check("read json (missing file)", lambda: dest.read_json("missing.json")),
            check(f"upload ({small_size} bytes)", lambda: upload(small, "small.parquet")),
            check(
                f"upload, multipart ({large_size} bytes)",
                lambda: upload(large, "changes", "large.parquet"),
            ),
            check(
                "exists (file, missing file)",
                lambda: (dest.exists("small.parquet"), dest.exists("missing.parquet")),
            ),
            check(
                "read footer (range requests)",
                lambda: dest.read_bcdata_metadata("changes", "large.parquet"),
            ),
            check(
                "read footer (missing file)",
                lambda: dest.read_bcdata_metadata("missing.parquet"),
            ),
            check("download", download),
        ]
        if "state.json" in created:
            check("conditional put (informational)", lambda: conditional_put(dest, "state.json"))

    for key in created:
        check(f"delete {key}", lambda key=key: obstore.delete(dest.store, key))
    ok = all(passed)
    print("\nall required checks passed" if ok else "\nsome required checks FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    uri = sys.argv[1] if len(sys.argv) == 2 else os.environ.get(replicate.DEST_ENV)
    if len(sys.argv) > 2 or not uri:
        sys.exit(__doc__)
    sys.exit(main(uri))
