# sql/crud.py
# Copyright (C) 2005-2022 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php
# mypy: allow-untyped-defs, allow-untyped-calls

"""Functions used by compiler.py to determine the parameters rendered
within INSERT and UPDATE statements.

"""
from __future__ import annotations

import functools
import operator
from typing import Any
from typing import Callable
from typing import cast
from typing import Dict
from typing import List
from typing import MutableMapping
from typing import NamedTuple
from typing import Optional
from typing import overload
from typing import Sequence
from typing import Tuple
from typing import TYPE_CHECKING
from typing import Union

from . import coercions
from . import dml
from . import elements
from . import roles
from .elements import ColumnClause
from .schema import default_is_clause_element
from .schema import default_is_sequence
from .selectable import TableClause
from .. import exc
from .. import util
from ..util.typing import Literal

if TYPE_CHECKING:
    from .compiler import _BindNameForColProtocol
    from .compiler import SQLCompiler
    from .dml import _DMLColumnElement
    from .dml import DMLState
    from .dml import ValuesBase
    from .elements import ColumnElement
    from .schema import _SQLExprDefault
    from .selectable import TableClause

REQUIRED = util.symbol(
    "REQUIRED",
    """
Placeholder for the value within a :class:`.BindParameter`
which is required to be present when the statement is passed
to :meth:`_engine.Connection.execute`.

This symbol is typically used when a :func:`_expression.insert`
or :func:`_expression.update` statement is compiled without parameter
values present.

""",
)


def _as_dml_column(c: ColumnElement[Any]) -> ColumnClause[Any]:
    if not isinstance(c, ColumnClause):
        raise exc.CompileError(
            f"Can't create DML statement against column expression {c!r}"
        )
    return c


class _CrudParams(NamedTuple):
    single_params: Sequence[
        Tuple[ColumnElement[Any], str, Optional[Union[str, _SQLExprDefault]]]
    ]
    all_multi_params: List[
        Sequence[
            Tuple[
                ColumnClause[Any],
                str,
                str,
            ]
        ]
    ]


