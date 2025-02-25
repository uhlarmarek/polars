from __future__ import annotations

import contextlib
import ctypes
import functools
import re
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal as PyDecimal
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    ForwardRef,
    Optional,
    Union,
    get_args,
    overload,
)

from polars.datatypes import (
    Array,
    Binary,
    Boolean,
    Categorical,
    DataType,
    DataTypeClass,
    Date,
    Datetime,
    Decimal,
    Duration,
    Field,
    Float32,
    Float64,
    Int8,
    Int16,
    Int32,
    Int64,
    List,
    Null,
    Object,
    String,
    Struct,
    Time,
    UInt8,
    UInt16,
    UInt32,
    UInt64,
    Unknown,
)
from polars.dependencies import numpy as np
from polars.dependencies import pyarrow as pa

with contextlib.suppress(ImportError):  # Module not available when building docs
    from polars.polars import dtype_str_repr as _dtype_str_repr


OptionType = type(Optional[type])
if sys.version_info >= (3, 10):
    from types import NoneType, UnionType
else:
    # infer equivalent class
    NoneType = type(None)
    UnionType = type(Union[int, float])

if TYPE_CHECKING:
    from typing import Literal

    from polars.type_aliases import PolarsDataType, PythonDataType, SchemaDict, TimeUnit


PY_STR_TO_DTYPE: SchemaDict = {
    "float": Float64,
    "int": Int64,
    "str": String,
    "bool": Boolean,
    "date": Date,
    "datetime": Datetime("us"),
    "timedelta": Duration("us"),
    "time": Time,
    "list": List,
    "tuple": List,
    "Decimal": Decimal,
    "bytes": Binary,
    "object": Object,
    "NoneType": Null,
}


@functools.lru_cache(16)
def _map_py_type_to_dtype(
    python_dtype: PythonDataType | type[object],
) -> PolarsDataType:
    """Convert Python data type to Polars data type."""
    if python_dtype is float:
        return Float64
    if python_dtype is int:
        return Int64
    if python_dtype is str:
        return String
    if python_dtype is bool:
        return Boolean
    if issubclass(python_dtype, datetime):
        # `datetime` is a subclass of `date`,
        # so need to check `datetime` first
        return Datetime("us")
    if issubclass(python_dtype, date):
        return Date
    if python_dtype is timedelta:
        return Duration("us")
    if python_dtype is time:
        return Time
    if python_dtype is list:
        return List
    if python_dtype is tuple:
        return List
    if python_dtype is PyDecimal:
        return Decimal
    if python_dtype is bytes:
        return Binary
    if python_dtype is object:
        return Object
    if python_dtype is None.__class__:
        return Null

    # cover generic typing aliases, such as 'list[str]'
    if hasattr(python_dtype, "__origin__") and hasattr(python_dtype, "__args__"):
        base_type = python_dtype.__origin__
        if base_type is not None:
            dtype = _map_py_type_to_dtype(base_type)
            nested = python_dtype.__args__
            if len(nested) == 1:
                nested = nested[0]
            return (
                dtype if nested is None else dtype(_map_py_type_to_dtype(nested))  # type: ignore[operator]
            )

    msg = f"unrecognised Python type: {python_dtype!r}"
    raise TypeError(msg)


def _timeunit_from_precision(precision: int | str | None) -> str | None:
    """Return `time_unit` from integer precision value."""
    from math import ceil

    if not precision:
        return None
    elif isinstance(precision, str):
        if precision.isdigit():
            precision = int(precision)
        elif (precision := precision.lower()) in ("s", "ms", "us", "ns"):
            return "ms" if precision == "s" else precision
    try:
        n = min(max(3, int(ceil(precision / 3)) * 3), 9)  # type: ignore[operator]
        return {3: "ms", 6: "us", 9: "ns"}.get(n)
    except TypeError:
        return None


