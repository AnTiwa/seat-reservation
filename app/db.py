import os

from psycopg_pool import ConnectionPool


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/seats",
)

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=2,
    max_size=20,
    open=False,
    check=ConnectionPool.check_connection,
)


def init_pool():
    pool.open()


def close_pool():
    pool.close()


def init_schema():
    with pool.connection() as conn:
        with open("app/schema.sql", "r", encoding="utf-8") as f:
            conn.execute(f.read())
        conn.commit()
