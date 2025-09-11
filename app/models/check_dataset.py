from sqlalchemy import create_engine, inspect, text
from app.models.database import DATABASE_URL

# Connect to your SQLite file
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
inspector = inspect(engine)

# 1️⃣ Show all tables
print("Tables:", inspector.get_table_names())

# 2️⃣ Show columns for each table
for table_name in inspector.get_table_names():
    print(f"\nTable: {table_name}")
    for column in inspector.get_columns(table_name):
        print(f"  {column['name']} ({column['type']})")

# 3️⃣ Optional: show all rows
with engine.connect() as conn:
    for table_name in inspector.get_table_names():
        rows = conn.execute(text(f"SELECT * FROM {table_name}")).fetchall()
        for row in rows:
            print(dict(row))
        
