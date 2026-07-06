# Research Log

## Discovery Scope
Light discovery process for extension of existing LLM tool chain system.

## Key Findings

### 1. CHAT_TOOLS Structure
- Current: 15 tools defined as a module-level list in `server.py:4552`, each following `{"type":"function", "function":{name, description, parameters}}` format
- New tools: `geocode` and `search_nearby` follow the same pattern
- Both backend implementations (`_amap_client.geocode()`, `_amap_client.search_nearby()`) already exist in `skills/amap_poi/amap_poi.py`
- Flask endpoints `/api/poi/geocode` and `/api/poi/nearby` already exposed but not in CHAT_TOOLS

### 2. System Prompt Pattern
- Current prompt at `server.py:5356-5405`: keyword-based if-else rules for 3 intent types (info query, trip edit, new trip)
- Problem: LLM follows rules literally, never decomposes complex queries
- Decision: Rewrite prompt to teach decomposition thinking pattern, keeping identity/constraints but replacing keyword rules with workflow guidance
- Source: `.claude/skills/kiro-spec-requirements/rules/ears-format.md`, `design-principles.md`

### 3. JSON Mode Availability
- DeepSeek Chat supports `response_format={"type": "json_object"}` via OpenAI-compatible API
- Current code never uses this parameter
- `_call_llm()` at `main.py:105` passes `model, messages, max_tokens, tools, tool_choice, timeout` — no `response_format`
- Decision: Add optional `response_format` parameter to `_call_llm()`, `chat_stream()`, and `chat_stream_continue()`
- Source: DeepSeek API documentation (OpenAI-compatible)

### 4. Coordinate Hardcoding Scope
- 45 occurrences of `39.93` / `116.45` across POI search, route planning, distance calculation, weather, hotel, city lookup
- Decision for this phase: Define `DEFAULT_CENTER_COORD` constant, use in LLM-related paths only (POI search, chat context). Non-LLM paths (route planning distance calc) deferred to P2.
- Source: Grep analysis of server.py

### 5. session_state Multi-Day Structure
- Multi-day keys: `trip_mode`, `trip_days`, `trip_destination`, `trip_transport`, `trip_checkin_lat/lng`, `active_day_index`, `days[]`, `candidate_pool`
- Each day dict: `{day_index, label, selected_pairs, task_list, spatial_matrix, schedule_result, chat_history, transport_override}`
- `get_trip_status` handler currently returns only `selected_pairs` from root session_state, ignoring `days[]`
- Decision: Enhance handler to detect `trip_mode`, return `days[]` summary + current day timeline

### 6. Edit Confirmation Feasibility
- `api_edit_trip()` and `api_multi_day_edit()` both use single-shot LLM calls
- `session_state` pattern already supports caching intermediate state (used for `_review_state`)
- Decision: Add `_pending_confirmation` key to session_state, split into 2 phases (interpret → confirm → execute)
- Frontend already has chat SSE infrastructure; confirm cards can be rendered as SSE events

## Architecture Pattern Evaluation

**Evaluated**: Whether to create a new "ToolManager" abstraction vs. extending existing flat dispatch
**Decision**: Extend existing flat dispatch (add elif branches)
**Rationale**: 
- Only 2 new tools; a new abstraction layer for 17 tools is over-engineering
- Existing pattern is simple, tested, and understood by all contributors
- Adding abstraction would break the "入口薄层" principle from `structure.md`

## Design Decisions

### Build vs. Adopt
- `geocode()` / `search_nearby()` implementation: **Adopt** — already built in amap_poi skill
- JSON mode: **Adopt** — DeepSeek platform capability, just enable via parameter
- System prompt: **Build** — custom decomposition teaching prompt
- Confirmation flow: **Build** — session_state-based state machine

### Generalization
- Requirements 1-3 unified as "LLM tool chain capability" — all addressed by tool additions + prompt rewrite
- Requirements 4-5 unified as "LLM output handling" — addressed by JSON mode + confirmation state
- Requirement 6 is standalone configuration hygiene

### Simplification
- Not creating new abstraction for tool management (flat dispatch pattern works)
- Not creating a general "confirmation framework" — simple state flag sufficient for 2 edit endpoints
- Not replacing all 45 coordinate occurrences — only LLM-relevant paths in this phase
- Not adding new dependencies or files — all changes in existing server.py + main.py

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| DeepSeek JSON mode incompatible with tool_calls | Low | High | Test both modes; fallback to prompt instruct if incompatible |
| New prompt causes regression in simple queries | Medium | Medium | Keep identity + constraint rules; only replace classification logic |
| Confirmation flow breaks existing frontend | Medium | Medium | Return confirm phase as SSE event; existing SSE handler forwards to UI |
| geocode API returns unexpected format | Low | Low | Wrap in try/except; return ERROR with guidance message |