def _infer_dtype_from_database_typename(
    value: str,
    *,
    raise_unmatched: bool = True,
) -> PolarsDataType | None:
    """Attempt to infer Polars dtype from database cursor `type_code` string value."""
    dtype: PolarsDataType | None = None

    # normalise string name/case (eg: 'IntegerType' -> 'INTEGER')
    original_value = value
    value = value.upper().replace("TYPE", "")

    # extract optional type modifier (eg: 'VARCHAR(64)' -> '64')
    if re.search(r"\([\w,: ]+\)$", value):
        modifier = value[value.find("(") + 1 : -1]
        value = value.split("(")[0]
    elif (
        not value.startswith(("<", ">")) and re.search(r"\[[\w,\]\[: ]+]$", value)
    ) or value.endswith(("[S]", "[MS]", "[US]", "[NS]")):
        modifier = value[value.find("[") + 1 : -1]
        value = value.split("[")[0]
    else:
        modifier = ""

    # array dtypes
    array_aliases = ("ARRAY", "LIST", "[]")
    if value.endswith(array_aliases) or value.startswith(array_aliases):
        for a in array_aliases:
            value = value.replace(a, "", 1) if value else ""

        nested: PolarsDataType | None = None
        if not value and modifier:
            nested = _infer_dtype_from_database_typename(
                value=modifier,
                raise_unmatched=False,
            )
        else:
            if inner_value := _infer_dtype_from_database_typename(
                value[1:-1]
                if (value[0], value[-1]) == ("<", ">")
                else re.sub(r"\W", "", re.sub(r"\WOF\W", "", value)),
                raise_unmatched=False,
            ):
                nested = inner_value
            elif modifier:
                nested = _infer_dtype_from_database_typename(
                    value=modifier,
                    raise_unmatched=False,
                )
        if nested:
            dtype = List(nested)

    # float dtypes
    elif value.startswith("FLOAT") or ("DOUBLE" in value) or (value == "REAL"):
        dtype = (
            Float32
            if value == "FLOAT4"
            or (value.endswith(("16", "32")) or (modifier in ("16", "32")))
            else Float64
        )

    # integer dtypes
    elif ("INTERVAL" not in value) and (
        value.startswith(("INT", "UINT", "UNSIGNED"))
        or value.endswith(("INT", "SERIAL"))
        or ("INTEGER" in value)
        or value == "ROWID"
    ):
        sz: Any
        if "LARGE" in value or value.startswith("BIG") or value == "INT8":
            sz = 64
        elif "MEDIUM" in value or value in ("INT4", "SERIAL"):
            sz = 32
        elif "SMALL" in value or value == "INT2":
            sz = 16
        elif "TINY" in value:
            sz = 8
        else:
            sz = None

        sz = modifier if (not sz and modifier) else sz
        if not isinstance(sz, int):
            sz = int(sz) if isinstance(sz, str) and sz.isdigit() else None
        if (
            ("U" in value and "MEDIUM" not in value)
            or ("UNSIGNED" in value)
            or value == "ROWID"
        ):
            dtype = _integer_dtype_from_nbits(sz, unsigned=True, default=UInt64)
        else:
            dtype = _integer_dtype_from_nbits(sz, unsigned=False, default=Int64)

    # decimal dtypes
    elif (is_dec := ("DECIMAL" in value)) or ("NUMERIC" in value):
        if "," in modifier:
            prec, scale = modifier.split(",")
            dtype = Decimal(int(prec), int(scale))
        else:
            dtype = Decimal if is_dec else Float64

    # string dtypes
    elif (
        any(tp in value for tp in ("VARCHAR", "STRING", "TEXT", "UNICODE"))
        or value.startswith(("STR", "CHAR", "NCHAR", "UTF"))
        or value.endswith(("_UTF8", "_UTF16", "_UTF32"))
    ):
        dtype = String

    # binary dtypes
    elif value in ("BYTEA", "BYTES", "BLOB", "CLOB", "BINARY"):
        dtype = Binary

    # boolean dtypes
    elif value.startswith("BOOL"):
        dtype = Boolean

    # temporal dtypes
    elif value.startswith(("DATETIME", "TIMESTAMP")) and not (value.endswith("[D]")):
        if any((tz in value.replace(" ", "")) for tz in ("TZ", "TIMEZONE")):
            if "WITHOUT" not in value:
                return None  # there's a timezone, but we don't know what it is
        unit = _timeunit_from_precision(modifier) if modifier else "us"
        dtype = Datetime(time_unit=(unit or "us"))  # type: ignore[arg-type]

    elif re.sub(r"\d", "", value) in ("INTERVAL", "TIMEDELTA"):
        dtype = Duration

    elif value in ("DATE", "DATE32", "DATE64"):
        dtype = Date

    elif value in ("TIME", "TIME32", "TIME64"):
        dtype = Time

    if not dtype and raise_unmatched:
        msg = f"cannot infer dtype from {original_value!r} string value"
        raise ValueError(msg)

    return dtype


