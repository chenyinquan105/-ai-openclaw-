# Implementation Plan

- [ ] 1. Backend API contract updates for clock state visibility and postponed node filtering
- [ ] 1.1 (P) Add is_running field to all clock API responses
  - Clock state response includes current auto-tick running status
  - jump, offset, speed, start, stop endpoints all return is_running in their JSON body
  - Frontend clockUpdateUI receives definitive is_running value after every operation
  - Existing clock tests (if any) still pass with the new response field
  - _Requirements: 2.1, 2.2, 2.3, 2.6_
  - _Boundary: time_master_

- [ ] 1.2 (P) Tag postponed schedule nodes with internal visibility flag
  - When user postpones a reminder by 30 minutes, the newly created schedule node carries a hidden marker
  - The marker distinguishes postponed nodes from user-created visible nodes
  - Postponed nodes still trigger normally when their time arrives
  - Original reminder node is not affected by the tag
  - _Requirements: 4.1, 4.3_
  - _Boundary: task_reminder_skill_

- [ ] 1.3 Filter postponed nodes from task list API response
  - Task list endpoint excludes nodes with the postponed marker
  - Postponing a reminder does not increase the visible task count in the butler task list
  - Speed validation range updated to accept the new speed semantics (1/60 to 1440)
  - Water and medication nodes continue to appear normally
  - _Requirements: 4.2, 4.4, 5.1, 5.2, 5.3_
  - _Boundary: server.py_

- [ ] 2. Frontend popup countdown with virtual-time-driven escalation
- [ ] 2.1 Build countdown badge display in popup corner following virtual clock
  - Popup opens with a "⏱️ 5:00" badge visible in the top-right corner
  - Badge decrements in real-time as virtual clock advances (at any speed: 1x/60x/300x)
  - When remaining time drops to 60 virtual seconds or less, badge turns red with warning background
  - User clicking confirm or postpone stops the countdown and closes the popup
  - _Requirements: 1.1, 1.2, 1.5, 1.6_
  - _Boundary: index.html reminder dialog_

- [ ] 2.2 Implement automatic escalation when countdown expires
  - After 5 virtual minutes without user action, popup closes and reopens at next escalation level
  - Escalation levels progress: 1 (initial ring) → 2 (heavy urge) → 3 (critical)
  - At level 4 (after third expiry), emergency contact screen displays name and phone number
  - Each escalation level resets the 5-minute countdown for the new popup
  - _Requirements: 1.3, 1.4_
  - _Boundary: index.html reminder dialog_

- [ ] 2.3 Reposition popup inside phone screen container only
  - Popup overlay and semi-transparent backdrop cover only the phone screen area
  - When phone container is unavailable, popup falls back to full-viewport positioning
  - Left-side virtual clock panel and right-side anomaly panel remain interactive during popup
  - Popup DOM is mounted to the phone container element rather than document body
  - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - _Boundary: index.html DOM_

- [ ] 3. Frontend clock controls consuming backend state and aligned speed values
- [ ] 3.1 Wire play/pause button to backend is_running response field
  - After time jump or fast-forward, play button shows correct play/pause state from API response
  - After speed change, play button reflects actual running state
  - During auto-tick, play button consistently shows pause icon (⏸)
  - When clock is stopped, play button shows play icon (▶)
  - _Requirements: 2.4, 2.5_
  - _Boundary: index.html clock controls_

- [ ] 3.2 Align speed multiplier buttons with corrected speed semantics
  - 1x button sends speed value corresponding to 1 virtual minute per 60 real seconds
  - 60x button sends speed value corresponding to 1 virtual minute per 1 real second
  - 300x button sends speed value corresponding to 5 virtual minutes per 1 real second
  - Active speed button is visually highlighted; switching clears previous highlight
  - _Requirements: 5.1, 5.2, 5.3, 5.4_
  - _Boundary: index.html speed controls_

- [ ] 4. Integration verification of complete popup and clock behavior
- [ ] 4.1 Verify full popup lifecycle at each clock speed
  - At 1x speed: popup stays visible for ~5 real minutes, countdown decrements smoothly
  - At 60x speed: popup stays for ~5 real seconds, countdown decrements every real second
  - At 300x speed: popup stays for ~1 real second, countdown jumps rapidly
  - User action during countdown (confirm/postpone) stops timer and closes popup cleanly
  - _Requirements: 1.1, 1.3, 1.5_

- [ ] 4.2 Verify clock state consistency after all operations
  - After slider drag: play button state is correct, virtual time matches slider position
  - After fast-forward: clock continues auto-ticking, play button shows correct state
  - After speed change: clock continues at new speed, play button unchanged
  - Quick consecutive operations do not cause UI flicker or state drift
  - _Requirements: 2.4, 2.5, 2.6_

- [ ] 4.3 Verify postponed reminders do not create visible task icons
  - Postpone a medication reminder → task list count unchanged
  - Fast-forward 30 virtual minutes → popup reappears at postponed time
  - User-created water and medication reminders still appear normally in list
  - _Requirements: 4.1, 4.2, 4.3, 4.4_
