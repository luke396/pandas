from __future__ import annotations

import abc
from collections import defaultdict
from contextlib import nullcontext
from functools import partial
import inspect
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ContextManager,
    DefaultDict,
    Dict,
    Hashable,
    Iterable,
    Iterator,
    List,
    Sequence,
    cast,
)
import warnings

import numpy as np

from pandas._config import option_context

from pandas._typing import (
    AggFuncType,
    AggFuncTypeBase,
    AggFuncTypeDict,
    AggObjType,
    Axis,
    AxisInt,
    NDFrameT,
    npt,
)
from pandas.errors import SpecificationError
from pandas.util._decorators import cache_readonly
from pandas.util._exceptions import find_stack_level

from pandas.core.dtypes.cast import is_nested_object
from pandas.core.dtypes.common import (
    is_dict_like,
    is_list_like,
    is_sequence,
)
from pandas.core.dtypes.dtypes import (
    CategoricalDtype,
    ExtensionDtype,
)
from pandas.core.dtypes.generic import (
    ABCDataFrame,
    ABCNDFrame,
    ABCSeries,
)

from pandas.core.algorithms import safe_sort
from pandas.core.base import SelectionMixin
import pandas.core.common as com
from pandas.core.construction import ensure_wrapped_if_datetimelike

if TYPE_CHECKING:
    from pandas import (
        DataFrame,
        Index,
        Series,
    )
    from pandas.core.groupby import GroupBy
    from pandas.core.resample import Resampler
    from pandas.core.window.rolling import BaseWindow


ResType = Dict[int, Any]


def frame_apply(
    obj: DataFrame,
    func: AggFuncType,
    axis: Axis = 0,
    raw: bool = False,
    result_type: str | None = None,
    args=None,
    kwargs=None,
) -> FrameApply:
    """construct and return a row or column based frame apply object"""
    axis = obj._get_axis_number(axis)
    klass: type[FrameApply]
    if axis == 0:
        klass = FrameRowApply
    elif axis == 1:
        klass = FrameColumnApply

    return klass(
        obj,
        func,
        raw=raw,
        result_type=result_type,
        args=args,
        kwargs=kwargs,
    )


