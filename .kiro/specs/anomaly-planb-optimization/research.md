# Research Log

## Discovery Scope
Light discovery for Extension-type feature. Focus on integration points, existing patterns, and modified file identification.

## Key Findings

### Codebase Analysis
- **Anomaly buttons**: 4 buttons at `index.html:267-278`, no `id` attributes, no active state CSS toggling
- **Active exceptions**: `activeExceptions` array at `index.html:2719`, `renderActiveExceptions()` targets non-existent `#active-exceptions-list`
- **Notification system**: `appendExecChatMessage()` at `index.html:2869` targets non-existent `#exec-plan-content` (complete no-op). Trip chat area `#trip-chat-area` exists and is created by `renderTripView()`
- **Timeline bar**: `renderMetroTimelineForMainFace()` at `index.html:1997` and `_refreshTripView()` at `index.html:2154` work correctly
- **Audio system**: `_playNotificationSound()` at `index.html:5123` uses Web Audio API dual-tone (880Hz+1100Hz), already warmed up via `_initAudioOnFirstTouch`
- **Schedule card**: `renderSchedule()` at `index.html:1607` creates `bg-gray-900` dark card, sets `lastScheduleData`
- **Amap POI client**: `skills/amap_poi/amap_poi.py` has `search_nearby()` method, category code `050500` for cafe/tea. Pre-cache interceptor in `search_poi_matrix()` bypasses real API for cached data
- **Swap shop API**: `/api/swap_shop` at `server.py:1860`, `/api/get_swap_candidates` at `server.py:1690`
- **Insert shelter API**: `/api/insert_shelter` at `server.py:1619`

### Existing Patterns to Follow
- **Button active state**: Speed buttons use `clockUpdateUI()` pattern with `border-green-400 bg-green-900/30` toggle (reference implementation)
- **Modal + second modal**: `triggerDemoException()` → `showSecondModal()` → `confirmSecondModal()` callback pattern
- **PlanB execution**: `executePlanB()` dispatches by `optId`, each branch handles its own API calls
- **Timeline refresh**: `_refreshTripView()` pattern: remove old view → `renderTripView()` + `renderMetroTimelineForMainFace()` + `buildWaypointCoords()`

### Design Decisions
- **Notification function**: Create `appendTripNotification()` targeting existing `#trip-chat-area` instead of fixing `#exec-cover` (which requires complex DOM injection)
- **Confirmation flow**: Create `appendTripConfirm()` with inline buttons, insert between `showSecondModal` callback and API call
- **Amap API**: Bypass pre-cache by calling `AmapPOIClient.search_nearby()` directly instead of `search_poi_matrix()`
- **Button toggle**: Use `activeExceptions` array as source of truth, reconcile button state on push/remove

### Integration Risks
- **Low risk**: Button toggle, notification functions, ringing — all are additive changes
- **Medium risk**: Amap API direct call — network dependency, requires fallback to cache
- **Medium risk**: Swap confirmation flow — changes callback behavior, must preserve error handling chain
