---
name: update-schedule
description: Update the user's quiet hours schedule (when they sleep/wake or adjust their routine).
metadata: {"akasic":{}}
---

# Update Schedule

When the user signals a change in their routine — says goodnight, mentions they're waking up, or tells you they're adjusting their sleep schedule — update the schedule file so the proactive loop knows when to stay quiet.

## File location

```
~/.akasic/workspace/schedule.json
```

## Format

```json
{
  "quiet_hours_start": 23,
  "quiet_hours_end": 8,
  "quiet_hours_weight": 0.1
}
```

- `quiet_hours_start` — hour (0–23, local time) when the user goes to sleep
- `quiet_hours_end` — hour (0–23, local time) when the user wakes up
- `quiet_hours_weight` — **always keep this at `0.1`**, do not change it

## How to update

Read the current file first, then write back with updated values:

```python
# read
read_file("~/.akasic/workspace/schedule.json")

# write with updated hours, weight always stays 0.1
write_file("~/.akasic/workspace/schedule.json", '{
  "quiet_hours_start": 23,
  "quiet_hours_end": 8,
  "quiet_hours_weight": 0.1
}')
```

## When to use

- User says 晚安 / goodnight / going to sleep → update `quiet_hours_start` to match the current hour (or what they mention)
- User says 早安 / good morning / just woke up → update `quiet_hours_end` to match
- User says "I've been sleeping later lately" or "I adjusted my schedule" → update both hours accordingly
- If the file doesn't exist yet, create it with the full structure

## Notes

- The proactive loop reads this file on every tick — no restart needed
- If the file is missing, it falls back to the defaults in `config.json`
- Only modify `quiet_hours_start` and `quiet_hours_end`; never touch `quiet_hours_weight`