class Apply(metaclass=abc.ABCMeta):
    axis: AxisInt

    def __init__(
        self,
        obj: AggObjType,
        func,
        raw: bool,
        result_type: str | None,
        args,
        kwargs,
    ) -> None:
        self.obj = obj
        self.raw = raw
        self.args = args or ()
        self.kwargs = kwargs or {}

        if result_type not in [None, "reduce", "broadcast", "expand"]:
            raise ValueError(
                "invalid value for result_type, must be one "
                "of {None, 'reduce', 'broadcast', 'expand'}"
            )

        self.result_type = result_type

        # curry if needed
        if (
            (kwargs or args)
            and not isinstance(func, (np.ufunc, str))
            and not is_list_like(func)
        ):

            def f(x):
                return func(x, *args, **kwargs)

        else:
            f = func

        self.orig_f: AggFuncType = func
        self.f: AggFuncType = f

    @abc.abstractmethod
    def apply(self) -> DataFrame | Series:
        pass

    def agg(self) -> DataFrame | Series | None:
        """
        Provide an implementation for the aggregators.

        Returns
        -------
        Result of aggregation, or None if agg cannot be performed by
        this method.
        """
        obj = self.obj
        arg = self.f
        args = self.args
        kwargs = self.kwargs

        if isinstance(arg, str):
            return self.apply_str()

        if is_dict_like(arg):
            return self.agg_dict_like()
        elif is_list_like(arg):
            # we require a list, but not a 'str'
            return self.agg_list_like()

        if callable(arg):
            f = com.get_cython_func(arg)
            if f and not args and not kwargs:
                return getattr(obj, f)()

        # caller can react
        return None

    def transform(self) -> DataFrame | Series:
        """
        Transform a DataFrame or Series.

        Returns
        -------
        DataFrame or Series
            Result of applying ``func`` along the given axis of the
            Series or DataFrame.

        Raises
        ------
        ValueError
            If the transform function fails or does not transform.
        """
        obj = self.obj
        func = self.orig_f
        axis = self.axis
        args = self.args
        kwargs = self.kwargs

        is_series = obj.ndim == 1

        if obj._get_axis_number(axis) == 1:
            assert not is_series
            return obj.T.transform(func, 0, *args, **kwargs).T

        if is_list_like(func) and not is_dict_like(func):
            func = cast(List[AggFuncTypeBase], func)
            # Convert func equivalent dict
            if is_series:
                func = {com.get_callable_name(v) or v: v for v in func}
            else:
                func = {col: func for col in obj}

        if is_dict_like(func):
            func = cast(AggFuncTypeDict, func)
            return self.transform_dict_like(func)

        # func is either str or callable
        func = cast(AggFuncTypeBase, func)
        try:
            result = self.transform_str_or_callable(func)
        except TypeError:
            raise
        except Exception as err:
            raise ValueError("Transform function failed") from err

        # Functions that transform may return empty Series/DataFrame
        # when the dtype is not appropriate
        if (
            isinstance(result, (ABCSeries, ABCDataFrame))
            and result.empty
            and not obj.empty
        ):
            raise ValueError("Transform function failed")
        # error: Argument 1 to "__get__" of "AxisProperty" has incompatible type
        # "Union[Series, DataFrame, GroupBy[Any], SeriesGroupBy,
        # DataFrameGroupBy, BaseWindow, Resampler]"; expected "Union[DataFrame,
        # Series]"
        if not isinstance(result, (ABCSeries, ABCDataFrame)) or not result.index.equals(
            obj.index  # type: ignore[arg-type]
        ):
            raise ValueError("Function did not transform")

        return result

    def transform_dict_like(self, func):
        """
        Compute transform in the case of a dict-like func
        """
        from pandas.core.reshape.concat import concat

        obj = self.obj
        args = self.args
        kwargs = self.kwargs

        # transform is currently only for Series/DataFrame
        assert isinstance(obj, ABCNDFrame)

        if len(func) == 0:
            raise ValueError("No transform functions were provided")

        func = self.normalize_dictlike_arg("transform", obj, func)

        results: dict[Hashable, DataFrame | Series] = {}
        for name, how in func.items():
            colg = obj._gotitem(name, ndim=1)
            results[name] = colg.transform(how, 0, *args, **kwargs)
        return concat(results, axis=1)

    def transform_str_or_callable(self, func) -> DataFrame | Series:
        """
        Compute transform in the case of a string or callable func
        """
        obj = self.obj
        args = self.args
        kwargs = self.kwargs

        if isinstance(func, str):
            return self._try_aggregate_string_function(obj, func, *args, **kwargs)

        if not args and not kwargs:
            if f := com.get_cython_func(func):
                return getattr(obj, f)()

        # Two possible ways to use a UDF - apply or call directly
        try:
            return obj.apply(func, args=args, **kwargs)
        except Exception:
            return func(obj, *args, **kwargs)

    def agg_list_like(self) -> DataFrame | Series:
        """
        Compute aggregation in the case of a list-like argument.

        Returns
        -------
        Result of aggregation.
        """
        from pandas.core.groupby.generic import (
            DataFrameGroupBy,
            SeriesGroupBy,
        )
        from pandas.core.reshape.concat import concat

        obj = self.obj
        arg = cast(List[AggFuncTypeBase], self.f)

        if getattr(obj, "axis", 0) == 1:
            raise NotImplementedError("axis other than 0 is not supported")

        if not isinstance(obj, SelectionMixin):
            # i.e. obj is Series or DataFrame
            selected_obj = obj
        elif obj._selected_obj.ndim == 1:
            # For SeriesGroupBy this matches _obj_with_exclusions
            selected_obj = obj._selected_obj
        else:
            selected_obj = obj._obj_with_exclusions

        results = []
        keys = []

        is_groupby = isinstance(obj, (DataFrameGroupBy, SeriesGroupBy))
        context_manager: ContextManager
        if is_groupby:
            # When as_index=False, we combine all results using indices
            # and adjust index after
            context_manager = com.temp_setattr(obj, "as_index", True)
        else:
            context_manager = nullcontext()
        with context_manager:
            # degenerate case
            if selected_obj.ndim == 1:
                for a in arg:
                    colg = obj._gotitem(selected_obj.name, ndim=1, subset=selected_obj)
                    if isinstance(colg, (ABCSeries, ABCDataFrame)):
                        new_res = colg.aggregate(
                            a, self.axis, *self.args, **self.kwargs
                        )
                    else:
                        new_res = colg.aggregate(a, *self.args, **self.kwargs)
                    results.append(new_res)

                    # make sure we find a good name
                    name = com.get_callable_name(a) or a
                    keys.append(name)

            else:
                indices = []
                for index, col in enumerate(selected_obj):
                    colg = obj._gotitem(col, ndim=1, subset=selected_obj.iloc[:, index])
                    if isinstance(colg, (ABCSeries, ABCDataFrame)):
                        new_res = colg.aggregate(
                            arg, self.axis, *self.args, **self.kwargs
                        )
                    else:
                        new_res = colg.aggregate(arg, *self.args, **self.kwargs)
                    results.append(new_res)
                    indices.append(index)
                keys = selected_obj.columns.take(indices)

        try:
            return concat(results, keys=keys, axis=1, sort=False)
        except TypeError as err:
            # we are concatting non-NDFrame objects,
            # e.g. a list of scalars
            from pandas import Series

            result = Series(results, index=keys, name=obj.name)
            if is_nested_object(result):
                raise ValueError(
                    "cannot combine transform and aggregation operations"
                ) from err
            return result

    def agg_dict_like(self) -> DataFrame | Series:
        """
        Compute aggregation in the case of a dict-like argument.

        Returns
        -------
        Result of aggregation.
        """
        from pandas import Index
        from pandas.core.groupby.generic import (
            DataFrameGroupBy,
            SeriesGroupBy,
        )
        from pandas.core.reshape.concat import concat

        obj = self.obj
        arg = cast(AggFuncTypeDict, self.f)

        if getattr(obj, "axis", 0) == 1:
            raise NotImplementedError("axis other than 0 is not supported")

        if not isinstance(obj, SelectionMixin):
            # i.e. obj is Series or DataFrame
            selected_obj = obj
            selection = None
        else:
            selected_obj = obj._selected_obj
            selection = obj._selection

        arg = self.normalize_dictlike_arg("agg", selected_obj, arg)

        is_groupby = isinstance(obj, (DataFrameGroupBy, SeriesGroupBy))
        context_manager: ContextManager
        if is_groupby:
            # When as_index=False, we combine all results using indices
            # and adjust index after
            context_manager = com.temp_setattr(obj, "as_index", True)
        else:
            context_manager = nullcontext()

        is_non_unique_col = (
            selected_obj.ndim == 2
            and selected_obj.columns.nunique() < len(selected_obj.columns)
        )

        with context_manager:
            if selected_obj.ndim == 1:
                # key only used for output
                colg = obj._gotitem(selection, ndim=1)
                result_data = [colg.agg(how) for _, how in arg.items()]
                result_index = list(arg.keys())
            elif is_non_unique_col:
                # key used for column selection and output
                # GH#51099
                result_data = []
                result_index = []
                for key, how in arg.items():
                    indices = selected_obj.columns.get_indexer_for([key])
                    labels = selected_obj.columns.take(indices)
                    label_to_indices = defaultdict(list)
                    for index, label in zip(indices, labels):
                        label_to_indices[label].append(index)

                    key_data = [
                        selected_obj._ixs(indice, axis=1).agg(how)
                        for label, indices in label_to_indices.items()
                        for indice in indices
                    ]

                    result_index += [key] * len(key_data)
                    result_data += key_data
            else:
                # key used for column selection and output
                result_data = [
                    obj._gotitem(key, ndim=1).agg(how) for key, how in arg.items()
                ]
                result_index = list(arg.keys())

        # Avoid making two isinstance calls in all and any below
        is_ndframe = [isinstance(r, ABCNDFrame) for r in result_data]

        # combine results
        if all(is_ndframe):
            results = dict(zip(result_index, result_data))
            keys_to_use: Iterable[Hashable]
            keys_to_use = [k for k in result_index if not results[k].empty]
            # Have to check, if at least one DataFrame is not empty.
            keys_to_use = keys_to_use if keys_to_use != [] else result_index
            if selected_obj.ndim == 2:
                # keys are columns, so we can preserve names
                ktu = Index(keys_to_use)
                ktu._set_names(selected_obj.columns.names)
                keys_to_use = ktu

            axis: AxisInt = 0 if isinstance(obj, ABCSeries) else 1
            result = concat(
                {k: results[k] for k in keys_to_use},
                axis=axis,
                keys=keys_to_use,
            )
        elif any(is_ndframe):
            # There is a mix of NDFrames and scalars
            raise ValueError(
                "cannot perform both aggregation "
                "and transformation operations "
                "simultaneously"
            )
        else:
            from pandas import Series

            # we have a list of scalars
            # GH 36212 use name only if obj is a series
            if obj.ndim == 1:
                obj = cast("Series", obj)
                name = obj.name
            else:
                name = None

            result = Series(result_data, index=result_index, name=name)

        return result

    def apply_str(self) -> DataFrame | Series:
        """
        Compute apply in case of a string.

        Returns
        -------
        result: Series or DataFrame
        """
        # Caller is responsible for checking isinstance(self.f, str)
        f = cast(str, self.f)

        obj = self.obj

        from pandas.core.groupby.generic import (
            DataFrameGroupBy,
            SeriesGroupBy,
        )

        # Support for `frame.transform('method')`
        # Some methods (shift, etc.) require the axis argument, others
        # don't, so inspect and insert if necessary.
        func = getattr(obj, f, None)
        if callable(func):
            sig = inspect.getfullargspec(func)
            arg_names = (*sig.args, *sig.kwonlyargs)
            if self.axis != 0 and (
                "axis" not in arg_names or f in ("corrwith", "skew")
            ):
                raise ValueError(f"Operation {f} does not support axis=1")
            if "axis" in arg_names:
                if isinstance(obj, (SeriesGroupBy, DataFrameGroupBy)):
                    default_axis = self.obj.axis if f in ["idxmax", "idxmin"] else 0
                    if default_axis != self.axis:
                        self.kwargs["axis"] = self.axis
                else:
                    self.kwargs["axis"] = self.axis
        return self._try_aggregate_string_function(obj, f, *self.args, **self.kwargs)

    def apply_multiple(self) -> DataFrame | Series:
        """
        Compute apply in case of a list-like or dict-like.

        Returns
        -------
        result: Series, DataFrame, or None
            Result when self.f is a list-like or dict-like, None otherwise.
        """
        return self.obj.aggregate(self.f, self.axis, *self.args, **self.kwargs)

    def normalize_dictlike_arg(
        self, how: str, obj: DataFrame | Series, func: AggFuncTypeDict
    ) -> AggFuncTypeDict:
        """
        Handler for dict-like argument.

        Ensures that necessary columns exist if obj is a DataFrame, and
        that a nested renamer is not passed. Also normalizes to all lists
        when values consists of a mix of list and non-lists.
        """
        assert how in {"apply", "agg", "transform"}

        # Can't use func.values(); wouldn't work for a Series
        if (
            how == "agg"
            and isinstance(obj, ABCSeries)
            and any(is_list_like(v) for _, v in func.items())
        ) or (any(is_dict_like(v) for _, v in func.items())):
            # GH 15931 - deprecation of renaming keys
            raise SpecificationError("nested renamer is not supported")

        if obj.ndim != 1:
            # Check for missing columns on a frame
            cols = set(func.keys()) - set(obj.columns)
            if len(cols) > 0:
                cols_sorted = list(safe_sort(list(cols)))
                raise KeyError(f"Column(s) {cols_sorted} do not exist")

        aggregator_types = (list, tuple, dict)

        # if we have a dict of any non-scalars
        # eg. {'A' : ['mean']}, normalize all to
        # be list-likes
        # Cannot use func.values() because arg may be a Series
        if any(isinstance(x, aggregator_types) for _, x in func.items()):
            new_func: AggFuncTypeDict = {
                k: v if isinstance(v, aggregator_types) else [v]
                for k, v in func.items()
            }
            func = new_func
        return func

    def _try_aggregate_string_function(self, obj, arg: str, *args, **kwargs):
        """
        if arg is a string, then try to operate on it:
        - try to find a function (or attribute) on ourselves
        - try to find a numpy function
        - raise
        """
        assert isinstance(arg, str)

        f = getattr(obj, arg, None)
        if f is not None:
            if callable(f):
                return f(*args, **kwargs)

            # people may try to aggregate on a non-callable attribute
            # but don't let them think they can pass args to it
            assert not args
            assert not [kwarg for kwarg in kwargs if kwarg not in ["axis"]]
            return f

        f = getattr(np, arg, None)
        if f is not None and hasattr(obj, "__array__"):
            # in particular exclude Window
            return f(obj, *args, **kwargs)

        raise AttributeError(
            f"'{arg}' is not a valid function for '{type(obj).__name__}' object"
        )


