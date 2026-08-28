from __future__ import annotations

from python.utils.config import get, require
from python.utils.logger import get_logger

logger = get_logger(__name__)


def get_workspace_client():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient(
        host=require("DATABRICKS_HOST"), token=require("DATABRICKS_TOKEN")
    )


def _warehouse_id() -> str:
    # DATABRICKS_HTTP_PATH looks like /sql/1.0/warehouses/<warehouse_id>
    return require("DATABRICKS_HTTP_PATH").rstrip("/").split("/")[-1]


def execute_statement(sql: str) -> list[dict]:
    client = get_workspace_client()
    logger.debug("Executing Databricks SQL statement")
    response = client.statement_execution.execute_statement(
        statement=sql,
        warehouse_id=_warehouse_id(),
        wait_timeout="30s",
    )
    status = response.status
    if status and status.state and status.state.value != "SUCCEEDED":
        raise RuntimeError(f"Databricks SQL statement failed: {status}")

    if not response.result or not response.manifest or not response.manifest.schema:
        return []
    columns = [col.name for col in response.manifest.schema.columns]
    rows = response.result.data_array or []
    return [dict(zip(columns, row)) for row in rows]


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def insert_ops_incident_log(record: dict) -> None:
    table = get("DATABRICKS_OPS_INCIDENT_TABLE", "dev.ops_incidents.incident_log")
    columns = list(record.keys()) + ["loaded_at"]
    values = [_sql_literal(v) for v in record.values()] + ["current_timestamp()"]
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(values)})"
    logger.debug("Inserting ops incident log row: %s", record.get("incident_id"))
    execute_statement(sql)
