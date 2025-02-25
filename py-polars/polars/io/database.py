from __future__ import annotations

import re
import sys
from contextlib import suppress
from importlib import import_module
from inspect import Parameter, isclass, signature
from typing import TYPE_CHECKING, Any, Iterable, Literal, Sequence, TypedDict, overload

from polars._utils.deprecation import issue_deprecation_warning
from polars.convert import from_arrow
from polars.datatypes import (
    INTEGER_DTYPES,
    N_INFER_DEFAULT,
    UNSIGNED_INTEGER_DTYPES,
    Decimal,
    Float32,
    Float64,
)
from polars.datatypes.convert import (
    _infer_dtype_from_database_typename,
    _integer_dtype_from_nbits,
    _map_py_type_to_dtype,
)
from polars.exceptions import InvalidOperationError, UnsuitableSQLError

if TYPE_CHECKING:
    from types import TracebackType

    import pyarrow as pa

    if sys.version_info >= (3, 10):
        from typing import TypeAlias
    else:
        from typing_extensions import TypeAlias
    if sys.version_info >= (3, 11):
        from typing import Self
    else:
        from typing_extensions import Self

    from polars import DataFrame
    from polars.datatypes import PolarsDataType
    from polars.type_aliases import ConnectionOrCursor, Cursor, DbReadEngine, SchemaDict

    try:
        from sqlalchemy.sql.expression import Selectable
    except ImportError:
        Selectable: TypeAlias = Any  # type: ignore[no-redef]


class _ArrowDriverProperties_(TypedDict):
    # name of the method that fetches all arrow data; tuple form
    # calls the fetch_all method with the given chunk size (int)
    fetch_all: str | tuple[str, int]
    # name of the method that fetches arrow data in batches
    fetch_batches: str | None
    # indicate whether the given batch size is respected exactly
    exact_batch_size: bool | None
    # repeat batch calls (if False, the batch call is a generator)
    repeat_batch_calls: bool


_ARROW_DRIVER_REGISTRY_: dict[str, _ArrowDriverProperties_] = {
    "adbc_.*": {
        "fetch_all": "fetch_arrow_table",
        "fetch_batches": None,
        "exact_batch_size": None,
        "repeat_batch_calls": False,
    },
    "arrow_odbc_proxy": {
        "fetch_all": "fetch_arrow_table",
        "fetch_batches": "fetch_record_batches",
        "exact_batch_size": True,
        "repeat_batch_calls": False,
    },
    "databricks": {
        "fetch_all": "fetchall_arrow",
        "fetch_batches": "fetchmany_arrow",
        "exact_batch_size": True,
        "repeat_batch_calls": True,
    },
    "duckdb": {
        "fetch_all": "fetch_arrow_table",
        "fetch_batches": "fetch_record_batch",
        "exact_batch_size": True,
        "repeat_batch_calls": False,
    },
    "kuzu": {
        # 'get_as_arrow' currently takes a mandatory chunk size
        "fetch_all": ("get_as_arrow", 10_000),
        "fetch_batches": None,
        "exact_batch_size": None,
        "repeat_batch_calls": False,
    },
    "snowflake": {
        "fetch_all": "fetch_arrow_all",
        "fetch_batches": "fetch_arrow_batches",
        "exact_batch_size": False,
        "repeat_batch_calls": False,
    },
    "turbodbc": {
        "fetch_all": "fetchallarrow",
        "fetch_batches": "fetcharrowbatches",
        "exact_batch_size": False,
        "repeat_batch_calls": False,
    },
}

_INVALID_QUERY_TYPES = {
    "ALTER",
    "ANALYZE",
    "CREATE",
    "DELETE",
    "DROP",
    "INSERT",
    "REPLACE",
    "UPDATE",
    "UPSERT",
    "USE",
    "VACUUM",
}