class NDFrameApply(Apply):
    """
    Methods shared by FrameApply and SeriesApply but
    not GroupByApply or ResamplerWindowApply
    """

    obj: DataFrame | Series

    @property
    def index(self) -> Index:
        return self.obj.index

    @property
    def agg_axis(self) -> Index:
        return self.obj._get_agg_axis(self.axis)


class FrameApply(NDFrameApply):
    obj: DataFrame

    # ---------------------------------------------------------------
    # Abstract Methods

    @property
    @abc.abstractmethod
    def result_index(self) -> Index:
        pass

    @property
    @abc.abstractmethod
    def result_columns(self) -> Index:
        pass

    @property
    @abc.abstractmethod
    def series_generator(self) -> Iterator[Series]:
        pass

    @abc.abstractmethod
    def wrap_results_for_axis(
        self, results: ResType, res_index: Index
    ) -> DataFrame | Series:
        pass

    # ---------------------------------------------------------------

    @property
    def res_columns(self) -> Index:
        return self.result_columns

    @property
    def columns(self) -> Index:
        return self.obj.columns

    @cache_readonly
    def values(self):
        return self.obj.values

    def apply(self) -> DataFrame | Series:
        """compute the results"""
        # dispatch to agg
        if is_list_like(self.f):
            return self.apply_multiple()

        # all empty
        if len(self.columns) == 0 and len(self.index) == 0:
            return self.apply_empty_result()

        # string dispatch
        if isinstance(self.f, str):
            return self.apply_str()

        # ufunc
        elif isinstance(self.f, np.ufunc):
            with np.errstate(all="ignore"):
                results = self.obj._mgr.apply("apply", func=self.f)
            # _constructor will retain self.index and self.columns
            return self.obj._constructor(data=results)

        # broadcasting
        if self.result_type == "broadcast":
            return self.apply_broadcast(self.obj)

        # one axis empty
        elif not all(self.obj.shape):
            return self.apply_empty_result()

        # raw
        elif self.raw:
            return self.apply_raw()

        return self.apply_standard()

    def agg(self):
        obj = self.obj
        axis = self.axis

        # TODO: Avoid having to change state
        self.obj = self.obj if self.axis == 0 else self.obj.T
        self.axis = 0

        result = None
        try:
            result = super().agg()
        finally:
            self.obj = obj
            self.axis = axis

        if axis == 1:
            result = result.T if result is not None else result

        if result is None:
            result = self.obj.apply(self.orig_f, axis, args=self.args, **self.kwargs)

        return result

    def apply_empty_result(self):
        """
        we have an empty result; at least 1 axis is 0

        we will try to apply the function to an empty
        series in order to see if this is a reduction function
        """
        assert callable(self.f)

        # we are not asked to reduce or infer reduction
        # so just return a copy of the existing object
        if self.result_type not in ["reduce", None]:
            return self.obj.copy()

        # we may need to infer
        should_reduce = self.result_type == "reduce"

        from pandas import Series

        if not should_reduce:
            try:
                if self.axis == 0:
                    r = self.f(Series([], dtype=np.float64))
                else:
                    r = self.f(Series(index=self.columns, dtype=np.float64))
            except Exception:
                pass
            else:
                should_reduce = not isinstance(r, Series)

        if should_reduce:
            r = self.f(Series([], dtype=np.float64)) if len(self.agg_axis) else np.nan
            return self.obj._constructor_sliced(r, index=self.agg_axis)
        else:
            return self.obj.copy()

    def apply_raw(self):
        """apply to the values as a numpy array"""

        def wrap_function(func):
            """
            Wrap user supplied function to work around numpy issue.

            see https://github.com/numpy/numpy/issues/8352
            """

            def wrapper(*args, **kwargs):
                result = func(*args, **kwargs)
                if isinstance(result, str):
                    result = np.array(result, dtype=object)
                return result

            return wrapper

        result = np.apply_along_axis(wrap_function(self.f), self.axis, self.values)

        # TODO: mixed type case
        if result.ndim == 2:
            return self.obj._constructor(result, index=self.index, columns=self.columns)
        else:
            return self.obj._constructor_sliced(result, index=self.agg_axis)

    def apply_broadcast(self, target: DataFrame) -> DataFrame:
        assert callable(self.f)

        result_values = np.empty_like(target.values)

        # axis which we want to compare compliance
        result_compare = target.shape[0]

        for i, col in enumerate(target.columns):
            res = self.f(target[col])
            ares = np.asarray(res).ndim

            # must be a scalar or 1d
            if ares > 1:
                raise ValueError("too many dims to broadcast")
            if ares == 1 and result_compare != len(res):
                raise ValueError("cannot broadcast result")

            result_values[:, i] = res

        return self.obj._constructor(
            result_values, index=target.index, columns=target.columns
        )

    def apply_standard(self):
        results, res_index = self.apply_series_generator()

        # wrap results
        return self.wrap_results(results, res_index)

    def apply_series_generator(self) -> tuple[ResType, Index]:
        assert callable(self.f)

        series_gen = self.series_generator
        res_index = self.result_index

        results = {}

        with option_context("mode.chained_assignment", None):
            for i, v in enumerate(series_gen):
                # ignore SettingWithCopy here in case the user mutates
                results[i] = self.f(v)
                if isinstance(results[i], ABCSeries):
                    # If we have a view on v, we need to make a copy because
                    #  series_generator will swap out the underlying data
                    results[i] = results[i].copy(deep=False)

        return results, res_index

    def wrap_results(self, results: ResType, res_index: Index) -> DataFrame | Series:
        from pandas import Series

        # see if we can infer the results
        if len(results) > 0 and 0 in results and is_sequence(results[0]):
            return self.wrap_results_for_axis(results, res_index)

        # dict of scalars

        # the default dtype of an empty Series is `object`, but this
        # code can be hit by df.mean() where the result should have dtype
        # float64 even if it's an empty Series.
        constructor_sliced = self.obj._constructor_sliced
        if len(results) == 0 and constructor_sliced is Series:
            result = constructor_sliced(results, dtype=np.float64)
        else:
            result = constructor_sliced(results)
        result.index = res_index

        return result

    def apply_str(self) -> DataFrame | Series:
        # Caller is responsible for checking isinstance(self.f, str)
        # TODO: GH#39993 - Avoid special-casing by replacing with lambda
        if self.f == "size":
            # Special-cased because DataFrame.size returns a single scalar
            obj = self.obj
            value = obj.shape[self.axis]
            return obj._constructor_sliced(value, index=self.agg_axis)
        return super().apply_str()


