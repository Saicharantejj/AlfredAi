import os
import shutil
import psycopg
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
USERS_DIR = BASE_DIR / "users"
DATABASE_URL = os.getenv("ALFRED_DATABASE_URL")

def wipe_postgres():
    if not DATABASE_URL:
        print("No DATABASE_URL found. Skipping Postgres wipe.")
        return
    
    conn_info = DATABASE_URL.split('@')[-1]
    print(f"Connecting to Postgres: {conn_info}")
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                print("Dropping tables...")
                cur.execute("DROP TABLE IF EXISTS accounts CASCADE;")
                cur.execute("DROP TABLE IF EXISTS user_documents CASCADE;")
                conn.commit()
                print("✅ Postgres tables dropped.")
    except Exception as e:
        print(f"❌ Postgres error: {e}")

def wipe_local_files():
    print(f"Wiping local files in: {USERS_DIR}")
    if not USERS_DIR.exists():
        print("Users directory not found.")
        return

    for item in USERS_DIR.iterdir():
        if item.name == ".gitkeep":
            continue
        try:
            if item.is_file():
                item.unlink()
                print(f"✅ Deleted file: {item.name}")
            elif item.is_dir():
                shutil.rmtree(item)
                print(f"✅ Deleted directory: {item.name}")
        except Exception as e:
            print(f"❌ Error deleting {item.name}: {e}")

if __name__ == "__main__":
    print("\n--- STARTING FULL DATABASE WIPE ---")
    wipe_postgres()
    wipe_local_files()
    print("--- WIPE COMPLETE ---\n")