def _get_crud_params(
    compiler: SQLCompiler,
    stmt: ValuesBase,
    compile_state: DMLState,
    toplevel: bool,
    **kw: Any,
) -> _CrudParams:
    """create a set of tuples representing column/string pairs for use
    in an INSERT or UPDATE statement.

    Also generates the Compiled object's postfetch, prefetch, and
    returning column collections, used for default handling and ultimately
    populating the CursorResult's prefetch_cols() and postfetch_cols()
    collections.

    """

    # note: the _get_crud_params() system was written with the notion in mind
    # that INSERT, UPDATE, DELETE are always the top level statement and
    # that there is only one of them.  With the addition of CTEs that can
    # make use of DML, this assumption is no longer accurate; the DML
    # statement is not necessarily the top-level "row returning" thing
    # and it is also theoretically possible (fortunately nobody has asked yet)
    # to have a single statement with multiple DMLs inside of it via CTEs.

    # the current _get_crud_params() design doesn't accommodate these cases
    # right now.  It "just works" for a CTE that has a single DML inside of
    # it, and for a CTE with multiple DML, it's not clear what would happen.

    # overall, the "compiler.XYZ" collections here would need to be in a
    # per-DML structure of some kind, and DefaultDialect would need to
    # navigate these collections on a per-statement basis, with additional
    # emphasis on the "toplevel returning data" statement.  However we
    # still need to run through _get_crud_params() for all DML as we have
    # Python / SQL generated column defaults that need to be rendered.

    # if there is user need for this kind of thing, it's likely a post 2.0
    # kind of change as it would require deep changes to DefaultDialect
    # as well as here.

    compiler.postfetch = []
    compiler.insert_prefetch = []
    compiler.update_prefetch = []
    compiler.implicit_returning = []

    # getters - these are normally just column.key,
    # but in the case of mysql multi-table update, the rules for
    # .key must conditionally take tablename into account
    (
        _column_as_key,
        _getattr_col_key,
        _col_bind_name,
    ) = _key_getters_for_crud_column(compiler, stmt, compile_state)

    compiler._get_bind_name_for_col = _col_bind_name

    # no parameters in the statement, no parameters in the
    # compiled params - return binds for all columns
    if compiler.column_keys is None and compile_state._no_parameters:
        return _CrudParams(
            [
                (
                    c,
                    compiler.preparer.format_column(c),
                    _create_bind_param(compiler, c, None, required=True),
                )
                for c in stmt.table.columns
            ],
            [],
        )

    stmt_parameter_tuples: Optional[
        List[Tuple[Union[str, ColumnClause[Any]], Any]]
    ]
    spd: Optional[MutableMapping[_DMLColumnElement, Any]]

    if compile_state._has_multi_parameters:
        mp = compile_state._multi_parameters
        assert mp is not None
        spd = mp[0]
        stmt_parameter_tuples = list(spd.items())
    elif compile_state._ordered_values:
        spd = compile_state._dict_parameters
        stmt_parameter_tuples = compile_state._ordered_values
    elif compile_state._dict_parameters:
        spd = compile_state._dict_parameters
        stmt_parameter_tuples = list(spd.items())
    else:
        stmt_parameter_tuples = spd = None

    # if we have statement parameters - set defaults in the
    # compiled params
    if compiler.column_keys is None:
        parameters = {}
    elif stmt_parameter_tuples:
        assert spd is not None
        parameters = dict(
            (_column_as_key(key), REQUIRED)
            for key in compiler.column_keys
            if key not in spd
        )
    else:
        parameters = dict(
            (_column_as_key(key), REQUIRED) for key in compiler.column_keys
        )

    # create a list of column assignment clauses as tuples
    values: List[
        Tuple[ColumnClause[Any], str, Optional[Union[str, _SQLExprDefault]]]
    ] = []

    if stmt_parameter_tuples is not None:
        _get_stmt_parameter_tuples_params(
            compiler,
            compile_state,
            parameters,
            stmt_parameter_tuples,
            _column_as_key,
            values,
            kw,
        )

    check_columns: Dict[str, ColumnClause[Any]] = {}

    # special logic that only occurs for multi-table UPDATE
    # statements
    if dml.isupdate(compile_state) and compile_state.is_multitable:
        _get_update_multitable_params(
            compiler,
            stmt,
            compile_state,
            stmt_parameter_tuples,
            check_columns,
            _col_bind_name,
            _getattr_col_key,
            values,
            kw,
        )

    if compile_state.isinsert and stmt._select_names:
        # is an insert from select, is not a multiparams

        assert not compile_state._has_multi_parameters

        _scan_insert_from_select_cols(
            compiler,
            stmt,
            compile_state,
            parameters,
            _getattr_col_key,
            _column_as_key,
            _col_bind_name,
            check_columns,
            values,
            toplevel,
            kw,
        )
    else:
        _scan_cols(
            compiler,
            stmt,
            compile_state,
            parameters,
            _getattr_col_key,
            _column_as_key,
            _col_bind_name,
            check_columns,
            values,
            toplevel,
            kw,
        )

    if parameters and stmt_parameter_tuples:
        check = (
            set(parameters)
            .intersection(_column_as_key(k) for k, v in stmt_parameter_tuples)
            .difference(check_columns)
        )
        if check:
            raise exc.CompileError(
                "Unconsumed column names: %s"
                % (", ".join("%s" % (c,) for c in check))
            )

    if compile_state._has_multi_parameters:
        # is a multiparams, is not an insert from a select
        assert not stmt._select_names
        multi_extended_values = _extend_values_for_multiparams(
            compiler,
            stmt,
            compile_state,
            cast("Sequence[Tuple[ColumnClause[Any], str, str]]", values),
            cast("Callable[..., str]", _column_as_key),
            kw,
        )
        return _CrudParams(values, multi_extended_values)
    elif (
        not values
        and compiler.for_executemany
        and compiler.dialect.supports_default_metavalue
    ):
        # convert an "INSERT DEFAULT VALUES"
        # into INSERT (firstcol) VALUES (DEFAULT) which can be turned
        # into an in-place multi values.  This supports
        # insert_executemany_returning mode :)
        values = [
            (
                _as_dml_column(stmt.table.columns[0]),
                compiler.preparer.format_column(stmt.table.columns[0]),
                "DEFAULT",
            )
        ]

    return _CrudParams(values, [])


@overload
def _create_bind_param(
    compiler: SQLCompiler,
    col: ColumnElement[Any],
    value: Any,
    process: Literal[True] = ...,
    required: bool = False,
    name: Optional[str] = None,
    **kw: Any,
) -> str:
    ...


@overload
def _create_bind_param(
    compiler: SQLCompiler,
    col: ColumnElement[Any],
    value: Any,
    **kw: Any,
) -> str:
    ...


