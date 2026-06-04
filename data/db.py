import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).parent.parent / "database" / "worldcup_database.db"
SCHEMA_PATH = Path(__file__).parent.parent / "database" / "schema.sql"

def get_connection(db_path: Path = DB_PATH)-> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    #dict esen lehet belole hivni row['home_score']
    conn.row_factory = sqlite3.Row
    
    conn.execute("PRAGMA journal_mode = WAL") 
    conn.execute("PRAGMA foreign_keys = ON")
    return conn 

def init_db(db_path: Path = DB_PATH)-> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection(db_path) as conn:
        conn.executescript(schema)
    print(f"Adatbázis inicializálva: {db_path}")
    

if __name__ == "__main__":
    init_db()