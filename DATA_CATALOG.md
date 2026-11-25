# Parquet Data Catalog

This catalog lists all databases and tables dumped and converted to Parquet.
You can download any specific table, download an entire database bundle, or directly query any table in Python/DuckDB without downloading manually.

## Summary of Databases

### Database: `SQLite_masterdb`

- **Tables**: 3
- **Total Rows**: 13
- **Total Parquet Size**: 4.4 KiB
- **Download All Tables for `SQLite_masterdb`**: [`SQLite_masterdb.zip`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.zip)

| Table | Rows | Parquet Size | Columns | Direct Download Link |
| :--- | :--- | :--- | :--- | :--- |
| `notes` | 3 | 898 B | `id`, `body` | [Download `SQLite_masterdb.notes.parquet`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.notes.parquet) |
| `orders` | 5 | 1.8 KiB | `id`, `user_id`, `item`, `note`, `amount` | [Download `SQLite_masterdb.orders.parquet`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.orders.parquet) |
| `users` | 5 | 1.8 KiB | `id`, `ssn`, `email`, `password`, `username` | [Download `SQLite_masterdb.users.parquet`](https://github.com/chandranshB/hello-world/releases/latest/download/SQLite_masterdb.users.parquet) |

---

## Fast Usage Examples

### 1. Download via Python CLI Helper (`download.py`)

A zero-dependency helper included in the repository:

```bash
# List all databases and tables
python download.py --list

# Download a specific table from a specific DB
python download.py --db SQLite_masterdb --table users

# Download an entire database
python download.py --db SQLite_masterdb

# Download all databases
python download.py --all

# Print direct download URL
python download.py --db SQLite_masterdb --table users --url
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