def _create_bind_param(
    compiler: SQLCompiler,
    col: ColumnElement[Any],
    value: Any,
    process: bool = True,
    required: bool = False,
    name: Optional[str] = None,
    **kw: Any,
) -> Union[str, elements.BindParameter[Any]]:
    if name is None:
        name = col.key
    bindparam = elements.BindParameter(
        name, value, type_=col.type, required=required
    )
    bindparam._is_crud = True
    if process:
        return bindparam._compiler_dispatch(compiler, **kw)
    else:
        return bindparam


def _handle_values_anonymous_param(compiler, col, value, name, **kw):
    # the insert() and update() constructs as of 1.4 will now produce anonymous
    # bindparam() objects in the values() collections up front when given plain
    # literal values.  This is so that cache key behaviors, which need to
    # produce bound parameters in deterministic order without invoking any
    # compilation here, can be applied to these constructs when they include
    # values() (but not yet multi-values, which are not included in caching
    # right now).
    #
    # in order to produce the desired "crud" style name for these parameters,
    # which will also be targetable in engine/default.py through the usual
    # conventions, apply our desired name to these unique parameters by
    # populating the compiler truncated names cache with the desired name,
    # rather than having
    # compiler.visit_bindparam()->compiler._truncated_identifier make up a
    # name.  Saves on call counts also.

    # for INSERT/UPDATE that's a CTE, we don't need names to match to
    # external parameters and these would also conflict in the case where
    # multiple insert/update are combined together using CTEs
    is_cte = "visiting_cte" in kw

    if (
        not is_cte
        and value.unique
        and isinstance(value.key, elements._truncated_label)
    ):
        compiler.truncated_names[("bindparam", value.key)] = name

    if value.type._isnull:
        # either unique parameter, or other bound parameters that were
        # passed in directly
        # set type to that of the column unconditionally
        value = value._with_binary_element_type(col.type)

    return value._compiler_dispatch(compiler, **kw)


def _key_getters_for_crud_column(
    compiler: SQLCompiler, stmt: ValuesBase, compile_state: DMLState
) -> Tuple[
    Callable[[Union[str, ColumnClause[Any]]], Union[str, Tuple[str, str]]],
    Callable[[ColumnClause[Any]], Union[str, Tuple[str, str]]],
    _BindNameForColProtocol,
]:
    if dml.isupdate(compile_state) and compile_state._extra_froms:
        # when extra tables are present, refer to the columns
        # in those extra tables as table-qualified, including in
        # dictionaries and when rendering bind param names.
        # the "main" table of the statement remains unqualified,
        # allowing the most compatibility with a non-multi-table
        # statement.
        _et = set(compile_state._extra_froms)

        c_key_role = functools.partial(
            coercions.expect_as_key, roles.DMLColumnRole
        )

        def _column_as_key(
            key: Union[ColumnClause[Any], str]
        ) -> Union[str, Tuple[str, str]]:
            str_key = c_key_role(key)
            if hasattr(key, "table") and key.table in _et:  # type: ignore
                return (key.table.name, str_key)  # type: ignore
            else:
                return str_key  # type: ignore

        def _getattr_col_key(
            col: ColumnClause[Any],
        ) -> Union[str, Tuple[str, str]]:
            if col.table in _et:
                return (col.table.name, col.key)  # type: ignore
            else:
                return col.key

        def _col_bind_name(col: ColumnClause[Any]) -> str:
            if col.table in _et:
                if TYPE_CHECKING:
                    assert isinstance(col.table, TableClause)
                return "%s_%s" % (col.table.name, col.key)
            else:
                return col.key

    else:
        _column_as_key = functools.partial(  # type: ignore
            coercions.expect_as_key, roles.DMLColumnRole
        )
        _getattr_col_key = _col_bind_name = operator.attrgetter("key")  # type: ignore  # noqa: E501

    return _column_as_key, _getattr_col_key, _col_bind_name


def _scan_insert_from_select_cols(
    compiler,
    stmt,
    compile_state,
    parameters,
    _getattr_col_key,
    _column_as_key,
    _col_bind_name,
    check_columns,
    values,
    toplevel,
    kw,
):

    (
        need_pks,
        implicit_returning,
        implicit_return_defaults,
        postfetch_lastrowid,
    ) = _get_returning_modifiers(compiler, stmt, compile_state, toplevel)

    cols = [stmt.table.c[_column_as_key(name)] for name in stmt._select_names]

    assert compiler.stack[-1]["selectable"] is stmt

    compiler.stack[-1]["insert_from_select"] = stmt.select

    add_select_cols: List[Tuple[ColumnClause[Any], str, _SQLExprDefault]] = []
    if stmt.include_insert_from_select_defaults:
        col_set = set(cols)
        for col in stmt.table.columns:
            if col not in col_set and col.default:
                cols.append(col)

    for c in cols:
        col_key = _getattr_col_key(c)
        if col_key in parameters and col_key not in check_columns:
            parameters.pop(col_key)
            values.append((c, compiler.preparer.format_column(c), None))
        else:
            _append_param_insert_select_hasdefault(
                compiler, stmt, c, add_select_cols, kw
            )

    if add_select_cols:
        values.extend(add_select_cols)
        ins_from_select = compiler.stack[-1]["insert_from_select"]
        ins_from_select = ins_from_select._generate()
        ins_from_select._raw_columns = tuple(
            ins_from_select._raw_columns
        ) + tuple(expr for col, col_expr, expr in add_select_cols)
        compiler.stack[-1]["insert_from_select"] = ins_from_select