class FrameRowApply(FrameApply):
    axis: AxisInt = 0

    @property
    def series_generator(self):
        return (self.obj._ixs(i, axis=1) for i in range(len(self.columns)))

    @property
    def result_index(self) -> Index:
        return self.columns

    @property
    def result_columns(self) -> Index:
        return self.index

    def wrap_results_for_axis(
        self, results: ResType, res_index: Index
    ) -> DataFrame | Series:
        """return the results for the rows"""

        if self.result_type == "reduce":
            # e.g. test_apply_dict GH#8735
            res = self.obj._constructor_sliced(results)
            res.index = res_index
            return res

        elif self.result_type is None and all(
            isinstance(x, dict) for x in results.values()
        ):
            # Our operation was a to_dict op e.g.
            #  test_apply_dict GH#8735, test_apply_reduce_to_dict GH#25196 #37544
            res = self.obj._constructor_sliced(results)
            res.index = res_index
            return res

        try:
            result = self.obj._constructor(data=results)
        except ValueError as err:
            if "All arrays must be of the same length" in str(err):
                # e.g. result = [[2, 3], [1.5], ['foo', 'bar']]
                #  see test_agg_listlike_result GH#29587
                res = self.obj._constructor_sliced(results)
                res.index = res_index
                return res
            else:
                raise

        if not isinstance(results[0], ABCSeries) and len(result.index) == len(
            self.res_columns
        ):
            result.index = self.res_columns

        if len(result.columns) == len(res_index):
            result.columns = res_index

        return result


