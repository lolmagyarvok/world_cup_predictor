import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).parent / "database" / "worldcup_database.db"
SCHEMA_PATH = Path(__file__).parent / "database" / "schema.sql"

def get_connection(db_path: Path = DB_PATH)-> sqlite3.Connection:
    db_path.parent.mkdir(paretns=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL") 
    conn.execute("PRAGMA foreign_keys = ON")
    return conn 
