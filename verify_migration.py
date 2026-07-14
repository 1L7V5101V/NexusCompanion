"""Verify session migration."""
import sqlite3, os

db_path = os.path.expanduser(r'~\.local\share\opencode\opencode.db')
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Check sessions
cur.execute("SELECT COUNT(*) FROM session WHERE directory = ?", ('D:/.Projects/NexusCompanion',))
print(f"Sessions with NexusCompanion: {cur.fetchone()[0]}")

cur.execute("SELECT COUNT(*) FROM session WHERE directory = ?", ('D:/.Projects/akashic-agent',))
print(f"Sessions still with akashic-agent (should be 0): {cur.fetchone()[0]}")

# Check project
cur.execute("SELECT worktree FROM project WHERE worktree LIKE '%NexusCompanion'")
print(f"\nProject worktree: {cur.fetchone()}")

cur.execute("SELECT worktree FROM project WHERE worktree LIKE '%akashic-agent'")
print(f"Project worktree old (should be None): {cur.fetchone()}")

# Check project_directory
cur.execute("SELECT project_id, directory FROM project_directory WHERE directory LIKE '%NexusCompanion'")
print(f"\nProjectDirectory: {cur.fetchone()}")

cur.execute("SELECT project_id, directory FROM project_directory WHERE directory LIKE '%akashic-agent'")
print(f"ProjectDirectory old (should be None): {cur.fetchone()}")

# Show a sample session
cur.execute("SELECT id, directory FROM session WHERE directory = 'D:/.Projects/NexusCompanion' LIMIT 3")
for r in cur.fetchall():
    print(f"\nSession: {r[0]}, dir: {r[1]}")

conn.close()
