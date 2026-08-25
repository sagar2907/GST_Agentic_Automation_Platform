"""Reading a real purchase register into the domain model.

The rest of this system has only ever seen data its own generator produced,
which is data that is correct by construction. This is where data nobody
validated gets in, so the module's disposition is different from every other:
it assumes the file is wrong and has to be talked out of it.
"""

from gst_recon.ingest.coerce import CoercionError, DateColumn, DateOrder, infer_date_order
from gst_recon.ingest.loader import (
    IngestRefusedError,
    IngestReport,
    RowRejection,
    load_purchase_register,
    read_table,
)
from gst_recon.ingest.schema import ColumnMap, map_headers, normalise_heading
from gst_recon.ingest.xlsx import Sheet, XlsxError, read_sheet

__all__ = [
    "CoercionError",
    "ColumnMap",
    "DateColumn",
    "DateOrder",
    "IngestRefusedError",
    "IngestReport",
    "RowRejection",
    "Sheet",
    "XlsxError",
    "infer_date_order",
    "load_purchase_register",
    "map_headers",
    "normalise_heading",
    "read_sheet",
    "read_table",
]
