# Navi Directions UX Redesign

**Status:** Draft
**Author:** Claude + Matt
**Date:** 2026-04-26
**Implementation:** Deferred to dedicated session

---

## 1. Current State

### Components

| Component | File | Role |
|-----------|------|------|
| SearchBar | `SearchBar.jsx` | Overloaded: search, add stop, set origin (hidden modes) |
| StopList | `StopList.jsx` | Drag-drop reordering of stops |
| GpsOriginItem | `GpsOriginItem.jsx` | "Your location" row when GPS granted |
| StopItem | `StopItem.jsx` | Individual stop with delete button |
| ModeSelector | `ModeSelector.jsx` | auto/pedestrian/bicycle toggle |
| ManeuverList | `ManeuverList.jsx` | Turn-by-turn directions display |
| PlaceDetail | `PlaceDetail.jsx` | "Directions" button for selected place |

### State Model

```javascript
stops: []                    // Array of {id, lat, lon, name, source, matchCode}
gpsOrigin: true              // Use GPS as origin when available
pendingDestination: null     // Place waiting for origin (GPS-denied flow)
route: null                  // Valhalla trip response
routeLoading: false
routeError: null
```

### Failure Modes

1. **No visible from/to inputs** — Users cannot see or directly edit origin/destination
2. **SearchBar hidden mode-switching** — Three different behaviors based on invisible state:
   - Normal: opens place detail
   - With `pendingDestination`: first result becomes origin
   - After adding stops: unclear which role next selection plays
3. **GPS-denied flow uses ephemeral toast** — "Set a starting point" disappears, no persistent UI guidance
4. **No swap button** — Cannot reverse route direction
5. **No map context menu** — Right-click/long-press does nothing
6. **No waypoint addition UI** — Only drag-drop reordering, no insert-between
7. **Place panel "Directions" silently sets up route** — Based on hidden state, no confirmation

---

## 2. Design Principles

1. **Direct manipulation over hidden modes** — Every action should have visible UI
2. **Two visible inputs always** — When in directions mode, From and To fields are always visible
3. **Spatial interactions over linear** — Radial menu for map interactions, not dropdowns
4. **Same gesture model everywhere** — Right-click (desktop) = long-press (mobile)
5. **Preserve existing state model** — `stops[]` array stays, just better UI on top

---

## 3. Visual Mockup — Directions Panel

```
┌─────────────────────────────────────┐
│  DIRECTIONS                         │
├─────────────────────────────────────┤
│                                     │
│  From: [📍 Your location      ][×]  │
│         ────────────────────────    │
│                    [⇅]              │  ← Swap button
│         ────────────────────────    │
│  To:   [Coffee shop on Main St][×]  │
│                                     │
│  [+ Add stop]                       │
│                                     │
├─────────────────────────────────────┤
│  [🚗 Auto] [🚶 Walk] [🚲 Bike]      │
├─────────────────────────────────────┤
│  ┌─────────────────────────────┐    │
│  │ 12 min · 4.2 mi             │    │
│  │ via W Main St               │    │
│  └─────────────────────────────┘    │
│                                     │
│  ▼ Turn-by-turn (expandable)        │
│    → Head north on Oak Ave          │
│    ↱ Turn right onto Main St        │
│    ◉ Arrive at destination          │
│                                     │
└─────────────────────────────────────┘
```

### Input States

**From field:**
- GPS granted: Shows "📍 Your location" pill with clear button
- GPS denied/cleared: Empty, placeholder "Starting point..."
- Filled: Shows place name with clear button

**To field:**
- Empty: Placeholder "Destination..."
- Filled: Shows place name with clear button

**Active input:**
- Blue border highlight
- Search dropdown appears on typing
- Map click populates this field

---

## 4. Visual Mockup — Radial Map Menu

```
                    Drop pin
                      🔴
                    ╱    ╲
                  ╱        ╲
    Directions  ╱            ╲  Directions
    from here 🟢──────────────🔵 to here
              │   43.6166    │
              │  -116.2008   │
              │  [loading…]  │  ← Center disc with coords/label
    Add as   🟡──────────────🟣 Save place
    stop       ╲            ╱
                 ╲        ╱
                   ╲    ╱
                    🟠
                 What's here
```

