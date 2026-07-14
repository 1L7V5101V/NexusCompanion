"""Check scheduler state on server."""
import json
from datetime import datetime, timezone

with open("/root/.nexus/workspace/schedules.json") as f:
    jobs = json.load(f)

now = datetime.now(timezone.utc)
print(f"Current time (UTC): {now.isoformat()}")
print(f"Number of jobs: {len(jobs)}")
for j in jobs:
    fire_at = datetime.fromisoformat(j["fire_at"])
    expired = "EXPIRED" if fire_at < now else "FUTURE"
    print(f"  {j['name']}: fire_at={j['fire_at']}   {expired}   run_count={j['run_count']}   enabled={j['enabled']}")
