"""
One-time script: marks any open position with net_qty = 0 as 'closed'.
Run from the options_workflow directory:
    python3 close_orphaned_positions.py
"""
import os
import sqlite3

db_path = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "option_trade_journal", "trades.db")
)

con = sqlite3.connect(db_path)
con.row_factory = sqlite3.Row

orphans = con.execute("""
    SELECT p.id, p.symbol, p.option_type, p.strike, p.expiry,
           SUM(CASE WHEN t.action = 'sell' THEN  t.quantity
                    WHEN t.action = 'buy'  THEN -t.quantity
                    ELSE 0 END) AS net_qty
    FROM positions p
    JOIN trades t ON t.position_id = p.id
    WHERE p.status = 'open' AND t.is_test = 0
    GROUP BY p.id
    HAVING net_qty = 0
""").fetchall()

if not orphans:
    print("No orphaned positions found.")
    con.close()
    exit(0)

print("Orphaned positions (net_qty = 0) to be closed:")
for r in orphans:
    print(f"  id={r['id']}  {r['symbol']} {r['option_type'].upper()} ${r['strike']} {r['expiry']}")

confirm = input("\nClose these? [y/N] ").strip().lower()
if confirm != "y":
    print("Aborted.")
    con.close()
    exit(0)

for r in orphans:
    con.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (r["id"],))

con.commit()
con.close()
print("Done — positions marked closed.")
