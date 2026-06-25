# Research Log: reminder-popup-fix

## Discovery Scope
Light discovery — extension of existing reminder popup system.

## Key Findings

### 1. Root Cause: Broken Countdown System
- `index.html` lines 2867-3016 contain a countdown system added in commit `3d84c7b`
- `_updateMedCountdown` uses `setInterval(500)` (real-time) but checks virtual time
- `_onCountdownExpired` at line 3003: `if (elapsed * 60 >= 10)` — `elapsed` is integer virtual minutes, so `* 60` converts to virtual seconds; condition passes after just 1 virtual minute (60 >= 10)
- Additionally, time jumps can cause large `elapsed` values, triggering immediate close
- **Decision**: Remove countdown system entirely, revert to pre-`3d84c7b` behavior (dialog stays until user action)

### 2. Reference: Working Version `941a10a`
- `/tmp/old_index_941a10a.html` has no countdown system
- Dialog simply stays open until user clicks button
- Backend escalation pipeline handles urgency escalation independently

### 3. Dialog Positioning
- Current: `position:fixed;inset:0` on `#reminder-dialog` (line 2913), appended to `document.body`
- Target: `position:absolute;inset:0` inside `#main-phone-container`
- `#main-phone-container` already has `class="relative"` — supports absolute positioning

### 4. Postpone Visibility
- `task_reminder_skill.py` line 280: `current_schedule.append({...})` adds new schedule node
- `server.py` line 2348: `/api/reminder/tasks` iterates `cs.schedule_nodes` without filtering
- **Decision**: Add `_postponed: true` flag to postpone nodes; filter in API

## Technology Alignment
- No new dependencies required
- Uses existing CSS patterns (Tailwind, inline styles)
- Backend changes are additive (1 line per file)
