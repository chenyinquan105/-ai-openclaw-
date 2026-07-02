# Research Log

## Discovery Type
Light discovery (Extension to existing SPA)

## Key Findings

### Amap JS API 2.0
- Loader: `<script src="https://webapi.amap.com/v2/maps?v=2.0&key=..."></script>`
- Key type: Must be "JS API" platform (separate from "Web服务")
- Key registered: `f24e3c6371ef8e5760ffbea569987fee`
- GCJ-02 coordinates (already used by existing AmapPOIClient)
- Polyline, Marker, InfoWindow all available in standard API
- No additional npm packages needed (CDN only)

### Existing Architecture Integration
- `index.html` is a single-file SPA (~6088 lines) with Tailwind CSS
- All JS inline in `<script>` block — no module system
- DOM structure: phone container → 3D flip card (front/back) → dynamic-content
- Virtual clock: `/api/clock/status` returns `{virtual_time, is_running, speed}`
- Route planner: `plan_route()` outputs `{route: [{to_coord, duration_minutes, ...}]}`
- POI data already has `coord` field from Amap API

### Design Decisions
1. **No new files** — all changes within `index.html` (single-file SPA pattern)
2. **CDN Amap** — no build step, load via script tag
3. **Poll-based position** — 5s interval polling `/api/clock/status` for simplicity
4. **Linear interpolation** — sufficient for city-scale waypoint distances
5. **Full overlay map** — z-40 absolute panel, same pattern as old exec-cover

### Risks
- Amap JS API loads async; need guard in initMapPanel()
- Some shops may lack coord; skip in waypointCoords
- 5s poll may feel laggy at high speed clock; tunable interval