def _scan_cols(
    compiler,
    stmt,
    compile_state,
    parameters,
    _getattr_col_key,
    _column_as_key,
    _col_bind_name,
    check_columns,
    values,
    toplevel,
    kw,
):
    (
        need_pks,
        implicit_returning,
        implicit_return_defaults,
        postfetch_lastrowid,
    ) = _get_returning_modifiers(compiler, stmt, compile_state, toplevel)

    if compile_state._parameter_ordering:
        parameter_ordering = [
            _column_as_key(key) for key in compile_state._parameter_ordering
        ]
        ordered_keys = set(parameter_ordering)
        cols = [
            stmt.table.c[key]
            for key in parameter_ordering
            if isinstance(key, str) and key in stmt.table.c
        ] + [c for c in stmt.table.c if c.key not in ordered_keys]

    else:
        cols = stmt.table.columns

    for c in cols:
        # scan through every column in the target table

        col_key = _getattr_col_key(c)

        if col_key in parameters and col_key not in check_columns:
            # parameter is present for the column.  use that.

            _append_param_parameter(
                compiler,
                stmt,
                compile_state,
                c,
                col_key,
                parameters,
                _col_bind_name,
                implicit_returning,
                implicit_return_defaults,
                values,
                kw,
            )

        elif compile_state.isinsert:
            # no parameter is present and it's an insert.

            if c.primary_key and need_pks:
                # it's a primary key column, it will need to be generated by a
                # default generator of some kind, and the statement expects
                # inserted_primary_key to be available.

                if implicit_returning:
                    # we can use RETURNING, find out how to invoke this
                    # column and get the value where RETURNING is an option.
                    # we can inline server-side functions in this case.

                    _append_param_insert_pk_returning(
                        compiler, stmt, c, values, kw
                    )
                else:
                    # otherwise, find out how to invoke this column
                    # and get its value where RETURNING is not an option.
                    # if we have to invoke a server-side function, we need
                    # to pre-execute it.   or if this is a straight
                    # autoincrement column and the dialect supports it
                    # we can use cursor.lastrowid.

                    _append_param_insert_pk_no_returning(
                        compiler, stmt, c, values, kw
                    )

            elif c.default is not None:
                # column has a default, but it's not a pk column, or it is but
                # we don't need to get the pk back.
                _append_param_insert_hasdefault(
                    compiler, stmt, c, implicit_return_defaults, values, kw
                )

            elif c.server_default is not None:
                # column has a DDL-level default, and is either not a pk
                # column or we don't need the pk.
                if implicit_return_defaults and c in implicit_return_defaults:
                    compiler.implicit_returning.append(c)
                elif not c.primary_key:
                    compiler.postfetch.append(c)
            elif implicit_return_defaults and c in implicit_return_defaults:
                compiler.implicit_returning.append(c)
            elif (
                c.primary_key
                and c is not stmt.table._autoincrement_column
                and not c.nullable
            ):
                _warn_pk_with_no_anticipated_value(c)

        elif compile_state.isupdate:
            # no parameter is present and it's an insert.

            _append_param_update(
                compiler,
                compile_state,
                stmt,
                c,
                implicit_return_defaults,
                values,
                kw,
            )