class FrameColumnApply(FrameApply):
    axis: AxisInt = 1

    def apply_broadcast(self, target: DataFrame) -> DataFrame:
        result = super().apply_broadcast(target.T)
        return result.T

    @property
    def series_generator(self):
        values = self.values
        values = ensure_wrapped_if_datetimelike(values)
        assert len(values) > 0

        # We create one Series object, and will swap out the data inside
        #  of it.  Kids: don't do this at home.
        ser = self.obj._ixs(0, axis=0)
        if isinstance(ser.dtype, ExtensionDtype):
            # values will be incorrect for this block
            # TODO(EA2D): special case would be unnecessary with 2D EAs
            obj = self.obj
            for i in range(len(obj)):
                yield obj._ixs(i, axis=0)

        else:
            mgr = ser._mgr

            for arr, name in zip(values, self.index):
                # GH#35462 re-pin mgr in case setitem changed it
                ser._mgr = mgr
                mgr.set_values(arr)
                object.__setattr__(ser, "_name", name)
                yield ser

    @property
    def result_index(self) -> Index:
        return self.index

    @property
    def result_columns(self) -> Index:
        return self.columns

    def wrap_results_for_axis(
        self, results: ResType, res_index: Index
    ) -> DataFrame | Series:
        """return the results for the columns"""
        result: DataFrame | Series

        # we have requested to expand
        if self.result_type == "expand" or isinstance(results[0], ABCSeries):
            result = self.infer_to_same_shape(results, res_index)

        else:
            result = self.obj._constructor_sliced(results)
            result.index = res_index

        return result

    def infer_to_same_shape(self, results: ResType, res_index: Index) -> DataFrame:
        """infer the results to the same shape as the input object"""
        result = self.obj._constructor(data=results)
        result = result.T

        # set the index
        result.index = res_index

        return result.infer_objects(copy=False)