@functools.lru_cache(8)
def _integer_dtype_from_nbits(
    bits: int,
    *,
    unsigned: bool,
    default: PolarsDataType | None = None,
) -> PolarsDataType | None:
    dtype = {
        (8, False): Int8,
        (8, True): UInt8,
        (16, False): Int16,
        (16, True): UInt16,
        (32, False): Int32,
        (32, True): UInt32,
        (64, False): Int64,
        (64, True): UInt64,
    }.get((bits, unsigned), None)

    if dtype is None and default is not None:
        return default
    return dtype


def is_polars_dtype(dtype: Any, *, include_unknown: bool = False) -> bool:
    """Indicate whether the given input is a Polars dtype, or dtype specialization."""
    try:
        if dtype == Unknown:
            # does not represent a realizable dtype, so ignore by default
            return include_unknown
        else:
            return isinstance(dtype, (DataType, DataTypeClass))
    except TypeError:
        return False


def unpack_dtypes(
    *dtypes: PolarsDataType | None,
    include_compound: bool = False,
) -> set[PolarsDataType]:
    """
    Return a set of unique dtypes found in one or more (potentially compound) dtypes.

    Parameters
    ----------
    *dtypes
        One or more Polars dtypes.
    include_compound
        * if True, any parent/compound dtypes (List, Struct) are included in the result.
        * if False, only the child/scalar dtypes are returned from these types.

    Examples
    --------
    >>> from polars.datatypes import unpack_dtypes
    >>> list_dtype = [pl.List(pl.Float64)]
    >>> struct_dtype = pl.Struct(
    ...     [
    ...         pl.Field("a", pl.Int64),
    ...         pl.Field("b", pl.String),
    ...         pl.Field("c", pl.List(pl.Float64)),
    ...     ]
    ... )
    >>> unpack_dtypes([struct_dtype, list_dtype])  # doctest: +IGNORE_RESULT
    {Float64, Int64, String}
    >>> unpack_dtypes(
    ...     [struct_dtype, list_dtype], include_compound=True
    ... )  # doctest: +IGNORE_RESULT
    {Float64, Int64, String, List(Float64), Struct([Field('a', Int64), Field('b', String), Field('c', List(Float64))])}
    """  # noqa: W505
    if not dtypes:
        return set()
    elif len(dtypes) == 1 and isinstance(dtypes[0], Collection):
        dtypes = dtypes[0]

    unpacked: set[PolarsDataType] = set()
    for tp in dtypes:
        if isinstance(tp, (List, Array)):
            if include_compound:
                unpacked.add(tp)
            unpacked.update(unpack_dtypes(tp.inner, include_compound=include_compound))
        elif isinstance(tp, Struct):
            if include_compound:
                unpacked.add(tp)
            unpacked.update(unpack_dtypes(tp.fields, include_compound=include_compound))  # type: ignore[arg-type]
        elif isinstance(tp, Field):
            unpacked.update(unpack_dtypes(tp.dtype, include_compound=include_compound))
        elif tp is not None and is_polars_dtype(tp):
            unpacked.add(tp)
    return unpacked