def _append_param_parameter(
    compiler,
    stmt,
    compile_state,
    c,
    col_key,
    parameters,
    _col_bind_name,
    implicit_returning,
    implicit_return_defaults,
    values,
    kw,
):
    value = parameters.pop(col_key)

    col_value = compiler.preparer.format_column(
        c, use_table=compile_state.include_table_with_column_exprs
    )

    if coercions._is_literal(value):
        value = _create_bind_param(
            compiler,
            c,
            value,
            required=value is REQUIRED,
            name=_col_bind_name(c)
            if not compile_state._has_multi_parameters
            else "%s_m0" % _col_bind_name(c),
            **kw,
        )
    elif value._is_bind_parameter:
        value = _handle_values_anonymous_param(
            compiler,
            c,
            value,
            name=_col_bind_name(c)
            if not compile_state._has_multi_parameters
            else "%s_m0" % _col_bind_name(c),
            **kw,
        )
    else:
        # value is a SQL expression
        value = compiler.process(value.self_group(), **kw)

        if compile_state.isupdate:
            if implicit_return_defaults and c in implicit_return_defaults:
                compiler.implicit_returning.append(c)

            else:
                compiler.postfetch.append(c)
        else:
            if c.primary_key:

                if implicit_returning:
                    compiler.implicit_returning.append(c)
                elif compiler.dialect.postfetch_lastrowid:
                    compiler.postfetch_lastrowid = True

            elif implicit_return_defaults and c in implicit_return_defaults:
                compiler.implicit_returning.append(c)

            else:
                # postfetch specifically means, "we can SELECT the row we just
                # inserted by primary key to get back the server generated
                # defaults". so by definition this can't be used to get the
                # primary key value back, because we need to have it ahead of
                # time.

                compiler.postfetch.append(c)

    values.append((c, col_value, value))


def _append_param_insert_pk_returning(compiler, stmt, c, values, kw):
    """Create a primary key expression in the INSERT statement where
    we want to populate result.inserted_primary_key and RETURNING
    is available.

    """
    if c.default is not None:
        if c.default.is_sequence:
            if compiler.dialect.supports_sequences and (
                not c.default.optional
                or not compiler.dialect.sequences_optional
            ):
                values.append(
                    (
                        c,
                        compiler.preparer.format_column(c),
                        compiler.process(c.default, **kw),
                    )
                )
            compiler.implicit_returning.append(c)
        elif c.default.is_clause_element:
            values.append(
                (
                    c,
                    compiler.preparer.format_column(c),
                    compiler.process(c.default.arg.self_group(), **kw),
                )
            )
            compiler.implicit_returning.append(c)
        else:
            # client side default.  OK we can't use RETURNING, need to
            # do a "prefetch", which in fact fetches the default value
            # on the Python side
            values.append(
                (
                    c,
                    compiler.preparer.format_column(c),
                    _create_insert_prefetch_bind_param(compiler, c, **kw),
                )
            )
    elif c is stmt.table._autoincrement_column or c.server_default is not None:
        compiler.implicit_returning.append(c)
    elif not c.nullable:
        # no .default, no .server_default, not autoincrement, we have
        # no indication this primary key column will have any value
        _warn_pk_with_no_anticipated_value(c)


def _append_param_insert_pk_no_returning(compiler, stmt, c, values, kw):
    """Create a primary key expression in the INSERT statement where
    we want to populate result.inserted_primary_key and we cannot use
    RETURNING.

    Depending on the kind of default here we may create a bound parameter
    in the INSERT statement and pre-execute a default generation function,
    or we may use cursor.lastrowid if supported by the dialect.


    """

    if (
        # column has a Python-side default
        c.default is not None
        and (
            # and it either is not a sequence, or it is and we support
            # sequences and want to invoke it
            not c.default.is_sequence
            or (
                compiler.dialect.supports_sequences
                and (
                    not c.default.optional
                    or not compiler.dialect.sequences_optional
                )
            )
        )
    ) or (
        # column is the "autoincrement column"
        c is stmt.table._autoincrement_column
        and (
            # dialect can't use cursor.lastrowid
            not compiler.dialect.postfetch_lastrowid
            and (
                # column has a Sequence and we support those
                (
                    c.default is not None
                    and c.default.is_sequence
                    and compiler.dialect.supports_sequences
                )
                or
                # column has no default on it, but dialect can run the
                # "autoincrement" mechanism explicitly, e.g. PostgreSQL
                # SERIAL we know the sequence name
                (
                    c.default is None
                    and compiler.dialect.preexecute_autoincrement_sequences
                )
            )
        )
    ):
        # do a pre-execute of the default
        values.append(
            (
                c,
                compiler.preparer.format_column(c),
                _create_insert_prefetch_bind_param(compiler, c, **kw),
            )
        )
    elif (
        c.default is None
        and c.server_default is None
        and not c.nullable
        and c is not stmt.table._autoincrement_column
    ):
        # no .default, no .server_default, not autoincrement, we have
        # no indication this primary key column will have any value
        _warn_pk_with_no_anticipated_value(c)
    elif compiler.dialect.postfetch_lastrowid:
        # finally, where it seems like there will be a generated primary key
        # value and we haven't set up any other way to fetch it, and the
        # dialect supports cursor.lastrowid, switch on the lastrowid flag so
        # that the DefaultExecutionContext calls upon cursor.lastrowid
        compiler.postfetch_lastrowid = True