class SeriesApply(NDFrameApply):
    obj: Series
    axis: AxisInt = 0

    def __init__(
        self,
        obj: Series,
        func: AggFuncType,
        convert_dtype: bool,
        args,
        kwargs,
    ) -> None:
        self.convert_dtype = convert_dtype

        super().__init__(
            obj,
            func,
            raw=False,
            result_type=None,
            args=args,
            kwargs=kwargs,
        )

    def apply(self) -> DataFrame | Series:
        obj = self.obj

        if len(obj) == 0:
            return self.apply_empty_result()

        # dispatch to agg
        if is_list_like(self.f):
            return self.apply_multiple()

        return self.apply_str() if isinstance(self.f, str) else self.apply_standard()

    def agg(self):
        result = super().agg()
        if result is None:
            f = self.f
            kwargs = self.kwargs

            # string, list-like, and dict-like are entirely handled in super
            assert callable(f)

            # we can be called from an inner function which
            # passes this meta-data
            kwargs.pop("_level", None)

            # try a regular apply, this evaluates lambdas
            # row-by-row; however if the lambda is expected a Series
            # expression, e.g.: lambda x: x-x.quantile(0.25)
            # this will fail, so we can try a vectorized evaluation

            # we cannot FIRST try the vectorized evaluation, because
            # then .agg and .apply would have different semantics if the
            # operation is actually defined on the Series, e.g. str
            try:
                result = self.obj.apply(f)
            except (ValueError, AttributeError, TypeError):
                result = f(self.obj)

        return result

    def apply_empty_result(self) -> Series:
        obj = self.obj
        return obj._constructor(dtype=obj.dtype, index=obj.index).__finalize__(
            obj, method="apply"
        )

    def apply_standard(self) -> DataFrame | Series:
        # caller is responsible for ensuring that f is Callable
        f = cast(Callable, self.f)
        obj = self.obj

        if isinstance(f, np.ufunc):
            with np.errstate(all="ignore"):
                return f(obj)

        # row-wise access
        # apply doesn't have a `na_action` keyword and for backward compat reasons
        # we need to give `na_action="ignore"` for categorical data.
        # TODO: remove the `na_action="ignore"` when that default has been changed in
        #  Categorical (GH51645).
        action = "ignore" if isinstance(obj.dtype, CategoricalDtype) else None
        mapped = obj._map_values(mapper=f, na_action=action, convert=self.convert_dtype)

        if len(mapped) and isinstance(mapped[0], ABCSeries):
            warnings.warn(
                "Returning a DataFrame from Series.apply when the supplied function"
                "returns a Series is deprecated and will be removed in a future "
                "version.",
                FutureWarning,
                stacklevel=find_stack_level(),
            )  # GH52116

            # GH#43986 Need to do list(mapped) in order to get treated as nested
            #  See also GH#25959 regarding EA support
            return obj._constructor_expanddim(list(mapped), index=obj.index)
        else:
            return obj._constructor(mapped, index=obj.index).__finalize__(
                obj, method="apply"
            )


class GroupByApply(Apply):
    def __init__(
        self,
        obj: GroupBy[NDFrameT],
        func: AggFuncType,
        args,
        kwargs,
    ) -> None:
        kwargs = kwargs.copy()
        self.axis = obj.obj._get_axis_number(kwargs.get("axis", 0))
        super().__init__(
            obj,
            func,
            raw=False,
            result_type=None,
            args=args,
            kwargs=kwargs,
        )

    def apply(self):
        raise NotImplementedError

    def transform(self):
        raise NotImplementedError


class ResamplerWindowApply(Apply):
    axis: AxisInt = 0
    obj: Resampler | BaseWindow

    def __init__(
        self,
        obj: Resampler | BaseWindow,
        func: AggFuncType,
        args,
        kwargs,
    ) -> None:
        super().__init__(
            obj,
            func,
            raw=False,
            result_type=None,
            args=args,
            kwargs=kwargs,
        )

    def apply(self):
        raise NotImplementedError

    def transform(self):
        raise NotImplementedError


def reconstruct_func(
    func: AggFuncType | None, **kwargs
) -> tuple[bool, AggFuncType | None, list[str] | None, npt.NDArray[np.intp] | None]:
    """
    This is the internal function to reconstruct func given if there is relabeling
    or not and also normalize the keyword to get new order of columns.

    If named aggregation is applied, `func` will be None, and kwargs contains the
    column and aggregation function information to be parsed;
    If named aggregation is not applied, `func` is either string (e.g. 'min') or
    Callable, or list of them (e.g. ['min', np.max]), or the dictionary of column name
    and str/Callable/list of them (e.g. {'A': 'min'}, or {'A': [np.min, lambda x: x]})

    If relabeling is True, will return relabeling, reconstructed func, column
    names, and the reconstructed order of columns.
    If relabeling is False, the columns and order will be None.

    Parameters
    ----------
    func: agg function (e.g. 'min' or Callable) or list of agg functions
        (e.g. ['min', np.max]) or dictionary (e.g. {'A': ['min', np.max]}).
    **kwargs: dict, kwargs used in is_multi_agg_with_relabel and
        normalize_keyword_aggregation function for relabelling

    Returns
    -------
    relabelling: bool, if there is relabelling or not
    func: normalized and mangled func
    columns: list of column names
    order: array of columns indices

    Examples
    --------
    >>> reconstruct_func(None, **{"foo": ("col", "min")})
    (True, defaultdict(<class 'list'>, {'col': ['min']}), ('foo',), array([0]))

    >>> reconstruct_func("min")
    (False, 'min', None, None)
    """
    relabeling = func is None and is_multi_agg_with_relabel(**kwargs)
    columns: list[str] | None = None
    order: npt.NDArray[np.intp] | None = None

    if not relabeling:
        if isinstance(func, list) and len(func) > len(set(func)):
            # GH 28426 will raise error if duplicated function names are used and
            # there is no reassigned name
            raise SpecificationError(
                "Function names must be unique if there is no new column names "
                "assigned"
            )
        if func is None:
            # nicer error message
            raise TypeError("Must provide 'func' or tuples of '(column, aggfunc).")

    if relabeling:
        func, columns, order = normalize_keyword_aggregation(kwargs)

    return relabeling, func, columns, order


