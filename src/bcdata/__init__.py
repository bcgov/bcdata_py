from ._tables import load_table_metadata
from ._version import __version__ as __version__
from .bc2pg import bc2pg as bc2pg
from .bcdc import get_table_definition as get_table_definition
from .bcdc import get_table_name as get_table_name
from .parquet import to_parquet as to_parquet
from .wfs import get_count as get_count
from .wfs import get_data as get_data
from .wfs import get_sortkey as get_sortkey
from .wfs import list_tables as list_tables
from .wfs import validate_name as validate_name

# BCDC does not indicate which column in a table is the primary key (and has a few
# other gaps/quirks), so bcdata bundles its own table metadata - see tables.toml
table_metadata = load_table_metadata()
primary_keys = {
    table: fields["primary_key"]
    for table, fields in table_metadata.items()
    if "primary_key" in fields
}
