"""Small MySQL client wrapper for the V3 repository layer."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any


class StorageConfigError(RuntimeError):
    """Raised when the MySQL storage layer is not configured."""


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    pool_size: int = 5

    @classmethod
    def from_env(cls) -> "MySQLConfig":
        missing = [
            name
            for name in ("MYSQL_HOST", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")
            if not os.environ.get(name)
        ]
        if missing:
            raise StorageConfigError(
                "MySQL storage is not configured; missing environment variables: "
                + ", ".join(missing)
            )

        try:
            port = int(os.environ.get("MYSQL_PORT", "3306"))
        except ValueError as exc:
            raise StorageConfigError("MYSQL_PORT must be an integer") from exc

        try:
            pool_size = int(os.environ.get("MYSQL_POOL_SIZE", "5"))
        except ValueError as exc:
            raise StorageConfigError("MYSQL_POOL_SIZE must be an integer") from exc

        return cls(
            host=os.environ["MYSQL_HOST"],
            port=port,
            database=os.environ["MYSQL_DATABASE"],
            user=os.environ["MYSQL_USER"],
            password=os.environ["MYSQL_PASSWORD"],
            pool_size=pool_size,
        )


def _load_driver() -> Any:
    try:
        import pymysql  # type: ignore[import-not-found]
    except ImportError as exc:
        raise StorageConfigError(
            "PyMySQL is required for MySQL storage; install backend/tencent_scf/requirements.txt"
        ) from exc
    return pymysql


def get_connection(config: MySQLConfig | None = None) -> Any:
    cfg = config or MySQLConfig.from_env()
    pymysql = _load_driver()
    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def execute(sql: str, params: Any = None, connection: Any | None = None) -> dict[str, Any]:
    owns_connection = connection is None
    conn = connection or get_connection()
    try:
        with conn.cursor() as cursor:
            rowcount = cursor.execute(sql, params or ())
        if owns_connection:
            conn.commit()
        return {"rowcount": rowcount}
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        if owns_connection:
            conn.close()


def fetch_one(sql: str, params: Any = None, connection: Any | None = None) -> dict[str, Any] | None:
    owns_connection = connection is None
    conn = connection or get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            return cursor.fetchone()
    finally:
        if owns_connection:
            conn.close()


def fetch_all(sql: str, params: Any = None, connection: Any | None = None) -> list[dict[str, Any]]:
    owns_connection = connection is None
    conn = connection or get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            return list(cursor.fetchall())
    finally:
        if owns_connection:
            conn.close()