### Wedge Layout (60° each)

| Position | Action | Icon | Color |
|----------|--------|------|-------|
| Top | Drop pin | Pin | Red |
| Top-right | Directions to here | Arrow-in | Blue |
| Bottom-right | Save place | Star | Purple |
| Bottom | What's here | Info | Orange |
| Bottom-left | Add as stop | Plus | Yellow |
| Top-left | Directions from here | Arrow-out | Green |

### Behavior

- **Trigger:** Right-click (desktop) or long-press 400-500ms (mobile)
- **Center disc:** ~40px diameter, shows coordinates immediately, reverse-geocoded label async
- **Wedge highlight:** On hover (desktop) or drag-over (mobile)
- **Commit:** Release on wedge (mobile) or click wedge (desktop)
- **Cancel:** Release outside, Escape key, tap elsewhere

---

## 5. Component Breakdown

### DirectionsPanel

Replaces current Panel directions mode.

```
Props: none (reads from store)
State: none (all in global store)
Children:
  - LocationInput (from)
  - SwapButton
  - LocationInput (to)
  - WaypointList (if stops.length > 2)
  - AddStopButton
  - ModeSelector
  - RouteSummary
  - ManeuverList (collapsible)
```

### LocationInput

Reusable component for from, to, and waypoint inputs.

```
Props:
  - slot: 'from' | 'to' | `waypoint:${index}`
  - value: { lat, lon, name, source } | null
  - placeholder: string
  - showGpsPill: boolean
  - onClear: () => void

Features:
  - Search-as-you-type (Photon geocoder)
  - GPS pill state with clear button
  - Active-input visual state (blue border)
  - Reverse-geocoded labels for coord-only entries
  - Dropdown for search results
```

### SwapButton

Simple button between From and To inputs.

```
Props: none
Action: Swaps stops[0] and stops[stops.length - 1]
Visual: ⇅ icon, hover highlight
```

### WaypointList

Refactored from existing StopList, preserves drag-drop.

```
Props: none (reads stops from store)
Features:
  - Only renders stops[1..n-1] (middle waypoints)
  - Drag-drop reordering via @dnd-kit
  - Delete button per waypoint
  - "Via" label prefix
```

### RadialMenu

New general-purpose component.

```
Props:
  - open: boolean
  - x: number (screen X)
  - y: number (screen Y)
  - lat: number
  - lon: number
  - wedges: Array<{ id, icon, label, action: (lat, lon) => void }>
  - onClose: () => void

Features:
  - Configurable wedge count and actions
  - Async center label (reverse geocode)
  - Keyboard dismissal (Escape)
  - Touch-friendly sizing on mobile
  - Fade in/out animations
```

---

## 6. State Model

### Existing (unchanged)

```javascript
stops: []              // Origin = stops[0], destination = stops[last], waypoints in between
gpsOrigin: boolean     // Whether GPS should be used as origin
route: object | null   // Valhalla trip response
routeLoading: boolean
routeError: string | null
```

### New

```javascript
activeInputSlot: 'from' | 'to' | `waypoint:${N}` | null
// Which input is currently focused/active for map-click-to-fill

radialMenuState: {
  open: boolean,
  x: number,           // Screen coordinates
  y: number,
  lat: number,         // Map coordinates
  lon: number,
  label: string | null // Reverse-geocoded, async populated
}
```

### Removed

```javascript
pendingDestination: null  // No longer needed — explicit inputs replace hidden state
```

---

## 7. Interaction Flows

### Open directions tab fresh

1. From field shows GPS pill if `geoPermission === 'granted'`, else empty
2. To field is empty, focused by default
3. No route calculated yet

### Click "Directions" from place panel

1. Directions panel opens (if not already)
2. To field auto-fills with selected place
3. From field:
   - If GPS granted: shows GPS pill
   - Else: empty, receives focus
4. Route calculates if both filled

### Type in input

1. Input receives focus, becomes `activeInputSlot`
2. Photon search fires on debounce (300ms)
3. Dropdown shows results
4. Select result → populates input, clears dropdown
5. Route recalculates

### Right-click / long-press on map

1. Radial menu appears centered on click point
2. Center disc shows coordinates immediately
3. Reverse geocode fires async, populates label
4. User hovers/drags to wedge:

