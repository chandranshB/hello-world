# Data Catalog: Parquet & SQLite Dumps

This catalog lists all databases and tables dumped, converted to Parquet, and compiled into SQLite databases.
You can download individual Parquet tables, SQLite databases, or complete bundles.

## Complete Bundles (All Databases)

- **Complete SQLite Bundle**: [`all-databases-sqlite.zip`](https://github.com/chandranshB/hello-world/releases/latest/download/all-databases-sqlite.zip) (includes `.sqlite` + `.sql` dump)
- **Complete Parquet Bundle**: [`all-databases-parquet.zip`](https://github.com/chandranshB/hello-world/releases/latest/download/all-databases-parquet.zip)
- **Unified SQLite DB (Direct)**: [`all-databases.sqlite`](https://github.com/chandranshB/hello-world/releases/latest/download/all-databases.sqlite)

## Summary of Databases

### Database: `SQLite_masterdb`

- **Tables**: 3
- **Total Rows**: 13
- **Total Parquet Size**: 4.4 KiB
- **SQLite Database**: [`SQLite_masterdb.sqlite`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.sqlite) | [`SQLite_masterdb.sqlite.zip`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.sqlite.zip) | [`SQLite_masterdb.sql`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.sql)
- **Parquet Bundle**: [`SQLite_masterdb.zip`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.zip)

| Table | Rows | Parquet Size | Columns | Direct Download Link |
| :--- | :--- | :--- | :--- | :--- |
| `notes` | 3 | 898 B | `id`, `body` | [Download `SQLite_masterdb.notes.parquet`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.notes.parquet) |
| `orders` | 5 | 1.8 KiB | `id`, `user_id`, `item`, `note`, `amount` | [Download `SQLite_masterdb.orders.parquet`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.orders.parquet) |
| `users` | 5 | 1.8 KiB | `id`, `ssn`, `email`, `password`, `username` | [Download `SQLite_masterdb.users.parquet`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.users.parquet) |

---

## Fast Usage Examples

### 1. Download via Python CLI Helper (`download.py`)

```bash
# List all databases and tables
python download.py --list

# Download a specific table from a specific DB
python download.py --db SQLite_masterdb --table users

# Download an entire database
python download.py --db SQLite_masterdb

# Download all databases
python download.py --all
```

### 2. Direct Query in DuckDB (Zero local download required)

```python
import duckdb

# Query a specific table directly from GitHub Release over HTTPS:
url = 'https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.users.parquet'
df = duckdb.read_parquet(url)
print(df.show())

# Or run complex SQL directly against the remote Parquet file:
duckdb.sql(f'''
    SELECT username, email
    FROM read_parquet('{url}')
    WHERE id <= 3
''').show()
```

### 3. Read in Pandas / Polars

```python
# Pandas
import pandas as pd
df = pd.read_parquet('https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.users.parquet')

# Polars
import polars as pl
df = pl.read_parquet('https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.users.parquet')
```

### 4. Direct Download via curl / wget / PowerShell

**PowerShell:**
```powershell
Invoke-WebRequest -Uri 'https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.users.parquet' -OutFile 'users.parquet'
```

**curl:**
```bash
curl -LO 'https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.users.parquet'
```

**wget:**
```bash
wget 'https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.users.parquet'
```
