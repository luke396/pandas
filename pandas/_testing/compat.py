"""
Helpers for sharing tests between DataFrame/Series
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pandas import DataFrame

if TYPE_CHECKING:
    from pandas._typing import DtypeObj


def get_dtype(obj) -> DtypeObj:
    return obj.dtypes.iat[0] if isinstance(obj, DataFrame) else obj.dtype


def get_obj(df: DataFrame, klass):
    """
    For sharing tests using frame_or_series, either return the DataFrame
    unchanged or return it's first column as a Series.
    """
    return df if klass is DataFrame else df._ixs(0, axis=1)