| Wedge | Action |
|-------|--------|
| **Directions from here** | Opens directions if closed, fills From with coords, focuses To |
| **Directions to here** | Opens directions if closed, fills To with coords, focuses From if empty |
| **Add as stop** | Inserts new stop before destination |
| **What's here** | Reverse geocode → opens place panel |
| **Drop pin** | Creates transient marker (session-only) |
| **Save place** | Opens save dialog (auth required) |

5. Release outside or Escape → dismisses without action

### Click map with active input

When directions panel is open and an input is focused (`activeInputSlot !== null`):

1. Single click on map
2. Clicked coordinates populate the active input
3. Reverse geocode fires to get display name
4. Input loses focus, `activeInputSlot = null`
5. Route recalculates

### Swap button

1. Click swap button
2. `stops[0]` and `stops[stops.length - 1]` swap positions
3. If GPS was origin, GPS pill moves to destination (unusual but allowed)
4. Route recalculates

---

## 8. Place Panel "Directions" Handoff

**Current behavior:** Calls `startDirections(place)` with complex conditional logic, may show toast.

**New behavior:**

```javascript
handleDirections = () => {
  // Always open directions panel
  setActiveTab('directions')

  // Fill destination
  setStop(stops.length, {  // Appends or replaces last
    lat: place.lat,
    lon: place.lon,
    name: place.name,
    source: place.source
  })

  // Handle origin
  if (geoPermission === 'granted') {
    setGpsOrigin(true)  // GPS pill in From
  } else if (stops.length === 0) {
    setActiveInputSlot('from')  // Focus From input
  }

  // Close place panel
  clearSelectedPlace()
}
```

**No toast needed** — UI is self-explanatory with visible From/To fields.

---

## 9. Radial Menu Specifics

### Trigger

| Platform | Gesture | Duration |
|----------|---------|----------|
| Desktop | Right-click | Instant |
| Mobile | Long-press | 400-500ms |

### Conflict Avoidance

Long-press must NOT fire during active pan:
- Track touch start position
- If touch moves >5px before timer fires, cancel long-press
- Pan gesture takes priority

### Geometry

```
Outer radius: ~80px from center
Inner radius: ~40px (center disc)
Wedge angle: 60° each (6 wedges)
Gap between wedges: 2px
```

### Visual States

| Element | Default | Hover/Active | Selected |
|---------|---------|--------------|----------|
| Wedge background | `rgba(0,0,0,0.7)` | `rgba(0,0,0,0.85)` | Wedge accent color |
| Wedge icon | White, 50% opacity | White, 100% opacity | White |
| Wedge label | Hidden | Shown (tooltip) | Shown |
| Center disc | Dark, coords visible | — | — |

### Animation

- **Fade in:** <100ms ease-out
- **Fade out:** <150ms ease-in
- **Wedge hover:** Instant background change
- **Center label:** Fade in when reverse geocode completes

---

## 10. Mobile Considerations

### Panel Layout

**Decision needed:** Bottom sheet vs side panel

| Option | Pros | Cons |
|--------|------|------|
| Bottom sheet | Familiar (Google Maps), thumb-friendly | Complex sheet state management |
| Side panel | Consistent with desktop, more vertical space | Covers more map, less thumb-friendly |

**Recommendation:** Bottom sheet with three states: collapsed (summary only), half (inputs + summary), full (inputs + maneuvers).

### Long-press Timing

**Decision needed:** Exact timing

| Duration | Feel |
|----------|------|
| 400ms | Snappy, risk of accidental trigger |
| 450ms | Balanced |
| 500ms | Deliberate, slightly sluggish |

**Recommendation:** Start with 450ms, tune based on testing.

### Radial Sizing

Mobile radial should be larger for finger touch:
- Outer radius: ~100px (vs 80px desktop)
- Center disc: ~50px (vs 40px desktop)
- Minimum wedge touch target: 48px

### Compact Directions Mode

When route is calculated and user is navigating:
1. Collapse From/To inputs to single-line summary
2. Show prominent next maneuver
3. Expand on tap to edit inputs
4. Maneuver list scrollable

### Keyboard Awareness

- Detect keyboard open via `visualViewport` API
- Shift panel content up to keep active input visible
- Don't let keyboard overlap input being typed in