class _DataTypeMappings:
    @property
    @functools.lru_cache  # noqa: B019
    def DTYPE_TO_FFINAME(self) -> dict[PolarsDataType, str]:
        return {
            Int8: "i8",
            Int16: "i16",
            Int32: "i32",
            Int64: "i64",
            UInt8: "u8",
            UInt16: "u16",
            UInt32: "u32",
            UInt64: "u64",
            Float32: "f32",
            Float64: "f64",
            Decimal: "decimal",
            Boolean: "bool",
            String: "str",
            List: "list",
            Date: "date",
            Datetime: "datetime",
            Duration: "duration",
            Time: "time",
            Object: "object",
            Categorical: "categorical",
            Struct: "struct",
            Binary: "binary",
        }

    @property
    @functools.lru_cache  # noqa: B019
    def DTYPE_TO_CTYPE(self) -> dict[PolarsDataType, Any]:
        return {
            UInt8: ctypes.c_uint8,
            UInt16: ctypes.c_uint16,
            UInt32: ctypes.c_uint32,
            UInt64: ctypes.c_uint64,
            Int8: ctypes.c_int8,
            Int16: ctypes.c_int16,
            Int32: ctypes.c_int32,
            Int64: ctypes.c_int64,
            Float32: ctypes.c_float,
            Float64: ctypes.c_double,
            Datetime: ctypes.c_int64,
            Duration: ctypes.c_int64,
            Date: ctypes.c_int32,
            Time: ctypes.c_int64,
        }

    @property
    @functools.lru_cache  # noqa: B019
    def DTYPE_TO_PY_TYPE(self) -> dict[PolarsDataType, PythonDataType]:
        return {
            Float64: float,
            Float32: float,
            Int64: int,
            Int32: int,
            Int16: int,
            Int8: int,
            String: str,
            UInt8: int,
            UInt16: int,
            UInt32: int,
            UInt64: int,
            Decimal: PyDecimal,
            Boolean: bool,
            Duration: timedelta,
            Datetime: datetime,
            Date: date,
            Time: time,
            Binary: bytes,
            List: list,
            Array: list,
            Null: None.__class__,
        }

    @property
    @functools.lru_cache  # noqa: B019
    def NUMPY_KIND_AND_ITEMSIZE_TO_DTYPE(self) -> dict[tuple[str, int], PolarsDataType]:
        return {
            # (np.dtype().kind, np.dtype().itemsize)
            ("b", 1): Boolean,
            ("i", 1): Int8,
            ("i", 2): Int16,
            ("i", 4): Int32,
            ("i", 8): Int64,
            ("u", 1): UInt8,
            ("u", 2): UInt16,
            ("u", 4): UInt32,
            ("u", 8): UInt64,
            ("f", 4): Float32,
            ("f", 8): Float64,
            ("m", 8): Duration,
            ("M", 8): Datetime,
        }

    @property
    @functools.lru_cache  # noqa: B019
    def PY_TYPE_TO_ARROW_TYPE(self) -> dict[PythonDataType, pa.lib.DataType]:
        return {
            float: pa.float64(),
            int: pa.int64(),
            str: pa.large_utf8(),
            bool: pa.bool_(),
            date: pa.date32(),
            time: pa.time64("us"),
            datetime: pa.timestamp("us"),
            timedelta: pa.duration("us"),
            None.__class__: pa.null(),
        }

    @property
    @functools.lru_cache  # noqa: B019
    def REPR_TO_DTYPE(self) -> dict[str, PolarsDataType]:
        def _dtype_str_repr_safe(o: Any) -> PolarsDataType | None:
            try:
                return _dtype_str_repr(o.base_type()).split("[")[0]
            except TypeError:
                return None

        return {
            _dtype_str_repr_safe(obj): obj  # type: ignore[misc]
            for obj in globals().values()
            if is_polars_dtype(obj) and _dtype_str_repr_safe(obj) is not None
        }


# Initialize once (poor man's singleton :)
DataTypeMappings = _DataTypeMappings()


def dtype_to_ctype(dtype: PolarsDataType) -> Any:
    """Convert a Polars dtype to a ctype."""
    try:
        dtype = dtype.base_type()
        return DataTypeMappings.DTYPE_TO_CTYPE[dtype]
    except KeyError:  # pragma: no cover
        msg = f"conversion of polars data type {dtype!r} to C-type not implemented"
        raise NotImplementedError(msg) from None


def dtype_to_ffiname(dtype: PolarsDataType) -> str:
    """Return FFI function name associated with the given Polars dtype."""
    try:
        dtype = dtype.base_type()
        return DataTypeMappings.DTYPE_TO_FFINAME[dtype]
    except KeyError:  # pragma: no cover
        msg = f"conversion of polars data type {dtype!r} to FFI not implemented"
        raise NotImplementedError(msg) from None


def dtype_to_py_type(dtype: PolarsDataType) -> PythonDataType:
    """Convert a Polars dtype to a Python dtype."""
    try:
        dtype = dtype.base_type()
        return DataTypeMappings.DTYPE_TO_PY_TYPE[dtype]
    except KeyError:  # pragma: no cover
        msg = f"conversion of polars data type {dtype!r} to Python type not implemented"
        raise NotImplementedError(msg) from None


@overload
def py_type_to_dtype(
    data_type: Any, *, raise_unmatched: Literal[True] = ...
) -> PolarsDataType: ...


@overload
def py_type_to_dtype(
    data_type: Any, *, raise_unmatched: Literal[False]
) -> PolarsDataType | None: ...


