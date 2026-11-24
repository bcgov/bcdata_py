import logging
from urllib.parse import urlparse

import requests
import stamina

import bcdata
from bcdata.wfs import REQUEST_TIMEOUT, RETRY_POLICY, RETRYABLE_STATUS_CODES, _is_retryable

log = logging.getLogger(__name__)

BCDC_API_URL = "https://catalogue.data.gov.bc.ca/api/3/action/"


class ServiceException(Exception):
    pass


# Same timeouts and retry policy as the WFS requests (REQUEST_TIMEOUT, RETRY_POLICY
# in bcdata.wfs): retry only on transient errors (5xx, 429, connection errors, timeouts)


def _check_response(r):
    """Log and raise on any non-success response; retryable codes are retried by stamina"""
    if r.ok:
        log.debug(r.text)
        return r
    if r.status_code in RETRYABLE_STATUS_CODES:
        log.warning(f"HTTP error: {r.status_code}, retrying")
    else:
        log.error(f"HTTP error {r.status_code}")
    log.debug(f"Response headers: {r.headers}")
    log.debug(f"Response text: {r.text}")
    r.raise_for_status()
    return r


@stamina.retry(on=_is_retryable, **RETRY_POLICY)
def _package_show(package):
    r = requests.get(BCDC_API_URL + "package_show", params={"id": package}, timeout=REQUEST_TIMEOUT)
    if r.status_code in [400, 404]:
        raise ValueError(f"Dataset {package} not found in DataBC API list")
    return _check_response(r)


@stamina.retry(on=_is_retryable, **RETRY_POLICY)
def _table_definition(table_name):
    r = requests.get(
        BCDC_API_URL + "package_search",
        params={"q": "res_extras_object_name:" + table_name},
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code in [400, 401, 404]:
        raise ServiceException(r.text)  # presumed request error
    return _check_response(r)


def get_table_name(package):
    """Query DataBC API to find WFS table/layer name for given package"""
    package = package.lower()  # package names are lowercase
    r = _package_show(package)
    result = r.json()["result"]
    # Because the object_name in the result json is not a 100% reliable key
    # for WFS requests, parse URL in WMS resource(s).
    # Also, some packages may have >1 WFS layer - if this is the case, bail
    # and provide user with a list of layers
    layer_urls = [r["url"] for r in result["resources"] if r["format"] == "wms"]
    layer_names = [urlparse(url).path.split("/")[3] for url in layer_urls]
    if len(layer_names) > 1:
        raise ValueError(
            "Package {} includes more than one WFS resource, specify one of the following: \n{}".format(
                package, "\n".join(layer_names)
            )
        )
    if not layer_names:
        raise ValueError(f"Package {package} has no WMS resource, cannot determine WFS table name")
    return layer_names[0]


def _resource_matches(table_name, resource):
    """Return True if resource is a geographic resource for table_name with a schema present"""
    # the catalogue lists a few tables under a different object name (see tables.toml)
    bcdc_object_name = bcdata.table_metadata.get(table_name.lower(), {}).get(
        "bcdc_object_name", table_name
    )
    object_name = (resource.get("object_name") or "").upper()
    return (
        resource.get("bcdc_type") == "geographic"
        and object_name in (table_name.upper(), bcdc_object_name.upper())
        and bool(resource.get("details"))
    )


def get_table_definition(table_name):
    """
    Given a table/object name, search BCDC for the first package/resource with a matching "object_name",
    returns dict: {"comments": <>, "notes": <>, "schema": {<schema dict>} }
    """
    # only allow searching for tables present in WFS list
    table_name = table_name.upper()
    if table_name not in bcdata.list_tables():
        raise ValueError(f"Only tables available via WFS are supported, {table_name} not found")

    # search the api for the provided table
    r = _table_definition(table_name)

    # start with an empty table definition dict
    table_definition = {
        "description": None,
        "comments": None,
        "schema": [],
        "primary_key": None,
    }

    results = r.json()["result"]["results"]
    # if there are no matching results, let the user know
    if not results:
        log.warning(f"BC Data Catalogue API search provides no results for: {table_name}")
    else:
        # use the first package/resource matching the table, taking the description
        # (package level), schema and comments (resource level) all from that match
        match = next(
            (
                (package, resource)
                for package in results
                for resource in package["resources"]
                if _resource_matches(table_name, resource)
            ),
            None,
        )
        if match:
            package, resource = match
            table_definition["description"] = package["notes"]
            table_definition["schema"] = resource["details"]
            table_definition["comments"] = resource.get("object_table_comments")
        elif len(results) == 1:
            # no schema found, but the description is unambiguous
            table_definition["description"] = results[0]["notes"]

    if not table_definition["schema"]:
        log.warning(f"BC Data Catalouge API search provides no schema for: {table_name}")

    # add primary key if present in bcdata.primary_keys
    if table_name.lower() in bcdata.primary_keys:
        table_definition["primary_key"] = bcdata.primary_keys[table_name.lower()].upper()

    return table_definition