def _append_param_insert_hasdefault(
    compiler, stmt, c, implicit_return_defaults, values, kw
):
    if c.default.is_sequence:
        if compiler.dialect.supports_sequences and (
            not c.default.optional or not compiler.dialect.sequences_optional
        ):
            values.append(
                (
                    c,
                    compiler.preparer.format_column(c),
                    compiler.process(c.default, **kw),
                )
            )
            if implicit_return_defaults and c in implicit_return_defaults:
                compiler.implicit_returning.append(c)
            elif not c.primary_key:
                compiler.postfetch.append(c)
    elif c.default.is_clause_element:
        values.append(
            (
                c,
                compiler.preparer.format_column(c),
                compiler.process(c.default.arg.self_group(), **kw),
            )
        )

        if implicit_return_defaults and c in implicit_return_defaults:
            compiler.implicit_returning.append(c)
        elif not c.primary_key:
            # don't add primary key column to postfetch
            compiler.postfetch.append(c)
    else:
        values.append(
            (
                c,
                compiler.preparer.format_column(c),
                _create_insert_prefetch_bind_param(compiler, c, **kw),
            )
        )


def _append_param_insert_select_hasdefault(
    compiler: SQLCompiler,
    stmt: ValuesBase,
    c: ColumnClause[Any],
    values: List[Tuple[ColumnClause[Any], str, _SQLExprDefault]],
    kw: Dict[str, Any],
) -> None:

    if default_is_sequence(c.default):
        if compiler.dialect.supports_sequences and (
            not c.default.optional or not compiler.dialect.sequences_optional
        ):
            values.append(
                (c, compiler.preparer.format_column(c), c.default.next_value())
            )
    elif default_is_clause_element(c.default):
        values.append(
            (c, compiler.preparer.format_column(c), c.default.arg.self_group())
        )
    else:
        values.append(
            (
                c,
                compiler.preparer.format_column(c),
                _create_insert_prefetch_bind_param(
                    compiler, c, process=False, **kw
                ),
            )
        )


def _append_param_update(
    compiler, compile_state, stmt, c, implicit_return_defaults, values, kw
):

    include_table = compile_state.include_table_with_column_exprs
    if c.onupdate is not None and not c.onupdate.is_sequence:
        if c.onupdate.is_clause_element:
            values.append(
                (
                    c,
                    compiler.preparer.format_column(
                        c,
                        use_table=include_table,
                    ),
                    compiler.process(c.onupdate.arg.self_group(), **kw),
                )
            )
            if implicit_return_defaults and c in implicit_return_defaults:
                compiler.implicit_returning.append(c)
            else:
                compiler.postfetch.append(c)
        else:
            values.append(
                (
                    c,
                    compiler.preparer.format_column(
                        c,
                        use_table=include_table,
                    ),
                    _create_update_prefetch_bind_param(compiler, c, **kw),
                )
            )
    elif c.server_onupdate is not None:
        if implicit_return_defaults and c in implicit_return_defaults:
            compiler.implicit_returning.append(c)
        else:
            compiler.postfetch.append(c)
    elif (
        implicit_return_defaults
        and (stmt._return_defaults_columns or not stmt._return_defaults)
        and c in implicit_return_defaults
    ):
        compiler.implicit_returning.append(c)


@overload
def _create_insert_prefetch_bind_param(
    compiler: SQLCompiler,
    c: ColumnElement[Any],
    process: Literal[True] = ...,
    **kw: Any,
) -> str:
    ...


@overload
def _create_insert_prefetch_bind_param(
    compiler: SQLCompiler,
    c: ColumnElement[Any],
    process: Literal[False],
    **kw: Any,
) -> elements.BindParameter[Any]:
    ...


def _create_insert_prefetch_bind_param(
    compiler: SQLCompiler,
    c: ColumnElement[Any],
    process: bool = True,
    name: Optional[str] = None,
    **kw: Any,
) -> Union[elements.BindParameter[Any], str]:

    param = _create_bind_param(
        compiler, c, None, process=process, name=name, **kw
    )
    compiler.insert_prefetch.append(c)  # type: ignore
    return param


@overload
def _create_update_prefetch_bind_param(
    compiler: SQLCompiler,
    c: ColumnElement[Any],
    process: Literal[True] = ...,
    **kw: Any,
) -> str:
    ...


