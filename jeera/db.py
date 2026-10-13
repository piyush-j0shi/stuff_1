import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from jeera import config


def connect():
    conn = psycopg.connect(config.database_url, row_factory=dict_row)
    register_vector(conn)
    return conn


def init():
    sql = (config.root / "jeera" / "schema.sql").read_text()
    with psycopg.connect(config.database_url) as conn:
        conn.execute(sql)
    print("schema : ready")


if __name__ == "__main__":
    init()