def is_multi_agg_with_relabel(**kwargs) -> bool:
    """
    Check whether kwargs passed to .agg look like multi-agg with relabeling.

    Parameters
    ----------
    **kwargs : dict

    Returns
    -------
    bool

    Examples
    --------
    >>> is_multi_agg_with_relabel(a="max")
    False
    >>> is_multi_agg_with_relabel(a_max=("a", "max"), a_min=("a", "min"))
    True
    >>> is_multi_agg_with_relabel()
    False
    """
    return all(isinstance(v, tuple) and len(v) == 2 for v in kwargs.values()) and (
        len(kwargs) > 0
    )


def normalize_keyword_aggregation(
    kwargs: dict,
) -> tuple[dict, list[str], npt.NDArray[np.intp]]:
    """
    Normalize user-provided "named aggregation" kwargs.
    Transforms from the new ``Mapping[str, NamedAgg]`` style kwargs
    to the old Dict[str, List[scalar]]].

    Parameters
    ----------
    kwargs : dict

    Returns
    -------
    aggspec : dict
        The transformed kwargs.
    columns : List[str]
        The user-provided keys.
    col_idx_order : List[int]
        List of columns indices.

    Examples
    --------
    >>> normalize_keyword_aggregation({"output": ("input", "sum")})
    (defaultdict(<class 'list'>, {'input': ['sum']}), ('output',), array([0]))
    """
    from pandas.core.indexes.base import Index

    # Normalize the aggregation functions as Mapping[column, List[func]],
    # process normally, then fixup the names.
    # TODO: aggspec type: typing.Dict[str, List[AggScalar]]
    # May be hitting https://github.com/python/mypy/issues/5958
    # saying it doesn't have an attribute __name__
    aggspec: DefaultDict = defaultdict(list)
    order = []
    columns, pairs = list(zip(*kwargs.items()))

    for column, aggfunc in pairs:
        aggspec[column].append(aggfunc)
        order.append((column, com.get_callable_name(aggfunc) or aggfunc))

    # uniquify aggfunc name if duplicated in order list
    uniquified_order = _make_unique_kwarg_list(order)

    # GH 25719, due to aggspec will change the order of assigned columns in aggregation
    # uniquified_aggspec will store uniquified order list and will compare it with order
    # based on index
    aggspec_order = [
        (column, com.get_callable_name(aggfunc) or aggfunc)
        for column, aggfuncs in aggspec.items()
        for aggfunc in aggfuncs
    ]
    uniquified_aggspec = _make_unique_kwarg_list(aggspec_order)

    # get the new index of columns by comparison
    col_idx_order = Index(uniquified_aggspec).get_indexer(uniquified_order)
    return aggspec, columns, col_idx_order


def _make_unique_kwarg_list(
    seq: Sequence[tuple[Any, Any]]
) -> Sequence[tuple[Any, Any]]:
    """
    Uniquify aggfunc name of the pairs in the order list

    Examples:
    --------
    >>> kwarg_list = [('a', '<lambda>'), ('a', '<lambda>'), ('b', '<lambda>')]
    >>> _make_unique_kwarg_list(kwarg_list)
    [('a', '<lambda>_0'), ('a', '<lambda>_1'), ('b', '<lambda>')]
    """
    return [
        (pair[0], f"{pair[1]}_{seq[:i].count(pair)}") if seq.count(pair) > 1 else pair
        for i, pair in enumerate(seq)
    ]