@overload
def _create_update_prefetch_bind_param(
    compiler: SQLCompiler,
    c: ColumnElement[Any],
    process: Literal[False],
    **kw: Any,
) -> elements.BindParameter[Any]:
    ...


def _create_update_prefetch_bind_param(
    compiler: SQLCompiler,
    c: ColumnElement[Any],
    process: bool = True,
    name: Optional[str] = None,
    **kw: Any,
) -> Union[elements.BindParameter[Any], str]:
    param = _create_bind_param(
        compiler, c, None, process=process, name=name, **kw
    )
    compiler.update_prefetch.append(c)  # type: ignore
    return param


class _multiparam_column(elements.ColumnElement[Any]):
    _is_multiparam_column = True

    def __init__(self, original, index):
        self.index = index
        self.key = "%s_m%d" % (original.key, index + 1)
        self.original = original
        self.default = original.default
        self.type = original.type

    def compare(self, other, **kw):
        raise NotImplementedError()

    def _copy_internals(self, other, **kw):
        raise NotImplementedError()

    def __eq__(self, other):
        return (
            isinstance(other, _multiparam_column)
            and other.key == self.key
            and other.original == self.original
        )


def _process_multiparam_default_bind(
    compiler: SQLCompiler,
    stmt: ValuesBase,
    c: ColumnClause[Any],
    index: int,
    kw: Dict[str, Any],
) -> str:
    if not c.default:
        raise exc.CompileError(
            "INSERT value for column %s is explicitly rendered as a bound"
            "parameter in the VALUES clause; "
            "a Python-side value or SQL expression is required" % c
        )
    elif default_is_clause_element(c.default):
        return compiler.process(c.default.arg.self_group(), **kw)
    elif c.default.is_sequence:
        # these conditions would have been established
        # by append_param_insert_(?:hasdefault|pk_returning|pk_no_returning)
        # in order for us to be here, so these don't need to be
        # checked
        # assert compiler.dialect.supports_sequences and (
        #    not c.default.optional
        #    or not compiler.dialect.sequences_optional
        # )
        return compiler.process(c.default, **kw)
    else:
        col = _multiparam_column(c, index)
        if isinstance(stmt, dml.Insert):
            return _create_insert_prefetch_bind_param(
                compiler, col, process=True, **kw
            )
        else:
            return _create_update_prefetch_bind_param(
                compiler, col, process=True, **kw
            )


def _get_update_multitable_params(
    compiler,
    stmt,
    compile_state,
    stmt_parameter_tuples,
    check_columns,
    _col_bind_name,
    _getattr_col_key,
    values,
    kw,
):
    normalized_params = dict(
        (coercions.expect(roles.DMLColumnRole, c), param)
        for c, param in stmt_parameter_tuples
    )

    include_table = compile_state.include_table_with_column_exprs

    affected_tables = set()
    for t in compile_state._extra_froms:
        for c in t.c:
            if c in normalized_params:
                affected_tables.add(t)
                check_columns[_getattr_col_key(c)] = c
                value = normalized_params[c]

                col_value = compiler.process(c, include_table=include_table)
                if coercions._is_literal(value):
                    value = _create_bind_param(
                        compiler,
                        c,
                        value,
                        required=value is REQUIRED,
                        name=_col_bind_name(c),
                        **kw,  # TODO: no test coverage for literal binds here
                    )
                elif value._is_bind_parameter:
                    value = _handle_values_anonymous_param(
                        compiler, c, value, name=_col_bind_name(c), **kw
                    )
                else:
                    compiler.postfetch.append(c)
                    value = compiler.process(value.self_group(), **kw)
                values.append((c, col_value, value))
    # determine tables which are actually to be updated - process onupdate
    # and server_onupdate for these
    for t in affected_tables:
        for c in t.c:
            if c in normalized_params:
                continue
            elif c.onupdate is not None and not c.onupdate.is_sequence:
                if c.onupdate.is_clause_element:
                    values.append(
                        (
                            c,
                            compiler.process(c, include_table=include_table),
                            compiler.process(
                                c.onupdate.arg.self_group(), **kw
                            ),
                        )
                    )
                    compiler.postfetch.append(c)
                else:
                    values.append(
                        (
                            c,
                            compiler.process(c, include_table=include_table),
                            _create_update_prefetch_bind_param(
                                compiler, c, name=_col_bind_name(c), **kw
                            ),
                        )
                    )
            elif c.server_onupdate is not None:
                compiler.postfetch.append(c)


