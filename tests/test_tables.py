import pytest

import bcdata
from bcdata._tables import load_table_metadata


def test_bundled_metadata_loads():
    assert bcdata.table_metadata
    assert bcdata.primary_keys["whse_fish.pscis_assessment_svw"] == "stream_crossing_id"
    # tables with metadata but no primary key are not in primary_keys
    assert "whse_admin_boundaries.adm_nr_districts_spg" in bcdata.table_metadata
    assert "whse_admin_boundaries.adm_nr_districts_spg" not in bcdata.primary_keys


def test_load_minimal():
    tables = load_table_metadata('["a.b"]\nprimary_key = "id"\n')
    assert tables == {"a.b": {"primary_key": "id"}}


@pytest.mark.parametrize(
    "text, message",
    [
        ('["a.b"]\nprimay_key = "id"\n', "Unknown field"),
        ('["A.B"]\nprimary_key = "id"\n', "lowercase"),
        ('["a.b"]\nprimary_key = "id"\nsort_key = "id"\n', "same column"),
        ('["a.b"]\nprimary_key = "ID"\n', "lowercase"),
        ('["a.b"]\nsort_key = "Id"\n', "lowercase"),
        ('["a.b"]\nbcdc_object_name = "A.B_SP"\n', "lowercase"),
        ('["a.b"]\nprimary_key = 5\n', "non-empty string"),
        ('["a.b"]\nprimary_key = ""\n', "non-empty string"),
        ('"a.b" = "id"\n', "table of fields"),
        ('["a.b"]\nprimary_key = "id"\n["a.b"]\nsort_key = "x"\n', "twice"),
    ],
)
def test_load_invalid(text, message):
    with pytest.raises(ValueError, match=message):
        load_table_metadata(text)


def test_sort_key_is_not_a_primary_key():
    # an unstable unique id is used for sorting, but never exposed as a primary key
    table = "whse_fish.fiss_fish_obsrvtn_pnt_sp"
    assert bcdata.table_metadata[table]["sort_key"] == "fish_observation_point_id"
    assert table not in bcdata.primary_keys


def test_get_sortkey_prefers_sort_key():
    assert bcdata.get_sortkey("whse_fish.fiss_fish_obsrvtn_pnt_sp") == "FISH_OBSERVATION_POINT_ID"