class ODBCCursorProxy:
    """Cursor proxy for ODBC connections (requires `arrow-odbc`)."""

    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string
        self.execute_options: dict[str, Any] = {}
        self.query: str | None = None

    def close(self) -> None:
        """Close the cursor (n/a: nothing to close)."""

    def execute(self, query: str, **execute_options: Any) -> None:
        """Execute a query (n/a: just store query for the fetch* methods)."""
        self.execute_options = execute_options
        self.query = query

    def fetch_arrow_table(
        self, batch_size: int = 10_000, *, fetch_all: bool = False
    ) -> pa.Table:
        """Fetch all results as a pyarrow Table."""
        from pyarrow import Table

        return Table.from_batches(
            self.fetch_record_batches(batch_size=batch_size, fetch_all=True)
        )

    def fetch_record_batches(
        self, batch_size: int = 10_000, *, fetch_all: bool = False
    ) -> Iterable[pa.RecordBatch]:
        """Fetch results as an iterable of RecordBatches."""
        from arrow_odbc import read_arrow_batches_from_odbc
        from pyarrow import RecordBatch

        n_batches = 0
        batch_reader = read_arrow_batches_from_odbc(
            query=self.query,
            batch_size=batch_size,
            connection_string=self.connection_string,
            **self.execute_options,
        )
        for batch in batch_reader:
            yield batch
            n_batches += 1

        if n_batches == 0 and fetch_all:
            # empty result set; return empty batch with accurate schema
            yield RecordBatch.from_pylist([], schema=batch_reader.schema)

    # note: internally arrow-odbc always reads batches
    fetchall = fetch_arrow_table
    fetchmany = fetch_record_batches