def _extend_values_for_multiparams(
    compiler: SQLCompiler,
    stmt: ValuesBase,
    compile_state: DMLState,
    initial_values: Sequence[Tuple[ColumnClause[Any], str, str]],
    _column_as_key: Callable[..., str],
    kw: Dict[str, Any],
) -> List[Sequence[Tuple[ColumnClause[Any], str, str]]]:
    values_0 = initial_values
    values = [initial_values]

    mp = compile_state._multi_parameters
    assert mp is not None
    for i, row in enumerate(mp[1:]):
        extension: List[
            Tuple[
                ColumnClause[Any],
                str,
                str,
            ]
        ] = []

        row = {_column_as_key(key): v for key, v in row.items()}

        for (col, col_expr, param) in values_0:
            if col.key in row:
                key = col.key

                if coercions._is_literal(row[key]):
                    new_param = _create_bind_param(
                        compiler,
                        col,
                        row[key],
                        name="%s_m%d" % (col.key, i + 1),
                        **kw,
                    )
                else:
                    new_param = compiler.process(row[key].self_group(), **kw)
            else:
                new_param = _process_multiparam_default_bind(
                    compiler, stmt, col, i, kw
                )

            extension.append((col, col_expr, new_param))

        values.append(extension)

    return values


def _get_stmt_parameter_tuples_params(
    compiler,
    compile_state,
    parameters,
    stmt_parameter_tuples,
    _column_as_key,
    values,
    kw,
):

    for k, v in stmt_parameter_tuples:
        colkey = _column_as_key(k)
        if colkey is not None:
            parameters.setdefault(colkey, v)
        else:
            # a non-Column expression on the left side;
            # add it to values() in an "as-is" state,
            # coercing right side to bound param

            # note one of the main use cases for this is array slice
            # updates on PostgreSQL, as the left side is also an expression.

            col_expr = compiler.process(
                k, include_table=compile_state.include_table_with_column_exprs
            )

            if coercions._is_literal(v):
                v = compiler.process(
                    elements.BindParameter(None, v, type_=k.type), **kw
                )
            else:
                if v._is_bind_parameter and v.type._isnull:
                    # either unique parameter, or other bound parameters that
                    # were passed in directly
                    # set type to that of the column unconditionally
                    v = v._with_binary_element_type(k.type)

                v = compiler.process(v.self_group(), **kw)

            values.append((k, col_expr, v))


def _get_returning_modifiers(compiler, stmt, compile_state, toplevel):

    need_pks = (
        toplevel
        and compile_state.isinsert
        and not stmt._inline
        and (
            not compiler.for_executemany
            or (
                compiler.dialect.insert_executemany_returning
                and stmt._return_defaults
            )
        )
        and not stmt._returning
        and not compile_state._has_multi_parameters
    )

    implicit_returning = (
        need_pks
        and compiler.dialect.implicit_returning
        and stmt.table.implicit_returning
    )

    if compile_state.isinsert:
        implicit_return_defaults = implicit_returning and stmt._return_defaults
    elif compile_state.isupdate:
        implicit_return_defaults = (
            compiler.dialect.implicit_returning
            and stmt.table.implicit_returning
            and stmt._return_defaults
        )
    else:
        # this line is unused, currently we are always
        # isinsert or isupdate
        implicit_return_defaults = False  # pragma: no cover

    if implicit_return_defaults:
        if not stmt._return_defaults_columns:
            implicit_return_defaults = set(stmt.table.c)
        else:
            implicit_return_defaults = set(stmt._return_defaults_columns)

    postfetch_lastrowid = need_pks and compiler.dialect.postfetch_lastrowid

    return (
        need_pks,
        implicit_returning,
        implicit_return_defaults,
        postfetch_lastrowid,
    )


def _warn_pk_with_no_anticipated_value(c):
    msg = (
        "Column '%s.%s' is marked as a member of the "
        "primary key for table '%s', "
        "but has no Python-side or server-side default generator indicated, "
        "nor does it indicate 'autoincrement=True' or 'nullable=True', "
        "and no explicit value is passed.  "
        "Primary key columns typically may not store NULL."
        % (c.table.fullname, c.name, c.table.fullname)
    )
    if len(c.table.primary_key) > 1:
        msg += (
            " Note that as of SQLAlchemy 1.1, 'autoincrement=True' must be "
            "indicated explicitly for composite (e.g. multicolumn) primary "
            "keys if AUTO_INCREMENT/SERIAL/IDENTITY "
            "behavior is expected for one of the columns in the primary key. "
            "CREATE TABLE statements are impacted by this change as well on "
            "most backends."
        )
    util.warn(msg)
