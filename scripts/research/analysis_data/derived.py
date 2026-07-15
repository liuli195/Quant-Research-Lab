from __future__ import annotations

import duckdb


class DerivedViewError(ValueError):
    """Raised when cumulative source returns cannot be safely normalized."""


def register_return_views(connection: duckdb.DuckDBPyConnection) -> None:
    invalid = connection.sql(
        "select count(*) from results where returns is null or returns <= -1"
    ).fetchone()
    if invalid is None or invalid[0] != 0:
        raise DerivedViewError("results.returns must be finite cumulative returns above -1")
    duplicates = connection.sql(
        "select count(*) from ("
        "select cast(substr(time, 1, 10) as date) trading_date "
        "from results group by trading_date having count(*) <> 1)"
    ).fetchone()
    if duplicates is None or duplicates[0] != 0:
        raise DerivedViewError("results must contain one row per trading date")

    connection.execute(
        """
        create view strategy_daily_returns as
        with normalized as (
            select
                cast(substr(time, 1, 10) as date) as trading_date,
                cast(returns as double) as cumulative_returns
            from results
        ), lagged as (
            select
                trading_date,
                cumulative_returns,
                lag(cumulative_returns) over (order by trading_date) as previous_cumulative
            from normalized
        )
        select
            trading_date,
            cumulative_returns,
            case
                when previous_cumulative is not null
                    then (1.0 + cumulative_returns) / (1.0 + previous_cumulative) - 1.0
                when abs(cumulative_returns) <= 1e-15 then 0.0
                else cast(null as double)
            end as daily_returns,
            previous_cumulative is not null or abs(cumulative_returns) <= 1e-15
                as comparable
        from lagged
        """
    )
    connection.execute(
        """
        create view source_benchmark_returns as
        select
            cast(substr(time, 1, 10) as date) as trading_date,
            cast(benchmark_returns as double) as cumulative_returns
        from results
        """
    )