def py_type_to_dtype(
    data_type: Any, *, raise_unmatched: bool = True, allow_strings: bool = False
) -> PolarsDataType | None:
    """Convert a Python dtype (or type annotation) to a Polars dtype."""
    if isinstance(data_type, ForwardRef):
        annotation = data_type.__forward_arg__
        data_type = (
            PY_STR_TO_DTYPE.get(
                re.sub(r"(^None \|)|(\| None$)", "", annotation).strip(), data_type
            )
            if isinstance(annotation, str)  # type: ignore[redundant-expr]
            else annotation
        )
    elif type(data_type).__name__ == "InitVar":
        data_type = data_type.type

    if is_polars_dtype(data_type):
        return data_type

    elif isinstance(data_type, (OptionType, UnionType)):
        # not exhaustive; handles the common "type | None" case, but
        # should probably pick appropriate supertype when n_types > 1?
        possible_types = [tp for tp in get_args(data_type) if tp is not NoneType]
        if len(possible_types) == 1:
            data_type = possible_types[0]

    elif allow_strings and isinstance(data_type, str):
        data_type = DataTypeMappings.REPR_TO_DTYPE.get(
            re.sub(r"^(?:dataclasses\.)?InitVar\[(.+)\]$", r"\1", data_type),
            data_type,
        )
        if is_polars_dtype(data_type):
            return data_type
    try:
        return _map_py_type_to_dtype(data_type)
    except (KeyError, TypeError):  # pragma: no cover
        if raise_unmatched:
            msg = f"cannot infer dtype from {data_type!r} (type: {type(data_type).__name__!r})"
            raise ValueError(msg) from None
        return None


def py_type_to_arrow_type(dtype: PythonDataType) -> pa.lib.DataType:
    """Convert a Python dtype to an Arrow dtype."""
    try:
        return DataTypeMappings.PY_TYPE_TO_ARROW_TYPE[dtype]
    except KeyError:  # pragma: no cover
        msg = f"cannot parse Python data type {dtype!r} into Arrow data type"
        raise ValueError(msg) from None


def dtype_short_repr_to_dtype(dtype_string: str | None) -> PolarsDataType | None:
    """Map a PolarsDataType short repr (eg: 'i64', 'list[str]') back into a dtype."""
    if dtype_string is None:
        return None
    m = re.match(r"^(\w+)(?:\[(.+)\])?$", dtype_string)
    if m is None:
        return None

    dtype_base, subtype = m.groups()
    dtype = DataTypeMappings.REPR_TO_DTYPE.get(dtype_base)
    if dtype and subtype:
        # TODO: further-improve handling for nested types (such as List,Struct)
        try:
            if dtype == Decimal:
                subtype = (None, int(subtype))
            else:
                subtype = (
                    s.strip("'\" ") for s in subtype.replace("μs", "us").split(",")
                )
            return dtype(*subtype)  # type: ignore[operator]
        except ValueError:
            pass
    return dtype


def supported_numpy_char_code(dtype_char: str) -> bool:
    """Check if the input can be mapped to a Polars dtype."""
    dtype = np.dtype(dtype_char)
    return (
        dtype.kind,
        dtype.itemsize,
    ) in DataTypeMappings.NUMPY_KIND_AND_ITEMSIZE_TO_DTYPE


def numpy_char_code_to_dtype(dtype_char: str) -> PolarsDataType:
    """Convert a numpy character dtype to a Polars dtype."""
    dtype = np.dtype(dtype_char)
    if dtype.kind == "U":
        return String
    elif dtype.kind == "S":
        return Binary
    try:
        return DataTypeMappings.NUMPY_KIND_AND_ITEMSIZE_TO_DTYPE[
            (dtype.kind, dtype.itemsize)
        ]
    except KeyError:  # pragma: no cover
        msg = f"cannot parse numpy data type {dtype!r} into Polars data type"
        raise ValueError(msg) from None


def maybe_cast(el: Any, dtype: PolarsDataType) -> Any:
    """Try casting a value to a value that is valid for the given Polars dtype."""
    # cast el if it doesn't match
    from polars._utils.convert import (
        datetime_to_int,
        timedelta_to_int,
    )

    time_unit: TimeUnit
    if isinstance(el, datetime):
        time_unit = getattr(dtype, "time_unit", "us")
        return datetime_to_int(el, time_unit)
    elif isinstance(el, timedelta):
        time_unit = getattr(dtype, "time_unit", "us")
        return timedelta_to_int(el, time_unit)

    py_type = dtype_to_py_type(dtype)
    if not isinstance(el, py_type):
        try:
            el = py_type(el)  # type: ignore[call-arg, misc]
        except Exception:
            msg = f"cannot convert Python type {type(el).__name__!r} to {dtype!r}"
            raise TypeError(msg) from None
    return el