---

## 11. Place Panel Restructure

**Out of scope for this document.**

Separate session will address:
- Cleaner info card layout (Google Maps style)
- Better visual hierarchy
- Action button placement
- No new data sources, just CSS/JSX polish

---

## 12. Out of Scope (Future Phases)

| Feature | Notes |
|---------|-------|
| Saved routes | Auth required, dedicated work |
| Route alternatives | Valhalla supports, surface in v2 |
| Avoid tolls/highways | Valhalla supports via costing options |
| Real-time rerouting | Requires location tracking loop |
| Multi-modal | Drive + transit + walk hybrids |
| Traffic-aware routing | Requires traffic data source |
| Offline routing | Requires local Valhalla instance |

---

## 13. Implementation Sequence

| Phase | Task | Depends On |
|-------|------|------------|
| **a** | Build RadialMenu component (general-purpose, no actions wired) | — |
| **b** | Wire "What's here" action to validate trigger + reverse-geocode flow | a |
| **c** | Refactor SearchBar to single-mode (search-only, remove pending* logic) | — |
| **d** | Build LocationInput component (reusable) | c |
| **e** | Build DirectionsPanel layout with two LocationInputs | d |
| **f** | Wire remaining radial actions to directions flow | b, e |
| **g** | Wire place panel "Directions" handoff to new flow | e |
| **h** | Add SwapButton | e |
| **i** | Add map-click-to-fill-active-input | e |
| **j** | Mobile polish (long-press timing, bottom sheet, keyboard) | a-i |

**Estimated phases:** 10 discrete tasks, can be done incrementally.

---

## 14. Open Questions

### For Matt to decide:

1. **Bottom sheet vs side panel on mobile?**
   - Bottom sheet recommended but adds complexity

2. **Long-press timing exactly?**
   - 400ms / 450ms / 500ms
   - Recommend 450ms

3. **Should "Save place" wedge be visible to guests or hidden?**
   - Visible with login prompt = more discoverable
   - Hidden = cleaner for guests
   - Recommend: visible, shows "Sign in to save" toast

4. **Inner ring of secondary actions in radial v2?**
   - Could add less-common actions in inner ring
   - Recommend: stay single-ring for v1, evaluate need later

5. **What does "Drop pin" persistence look like?**
   - Session only (lost on refresh)
   - localStorage (persists locally)
   - Auth-only saved (sync across devices)
   - Recommend: session-only for v1, localStorage for v2

6. **Radial on map click during active input?**
   - Option A: No radial, click fills input directly
   - Option B: Radial appears, "Use this location" wedge fills input
   - Recommend: Option A (direct fill) for simplicity

---

## Appendix A: Current Code References

| File | Lines | Relevance |
|------|-------|-----------|
| `store.js` | 72-86 | `startDirections()` logic to replace |
| `store.js` | 16-34 | `stops[]` management to preserve |
| `SearchBar.jsx` | 140-170 | `pendingDestination` logic to remove |
| `PlaceDetail.jsx` | 574-579 | `handleDirections()` to rewrite |
| `App.jsx` | 31-66 | Route fetch effect to preserve |
| `api.js` | 29-56 | `requestRoute()` unchanged |

---

## Appendix B: Radial Menu SVG Structure

```svg
<svg viewBox="0 0 200 200">
  <!-- Wedge paths -->
  <g class="wedges">
    <path d="M100,100 L100,20 A80,80 0 0,1 169,60 Z" class="wedge" data-action="drop-pin" />
    <path d="M100,100 L169,60 A80,80 0 0,1 169,140 Z" class="wedge" data-action="to-here" />
    <!-- ... 4 more wedges ... -->
  </g>

  <!-- Center disc -->
  <circle cx="100" cy="100" r="40" class="center-disc" />
  <text x="100" y="95" class="coords">43.6166</text>
  <text x="100" y="110" class="coords">-116.2008</text>
  <text x="100" y="125" class="label">Loading...</text>

  <!-- Icons (positioned in wedge centers) -->
  <g class="icons">
    <use href="#pin-icon" x="100" y="40" />
    <!-- ... more icons ... -->
  </g>
</svg>
```

---

*Document created 2026-04-26. Implementation to follow in dedicated session.*