class ConnectionExecutor:
    """Abstraction for querying databases with user-supplied connection objects."""

    # indicate if we can/should close the cursor on scope exit. note that we
    # should never close the underlying connection, or a user-supplied cursor.
    can_close_cursor: bool = False

    def __init__(self, connection: ConnectionOrCursor) -> None:
        self.driver_name = (
            "arrow_odbc_proxy"
            if isinstance(connection, ODBCCursorProxy)
            else type(connection).__module__.split(".", 1)[0].lower()
        )
        self.cursor = self._normalise_cursor(connection)
        self.result: Any = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # if we created it and are finished with it, we can
        # close the cursor (but NOT the connection)
        if self.can_close_cursor and hasattr(self.cursor, "close"):
            self.cursor.close()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} module={self.driver_name!r}>"

    def _fetch_arrow(
        self,
        driver_properties: _ArrowDriverProperties_,
        *,
        batch_size: int | None,
        iter_batches: bool,
    ) -> Iterable[pa.RecordBatch]:
        """Yield Arrow data as a generator of one or more RecordBatches or Tables."""
        fetch_batches = driver_properties["fetch_batches"]
        if not iter_batches or fetch_batches is None:
            fetch_method, sz = driver_properties["fetch_all"], []
            if isinstance(fetch_method, tuple):
                fetch_method, chunk_size = fetch_method
                sz = [chunk_size]
            yield getattr(self.result, fetch_method)(*sz)
        else:
            size = batch_size if driver_properties["exact_batch_size"] else None
            repeat_batch_calls = driver_properties["repeat_batch_calls"]
            fetchmany_arrow = getattr(self.result, fetch_batches)
            if not repeat_batch_calls:
                yield from fetchmany_arrow(size)
            else:
                while True:
                    arrow = fetchmany_arrow(size)
                    if not arrow:
                        break
                    yield arrow

    @staticmethod
    def _fetchall_rows(result: Cursor) -> Iterable[Sequence[Any]]:
        """Fetch row data in a single call, returning the complete result set."""
        rows = result.fetchall()
        return (
            [tuple(row) for row in rows]
            if rows and not isinstance(rows[0], (list, tuple))
            else rows
        )

    def _fetchmany_rows(
        self, result: Cursor, batch_size: int | None
    ) -> Iterable[Sequence[Any]]:
        """Fetch row data incrementally, yielding over the complete result set."""
        while True:
            rows = result.fetchmany(batch_size)
            if not rows:
                break
            elif isinstance(rows[0], (list, tuple)):
                yield rows
            else:
                yield [tuple(row) for row in rows]

    def _from_arrow(
        self,
        *,
        batch_size: int | None,
        iter_batches: bool,
        schema_overrides: SchemaDict | None,
        infer_schema_length: int | None,
    ) -> DataFrame | Iterable[DataFrame] | None:
        """Return resultset data in Arrow format for frame init."""
        from polars import from_arrow

        try:
            for driver, driver_properties in _ARROW_DRIVER_REGISTRY_.items():
                if re.match(f"^{driver}$", self.driver_name):
                    fetch_batches = driver_properties["fetch_batches"]
                    self.can_close_cursor = fetch_batches is None or not iter_batches
                    frames = (
                        from_arrow(batch, schema_overrides=schema_overrides)
                        for batch in self._fetch_arrow(
                            driver_properties,
                            iter_batches=iter_batches,
                            batch_size=batch_size,
                        )
                    )
                    return frames if iter_batches else next(frames)  # type: ignore[arg-type,return-value]
        except Exception as err:
            # eg: valid turbodbc/snowflake connection, but no arrow support
            # compiled in to the underlying driver (or on this connection)
            arrow_not_supported = (
                "does not support Apache Arrow",
                "Apache Arrow format is not supported",
            )
            if not any(e in str(err) for e in arrow_not_supported):
                raise

        return None

    def _from_rows(
        self,
        *,
        batch_size: int | None,
        iter_batches: bool,
        schema_overrides: SchemaDict | None,
        infer_schema_length: int | None,
    ) -> DataFrame | Iterable[DataFrame] | None:
        """Return resultset data row-wise for frame init."""
        from polars import DataFrame

        if hasattr(self.result, "fetchall"):
            if self.driver_name == "sqlalchemy":
                if hasattr(self.result, "cursor"):
                    cursor_desc = {d[0]: d[1:] for d in self.result.cursor.description}
                elif hasattr(self.result, "_metadata"):
                    cursor_desc = {k: None for k in self.result._metadata.keys}
                else:
                    msg = f"Unable to determine metadata from query result; {self.result!r}"
                    raise ValueError(msg)
            else:
                cursor_desc = {d[0]: d[1:] for d in self.result.description}

            schema_overrides = self._inject_type_overrides(
                description=cursor_desc,
                schema_overrides=(schema_overrides or {}),
            )
            result_columns = list(cursor_desc)
            frames = (
                DataFrame(
                    data=rows,
                    schema=result_columns,
                    schema_overrides=schema_overrides,
                    infer_schema_length=infer_schema_length,
                    orient="row",
                )
                for rows in (
                    list(self._fetchmany_rows(self.result, batch_size))
                    if iter_batches
                    else [self._fetchall_rows(self.result)]  # type: ignore[list-item]
                )
            )
            return frames if iter_batches else next(frames)  # type: ignore[arg-type]
        return None

    def _inject_type_overrides(
        self,
        description: dict[str, Any],
        schema_overrides: SchemaDict,
    ) -> SchemaDict:
        """Attempt basic dtype inference from a cursor description."""
        # note: this is limited; the `type_code` property may contain almost anything,
        # from strings or python types to driver-specific codes, classes, enums, etc.
        # currently we only do additional inference from string/python type values.
        # (further refinement requires per-driver module knowledge and lookups).

        dtype: PolarsDataType | None = None
        for nm, desc in description.items():
            if desc is None:
                continue
            elif nm not in schema_overrides:
                type_code, _disp_size, internal_size, prec, scale, _null_ok = desc
                if isclass(type_code):
                    # python types, eg: int, float, str, etc
                    with suppress(TypeError):
                        dtype = _map_py_type_to_dtype(type_code)  # type: ignore[arg-type]

                elif isinstance(type_code, str):
                    # database/sql type names, eg: "VARCHAR", "NUMERIC", "BLOB", etc
                    dtype = _infer_dtype_from_database_typename(
                        value=type_code,
                        raise_unmatched=False,
                    )

                if dtype is not None:
                    # check additional cursor information to improve dtype inference
                    if dtype == Float64 and internal_size == 4:
                        dtype = Float32

                    elif dtype in INTEGER_DTYPES and internal_size in (2, 4, 8):
                        bits = internal_size * 8
                        dtype = _integer_dtype_from_nbits(
                            bits,
                            unsigned=(dtype in UNSIGNED_INTEGER_DTYPES),
                            default=dtype,
                        )
                    elif (
                        dtype == Decimal
                        and isinstance(prec, int)
                        and isinstance(scale, int)
                        and prec <= 38
                        and scale <= 38
                    ):
                        dtype = Decimal(prec, scale)

                if dtype is not None:
                    schema_overrides[nm] = dtype  # type: ignore[index]

        return schema_overrides

    def _normalise_cursor(self, conn: Any) -> Cursor:
        """Normalise a connection object such that we have the query executor."""
        if self.driver_name == "sqlalchemy":
            self.can_close_cursor = (conn_type := type(conn).__name__) == "Engine"
            if conn_type == "Session":
                return conn
            else:
                # where possible, use the raw connection to access arrow integration
                if conn.engine.driver == "databricks-sql-python":
                    self.driver_name = "databricks"
                    return conn.engine.raw_connection().cursor()
                elif conn.engine.driver == "duckdb_engine":
                    self.driver_name = "duckdb"
                    return conn.engine.raw_connection().driver_connection.c
                elif conn_type == "Engine":
                    return conn.connect()
                else:
                    return conn

        elif hasattr(conn, "cursor"):
            # connection has a dedicated cursor; prefer over direct execute
            cursor = cursor() if callable(cursor := conn.cursor) else cursor
            self.can_close_cursor = True
            return cursor

        elif hasattr(conn, "execute"):
            # can execute directly (given cursor, sqlalchemy connection, etc)
            return conn

        msg = f"Unrecognised connection {conn!r}; unable to find 'execute' method"
        raise TypeError(msg)

    def execute(
        self,
        query: str | Selectable,
        *,
        options: dict[str, Any] | None = None,
        select_queries_only: bool = True,
    ) -> Self:
        """Execute a query and reference the result set."""
        if select_queries_only and isinstance(query, str):
            q = re.search(r"\w{3,}", re.sub(r"/\*(.|[\r\n])*?\*/", "", query))
            if (query_type := "" if not q else q.group(0)) in _INVALID_QUERY_TYPES:
                msg = f"{query_type} statements are not valid 'read' queries"
                raise UnsuitableSQLError(msg)

        options = options or {}
        cursor_execute = self.cursor.execute

        if self.driver_name == "sqlalchemy":
            from sqlalchemy.orm import Session

            param_key = "parameters"
            if (
                isinstance(self.cursor, Session)
                and "parameters" in options
                and "params" not in options
            ):
                options = options.copy()
                options["params"] = options.pop("parameters")
                param_key = "params"

            if isinstance(query, str):
                params = options.get(param_key)
                if isinstance(params, Sequence) and hasattr(
                    self.cursor, "exec_driver_sql"
                ):
                    cursor_execute = self.cursor.exec_driver_sql
                    if isinstance(params, list) and not all(
                        isinstance(p, (dict, tuple)) for p in params
                    ):
                        options[param_key] = tuple(params)
                else:
                    from sqlalchemy.sql import text

                    query = text(query)  # type: ignore[assignment]

        # note: some cursor execute methods (eg: sqlite3) only take positional
        # params, hence the slightly convoluted resolution of the 'options' dict
        try:
            params = signature(cursor_execute).parameters
        except ValueError:
            params = {}

        if not options or any(
            p.kind in (Parameter.KEYWORD_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
            for p in params.values()
        ):
            result = cursor_execute(query, **options)
        else:
            positional_options = (
                options[o] for o in (params or options) if (not options or o in options)
            )
            result = cursor_execute(query, *positional_options)

        # note: some cursors execute in-place
        result = self.cursor if result is None else result
        self.result = result
        return self

    def to_polars(
        self,
        *,
        iter_batches: bool = False,
        batch_size: int | None = None,
        schema_overrides: SchemaDict | None = None,
        infer_schema_length: int | None = N_INFER_DEFAULT,
    ) -> DataFrame | Iterable[DataFrame]:
        """
        Convert the result set to a DataFrame.

        Wherever possible we try to return arrow-native data directly; only
        fall back to initialising with row-level data if no other option.
        """
        if self.result is None:
            msg = "Cannot return a frame before executing a query"
            raise RuntimeError(msg)
        elif iter_batches and not batch_size:
            msg = (
                "Cannot set `iter_batches` without also setting a non-zero `batch_size`"
            )
            raise ValueError(msg)

        for frame_init in (
            self._from_arrow,  # init from arrow-native data (where support exists)
            self._from_rows,  # row-wise fallback (sqlalchemy, dbapi2, pyodbc, etc)
        ):
            frame = frame_init(
                batch_size=batch_size,
                iter_batches=iter_batches,
                schema_overrides=schema_overrides,
                infer_schema_length=infer_schema_length,
            )
            if frame is not None:
                return frame

        msg = (
            f"Currently no support for {self.driver_name!r} connection {self.cursor!r}"
        )
        raise NotImplementedError(msg)


@overload
def read_database(
    query: str | Selectable,
    connection: ConnectionOrCursor | str,
    *,
    iter_batches: Literal[False] = False,
    batch_size: int | None = ...,
    schema_overrides: SchemaDict | None = ...,
    infer_schema_length: int | None = ...,
    execute_options: dict[str, Any] | None = ...,
    **kwargs: Any,
) -> DataFrame: ...


@overload
def read_database(
    query: str | Selectable,
    connection: ConnectionOrCursor | str,
    *,
    iter_batches: Literal[True],
    batch_size: int | None = ...,
    schema_overrides: SchemaDict | None = ...,
    infer_schema_length: int | None = ...,
    execute_options: dict[str, Any] | None = ...,
    **kwargs: Any,
) -> Iterable[DataFrame]: ...


def read_database(  # noqa: D417
    query: str | Selectable,
    connection: ConnectionOrCursor | str,
    *,
    iter_batches: bool = False,
    batch_size: int | None = None,
    schema_overrides: SchemaDict | None = None,
    infer_schema_length: int | None = N_INFER_DEFAULT,
    execute_options: dict[str, Any] | None = None,
    **kwargs: Any,
) -> DataFrame | Iterable[DataFrame]:
    """
    Read the results of a SQL query into a DataFrame, given a connection object.

    Parameters
    ----------
    query
        SQL query to execute (if using a SQLAlchemy connection object this can
        be a suitable "Selectable", otherwise it is expected to be a string).
    connection
        An instantiated connection (or cursor/client object) that the query can be
        executed against. Can also pass a valid ODBC connection string, identified as
        such if it contains the string "Driver=", in which case the `arrow-odbc`
        package will be used to establish the connection and return Arrow-native data
        to Polars.
    iter_batches
        Return an iterator of DataFrames, where each DataFrame represents a batch of
        data returned by the query; this can be useful for processing large resultsets
        in a memory-efficient manner. If supported by the backend, this value is passed
        to the underlying query execution method (note that very low values will
        typically result in poor performance as it will result in many round-trips to
        the database as the data is returned). If the backend does not support changing
        the batch size then a single DataFrame is yielded from the iterator.
    batch_size
        Indicate the size of each batch when `iter_batches` is True (note that you can
        still set this when `iter_batches` is False, in which case the resulting
        DataFrame is constructed internally using batched return before being returned
        to you. Note that some backends may support batched operation but not allow for
        an explicit size; in this case you will still receive batches, but their exact
        size will be determined by the backend (so may not equal the value set here).
    schema_overrides
        A dictionary mapping column names to dtypes, used to override the schema
        inferred from the query cursor or given by the incoming Arrow data (depending
        on driver/backend). This can be useful if the given types can be more precisely
        defined (for example, if you know that a given column can be declared as `u32`
        instead of `i64`).
    infer_schema_length
        The maximum number of rows to scan for schema inference. If set to `None`, the
        full data may be scanned *(this can be slow)*. This parameter only applies if
        the data is read as a sequence of rows and the `schema_overrides` parameter
        is not set for the given column; Arrow-aware drivers also ignore this value.
    execute_options
        These options will be passed through into the underlying query execution method
        as kwargs. In the case of connections made using an ODBC string (which use
        `arrow-odbc`) these options are passed to the `read_arrow_batches_from_odbc`
        method.

    Notes
    -----
    * This function supports a wide range of native database drivers (ranging from local
      databases such as SQLite to large cloud databases such as Snowflake), as well as
      generic libraries such as ADBC, SQLAlchemy and various flavours of ODBC. If the
      backend supports returning Arrow data directly then this facility will be used to
      efficiently instantiate the DataFrame; otherwise, the DataFrame is initialised
      from row-wise data.

    * Support for Arrow Flight SQL data is available via the `adbc-driver-flightsql`
      package; see https://arrow.apache.org/adbc/current/driver/flight_sql.html for
      more details about using this driver (notable databases implementing Flight SQL
      include Dremio and InfluxDB).

    * The `read_database_uri` function can be noticeably faster than `read_database`
      if you are using a SQLAlchemy or DBAPI2 connection, as `connectorx` optimises
      translation of the result set into Arrow format in Rust, whereas these libraries
      will return row-wise data to Python *before* we can load into Arrow. Note that
      you can determine the connection's URI from a SQLAlchemy engine object by calling
      `conn.engine.url.render_as_string(hide_password=False)`.

    * If polars has to create a cursor from your connection in order to execute the
      query then that cursor will be automatically closed when the query completes;
      however, polars will *never* close any other open connection or cursor.

    * We are able to support more than just relational databases and SQL queries
      through this function. For example, we can load graph database results from
      a `KùzuDB` connection in conjunction with a Cypher query.

    See Also
    --------
    read_database_uri : Create a DataFrame from a SQL query using a URI string.

    Examples
    --------
    Instantiate a DataFrame from a SQL query against a user-supplied connection:

    >>> df = pl.read_database(
    ...     query="SELECT * FROM test_data",
    ...     connection=user_conn,
    ...     schema_overrides={"normalised_score": pl.UInt8},
    ... )  # doctest: +SKIP

    Use a parameterised SQLAlchemy query, passing named values via `execute_options`:

    >>> df = pl.read_database(
    ...     query="SELECT * FROM test_data WHERE metric > :value",
    ...     connection=alchemy_conn,
    ...     execute_options={"parameters": {"value": 0}},
    ... )  # doctest: +SKIP

    Use 'qmark' style parameterisation; values are still passed via `execute_options`,
    but in this case the "parameters" value is a sequence of literals, not a dict:

    >>> df = pl.read_database(
    ...     query="SELECT * FROM test_data WHERE metric > ?",
    ...     connection=alchemy_conn,
    ...     execute_options={"parameters": [0]},
    ... )  # doctest: +SKIP

    Instantiate a DataFrame using an ODBC connection string (requires `arrow-odbc`)
    setting upper limits on the buffer size of variadic text/binary columns, returning
    the result as an iterator over DataFrames containing batches of 1000 rows:

    >>> for df in pl.read_database(
    ...     query="SELECT * FROM test_data",
    ...     connection="Driver={PostgreSQL};Server=localhost;Port=5432;Database=test;Uid=usr;Pwd=",
    ...     execute_options={"max_text_size": 512, "max_binary_size": 1024},
    ...     iter_batches=True,
    ...     batch_size=1000,
    ... ):
    ...     do_something(df)  # doctest: +SKIP

    Load graph data query results from a `KùzuDB` connection and a Cypher query:

    >>> df = pl.read_database(
    ...     query="MATCH (a:User)-[f:Follows]->(b:User) RETURN a.name, f.since, b.name",
    ...     connection=kuzu_db_conn,
    ... )  # doctest: +SKIP

    """  # noqa: W505
    if isinstance(connection, str):
        # check for odbc connection string
        if re.search(r"\bdriver\s*=\s*{[^}]+?}", connection, re.IGNORECASE):
            try:
                import arrow_odbc  # noqa: F401
            except ModuleNotFoundError:
                msg = (
                    "use of an ODBC connection string requires the `arrow-odbc` package"
                    "\n\nPlease run: pip install arrow-odbc"
                )
                raise ModuleNotFoundError(msg) from None

            connection = ODBCCursorProxy(connection)
        else:
            # otherwise looks like a call to read_database_uri
            issue_deprecation_warning(
                message="Use of a string URI with 'read_database' is deprecated; use `read_database_uri` instead",
                version="0.19.0",
            )
            if iter_batches or batch_size:
                msg = "Batch parameters are not supported for `read_database_uri`"
                raise InvalidOperationError(msg)
            if not isinstance(query, (list, str)):
                msg = f"`read_database_uri` expects one or more string queries; found {type(query)}"
                raise TypeError(msg)
            return read_database_uri(
                query,
                uri=connection,
                schema_overrides=schema_overrides,
                **kwargs,
            )

    # note: can remove this check (and **kwargs) once we drop the
    # pass-through deprecation support for read_database_uri
    if kwargs:
        msg = f"`read_database` **kwargs only exist for passthrough to `read_database_uri`: found {kwargs!r}"
        raise ValueError(msg)

    # return frame from arbitrary connections using the executor abstraction
    with ConnectionExecutor(connection) as cx:
        return cx.execute(
            query=query,
            options=execute_options,
        ).to_polars(
            batch_size=batch_size,
            iter_batches=iter_batches,
            schema_overrides=schema_overrides,
            infer_schema_length=infer_schema_length,
        )


def read_database_uri(
    query: list[str] | str,
    uri: str,
    *,
    partition_on: str | None = None,
    partition_range: tuple[int, int] | None = None,
    partition_num: int | None = None,
    protocol: str | None = None,
    engine: DbReadEngine | None = None,
    schema_overrides: SchemaDict | None = None,
    execute_options: dict[str, Any] | None = None,
) -> DataFrame:
    """
    Read the results of a SQL query into a DataFrame, given a URI.

    Parameters
    ----------
    query
        Raw SQL query (or queries).
    uri
        A connectorx or ADBC connection URI string that starts with the backend's
        driver name, for example:

        * "postgresql://user:pass@server:port/database"
        * "snowflake://user:pass@account/database/schema?warehouse=warehouse&role=role"

        The caller is responsible for escaping any special characters in the string,
        which will be passed "as-is" to the underlying engine (this is most often
        required when coming across special characters in the password).
    partition_on
        The column on which to partition the result (connectorx).
    partition_range
        The value range of the partition column (connectorx).
    partition_num
        How many partitions to generate (connectorx).
    protocol
        Backend-specific transfer protocol directive (connectorx); see connectorx
        documentation for more details.
    engine : {'connectorx', 'adbc'}
        Selects the engine used for reading the database (defaulting to connectorx):

        * `'connectorx'`
          Supports a range of databases, such as PostgreSQL, Redshift, MySQL, MariaDB,
          Clickhouse, Oracle, BigQuery, SQL Server, and so on. For an up-to-date list
          please see the connectorx docs:

          * https://github.com/sfu-db/connector-x#supported-sources--destinations

        * `'adbc'`
          Currently there is limited support for this engine, with a relatively small
          number of drivers available, most of which are still in development. For
          an up-to-date list of drivers please see the ADBC docs:

          * https://arrow.apache.org/adbc/
    schema_overrides
        A dictionary mapping column names to dtypes, used to override the schema
        given in the data returned by the query.
    execute_options
        These options will be passed to the underlying query execution method as
        kwargs. Note that connectorx does not support this parameter.

    Notes
    -----
    For `connectorx`, ensure that you have `connectorx>=0.3.2`. The documentation
    is available `here <https://sfu-db.github.io/connector-x/intro.html>`_.

    For `adbc` you will need to have installed `pyarrow` and the ADBC driver associated
    with the backend you are connecting to, eg: `adbc-driver-postgresql`.

    If your password contains special characters, you will need to escape them.
    This will usually require the use of a URL-escaping function, for example:

    >>> from urllib.parse import quote, quote_plus
    >>> quote_plus("pass word?")
    'pass+word%3F'
    >>> quote("pass word?")
    'pass%20word%3F'

    See Also
    --------
    read_database : Create a DataFrame from a SQL query using a connection object.

    Examples
    --------
    Create a DataFrame from a SQL query using a single thread:

    >>> uri = "postgresql://username:password@server:port/database"
    >>> query = "SELECT * FROM lineitem"
    >>> pl.read_database_uri(query, uri)  # doctest: +SKIP

    Create a DataFrame in parallel using 10 threads by automatically partitioning
    the provided SQL on the partition column:

    >>> uri = "postgresql://username:password@server:port/database"
    >>> query = "SELECT * FROM lineitem"
    >>> pl.read_database_uri(
    ...     query,
    ...     uri,
    ...     partition_on="partition_col",
    ...     partition_num=10,
    ...     engine="connectorx",
    ... )  # doctest: +SKIP

    Create a DataFrame in parallel using 2 threads by explicitly providing two
    SQL queries:

    >>> uri = "postgresql://username:password@server:port/database"
    >>> queries = [
    ...     "SELECT * FROM lineitem WHERE partition_col <= 10",
    ...     "SELECT * FROM lineitem WHERE partition_col > 10",
    ... ]
    >>> pl.read_database_uri(queries, uri, engine="connectorx")  # doctest: +SKIP

    Read data from Snowflake using the ADBC driver:

    >>> df = pl.read_database_uri(
    ...     "SELECT * FROM test_table",
    ...     "snowflake://user:pass@company-org/testdb/public?warehouse=test&role=myrole",
    ...     engine="adbc",
    ... )  # doctest: +SKIP
    """
    if not isinstance(uri, str):
        msg = f"expected connection to be a URI string; found {type(uri).__name__!r}"
        raise TypeError(msg)
    elif engine is None:
        engine = "connectorx"

    if engine == "connectorx":
        if execute_options:
            msg = "the 'connectorx' engine does not support use of `execute_options`"
            raise ValueError(msg)
        return _read_sql_connectorx(
            query,
            connection_uri=uri,
            partition_on=partition_on,
            partition_range=partition_range,
            partition_num=partition_num,
            protocol=protocol,
            schema_overrides=schema_overrides,
        )
    elif engine == "adbc":
        if not isinstance(query, str):
            msg = "only a single SQL query string is accepted for adbc"
            raise ValueError(msg)
        return _read_sql_adbc(
            query,
            connection_uri=uri,
            schema_overrides=schema_overrides,
            execute_options=execute_options,
        )
    else:
        msg = f"engine must be one of {{'connectorx', 'adbc'}}, got {engine!r}"
        raise ValueError(msg)


def _read_sql_connectorx(
    query: str | list[str],
    connection_uri: str,
    partition_on: str | None = None,
    partition_range: tuple[int, int] | None = None,
    partition_num: int | None = None,
    protocol: str | None = None,
    schema_overrides: SchemaDict | None = None,
) -> DataFrame:
    try:
        import connectorx as cx
    except ModuleNotFoundError:
        msg = "connectorx is not installed" "\n\nPlease run: pip install connectorx"
        raise ModuleNotFoundError(msg) from None

    try:
        tbl = cx.read_sql(
            conn=connection_uri,
            query=query,
            return_type="arrow2",
            partition_on=partition_on,
            partition_range=partition_range,
            partition_num=partition_num,
            protocol=protocol,
        )
    except BaseException as err:
        # basic sanitisation of /user:pass/ credentials exposed in connectorx errs
        errmsg = re.sub("://[^:]+:[^:]+@", "://***:***@", str(err))
        raise type(err)(errmsg) from err

    return from_arrow(tbl, schema_overrides=schema_overrides)  # type: ignore[return-value]


def _read_sql_adbc(
    query: str,
    connection_uri: str,
    schema_overrides: SchemaDict | None,
    execute_options: dict[str, Any] | None = None,
) -> DataFrame:
    with _open_adbc_connection(connection_uri) as conn, conn.cursor() as cursor:
        cursor.execute(query, **(execute_options or {}))
        tbl = cursor.fetch_arrow_table()
    return from_arrow(tbl, schema_overrides=schema_overrides)  # type: ignore[return-value]


def _open_adbc_connection(connection_uri: str) -> Any:
    driver_name = connection_uri.split(":", 1)[0].lower()

    # map uri prefix to module when not 1:1
    module_suffix_map: dict[str, str] = {
        "postgres": "postgresql",
    }
    try:
        module_suffix = module_suffix_map.get(driver_name, driver_name)
        module_name = f"adbc_driver_{module_suffix}.dbapi"
        import_module(module_name)
        adbc_driver = sys.modules[module_name]
    except ImportError:
        msg = (
            f"ADBC {driver_name} driver not detected"
            f"\n\nIf ADBC supports this database, please run: pip install adbc-driver-{driver_name} pyarrow"
        )
        raise ModuleNotFoundError(msg) from None

    # some backends require the driver name to be stripped from the URI
    if driver_name in ("sqlite", "snowflake"):
        connection_uri = re.sub(f"^{driver_name}:/{{,3}}", "", connection_uri)

    return adbc_driver.connect(connection_uri)