def relabel_result(
    result: DataFrame | Series,
    func: dict[str, list[Callable | str]],
    columns: Iterable[Hashable],
    order: Iterable[int],
) -> dict[Hashable, Series]:
    """
    Internal function to reorder result if relabelling is True for
    dataframe.agg, and return the reordered result in dict.

    Parameters:
    ----------
    result: Result from aggregation
    func: Dict of (column name, funcs)
    columns: New columns name for relabelling
    order: New order for relabelling

    Examples
    --------
    >>> from pandas.core.apply import relabel_result
    >>> result = pd.DataFrame(
    ...     {"A": [np.nan, 2, np.nan], "C": [6, np.nan, np.nan], "B": [np.nan, 4, 2.5]},
    ...     index=["max", "mean", "min"]
    ... )
    >>> funcs = {"A": ["max"], "C": ["max"], "B": ["mean", "min"]}
    >>> columns = ("foo", "aab", "bar", "dat")
    >>> order = [0, 1, 2, 3]
    >>> result_in_dict = relabel_result(result, funcs, columns, order)
    >>> pd.DataFrame(result_in_dict, index=columns)
           A    C    B
    foo  2.0  NaN  NaN
    aab  NaN  6.0  NaN
    bar  NaN  NaN  4.0
    dat  NaN  NaN  2.5
    """
    from pandas.core.indexes.base import Index

    reordered_indexes = [
        pair[0] for pair in sorted(zip(columns, order), key=lambda t: t[1])
    ]
    reordered_result_in_dict: dict[Hashable, Series] = {}
    idx = 0

    reorder_mask = not isinstance(result, ABCSeries) and len(result.columns) > 1
    for col, fun in func.items():
        s = result[col].dropna()

        # In the `_aggregate`, the callable names are obtained and used in `result`, and
        # these names are ordered alphabetically. e.g.
        #           C2   C1
        # <lambda>   1  NaN
        # amax     NaN  4.0
        # max      NaN  4.0
        # sum     18.0  6.0
        # Therefore, the order of functions for each column could be shuffled
        # accordingly so need to get the callable name if it is not parsed names, and
        # reorder the aggregated result for each column.
        # e.g. if df.agg(c1=("C2", sum), c2=("C2", lambda x: min(x))), correct order is
        # [sum, <lambda>], but in `result`, it will be [<lambda>, sum], and we need to
        # reorder so that aggregated values map to their functions regarding the order.

        # However there is only one column being used for aggregation, not need to
        # reorder since the index is not sorted, and keep as is in `funcs`, e.g.
        #         A
        # min   1.0
        # mean  1.5
        # mean  1.5
        if reorder_mask:
            fun = [f if isinstance(f, str) else com.get_callable_name(f) for f in fun]
            col_idx_order = Index(s.index).get_indexer(fun)
            s = s[col_idx_order]

        # assign the new user-provided "named aggregation" as index names, and reindex
        # it based on the whole user-provided names.
        s.index = reordered_indexes[idx : idx + len(fun)]
        reordered_result_in_dict[col] = s.reindex(columns, copy=False)
        idx = idx + len(fun)
    return reordered_result_in_dict


# TODO: Can't use, because mypy doesn't like us setting __name__
#   error: "partial[Any]" has no attribute "__name__"
# the type is:
#   typing.Sequence[Callable[..., ScalarResult]]
#     -> typing.Sequence[Callable[..., ScalarResult]]:


def _managle_lambda_list(aggfuncs: Sequence[Any]) -> Sequence[Any]:
    """
    Possibly mangle a list of aggfuncs.

    Parameters
    ----------
    aggfuncs : Sequence

    Returns
    -------
    mangled: list-like
        A new AggSpec sequence, where lambdas have been converted
        to have unique names.

    Notes
    -----
    If just one aggfunc is passed, the name will not be mangled.
    """
    if len(aggfuncs) <= 1:
        # don't mangle for .agg([lambda x: .])
        return aggfuncs
    i = 0
    mangled_aggfuncs = []
    for aggfunc in aggfuncs:
        if com.get_callable_name(aggfunc) == "<lambda>":
            aggfunc = partial(aggfunc)
            aggfunc.__name__ = f"<lambda_{i}>"
            i += 1
        mangled_aggfuncs.append(aggfunc)

    return mangled_aggfuncs


def maybe_mangle_lambdas(agg_spec: Any) -> Any:
    """
    Make new lambdas with unique names.

    Parameters
    ----------
    agg_spec : Any
        An argument to GroupBy.agg.
        Non-dict-like `agg_spec` are pass through as is.
        For dict-like `agg_spec` a new spec is returned
        with name-mangled lambdas.

    Returns
    -------
    mangled : Any
        Same type as the input.

    Examples
    --------
    >>> maybe_mangle_lambdas('sum')
    'sum'
    >>> maybe_mangle_lambdas([lambda: 1, lambda: 2])  # doctest: +SKIP
    [<function __main__.<lambda_0>,
     <function pandas...._make_lambda.<locals>.f(*args, **kwargs)>]
    """
    is_dict = is_dict_like(agg_spec)
    if not (is_dict or is_list_like(agg_spec)):
        return agg_spec
    mangled_aggspec = type(agg_spec)()  # dict or OrderedDict

    if is_dict:
        for key, aggfuncs in agg_spec.items():
            if is_list_like(aggfuncs) and not is_dict_like(aggfuncs):
                mangled_aggfuncs = _managle_lambda_list(aggfuncs)
            else:
                mangled_aggfuncs = aggfuncs

            mangled_aggspec[key] = mangled_aggfuncs
    else:
        mangled_aggspec = _managle_lambda_list(agg_spec)

    return mangled_aggspec


def validate_func_kwargs(
    kwargs: dict,
) -> tuple[list[str], list[str | Callable[..., Any]]]:
    """
    Validates types of user-provided "named aggregation" kwargs.
    `TypeError` is raised if aggfunc is not `str` or callable.

    Parameters
    ----------
    kwargs : dict

    Returns
    -------
    columns : List[str]
        List of user-provided keys.
    func : List[Union[str, callable[...,Any]]]
        List of user-provided aggfuncs

    Examples
    --------
    >>> validate_func_kwargs({'one': 'min', 'two': 'max'})
    (['one', 'two'], ['min', 'max'])
    """
    tuple_given_message = "func is expected but received {} in **kwargs."
    columns = list(kwargs)
    func = []
    for col_func in kwargs.values():
        if not (isinstance(col_func, str) or callable(col_func)):
            raise TypeError(tuple_given_message.format(type(col_func).__name__))
        func.append(col_func)
    if not columns:
        no_arg_message = "Must provide 'func' or named aggregation **kwargs."
        raise TypeError(no_arg_message)
    return columns, func
