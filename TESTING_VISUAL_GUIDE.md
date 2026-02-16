# Visual Testing Reference - What Changed

## Change #1: Error Message Text

### Before:
```
⚠️ Track Count Mismatch
Detected 5 tracks, but Discogs shows 4 tracks.
💡 Scroll up to the waveform and right-click to add splits, 
   or click track regions to remove them.  ❌ WRONG!
```

### After:
```
⚠️ Track Count Mismatch
Detected 5 tracks, but Discogs shows 4 tracks.
💡 Scroll up to the waveform and right-click to add splits, 
   or click Ignore to remove unwanted tracks.  ✅ CORRECT!
```

**Location**: Orange warning box below waveform viewer

---

## Change #2: Click Region Functionality

### New Interaction: Click to Toggle

```
┌─────────────────────────────────────────┐
│  WAVEFORM                               │
│  ┌────────┐ ┌────────┐ ┌────────┐      │
│  │Track 1 │ │Track 2 │ │Track 3 │      │
│  │ Blue   │ │ Green  │ │ Orange │      │
│  └────────┘ └────────┘ └────────┘      │
│       ↓ CLICK                           │
│  ┌────────┐ ┌────────┐ ┌────────┐      │
│  │Track 1 │ │Track 2 │ │Track 3 │      │
│  │  RED   │ │ Green  │ │ Orange │      │ ← Turns red!
│  │IGNORED │ └────────┘ └────────┘      │
│  └────────┘                             │
└─────────────────────────────────────────┘

TRACK LIST:
☑ Ignore  Track 1                         ← Checkbox auto-checked!
☐ Ignore  Track 2
☐ Ignore  Track 3

Active Tracks: 2 (was 3)                  ← Count updates!
```

---

## How to Test Each Feature

### Test A: Verify Error Message

```
STEPS:
1. Upload audio → Analyze
2. Search Discogs
3. Select release with DIFFERENT track count
4. Look at warning message

EXPECTED:
✅ "click Ignore to remove unwanted tracks"
❌ NOT "click track regions to remove them"
```

### Test B: Click Region to Ignore

```
STEPS:
1. See colored track regions in waveform
2. Click on Track 1 region (the colored band)
3. Watch what happens

EXPECTED:
✅ Region turns RED
✅ "Ignore" checkbox becomes CHECKED
✅ Track count DECREASES
```

### Test C: Click Again to Un-Ignore

```
STEPS:
1. Click the same (now red) region again
2. Watch what happens

EXPECTED:
✅ Region returns to ORIGINAL COLOR (blue/green/etc)
✅ "Ignore" checkbox becomes UNCHECKED
✅ Track count INCREASES
```

### Test D: Checkbox vs Region Click

```
BOTH METHODS SHOULD PRODUCE IDENTICAL RESULTS:

Method 1 (Checkbox):          Method 2 (Region Click):
☑ Check "Ignore"       =      Click region
→ Region turns red            → Region turns red
→ Count decreases             → Count decreases

☐ Uncheck "Ignore"     =      Click region again
→ Region original color       → Region original color
→ Count increases             → Count increases
```

---

## State Transitions

```
┌──────────────┐
│ ACTIVE TRACK │
│ Blue/Green/  │
│ Orange/etc   │
│ ☐ Ignore     │
└──────────────┘
       ↓ Click region OR check box
       ↓
┌──────────────┐
│IGNORED TRACK │
│   Red Tint   │
│ ☑ Ignore     │
└──────────────┘
       ↓ Click region OR uncheck box
       ↓
┌──────────────┐
│ ACTIVE TRACK │
│ Blue/Green/  │
│ Orange/etc   │
│ ☐ Ignore     │
└──────────────┘
```

---

## Important: Drag vs Click

### ✅ Clicking Region Body = Toggle Ignore
```
  ┌─────────────────┐
  │                 │ ← Click here
  │    Track 1      │ ← Or here
  │                 │ ← Or here
  └─────────────────┘
  
  RESULT: Toggle ignore status
```

### ✅ Dragging Handles = Resize (NO Toggle)
```
  ┃                 ┃
  ┃← Drag here      ┃← Or here
  ┃                 ┃
  
  RESULT: Resize boundary (ignore status unchanged)
```

---

## Browser Console Check

Open DevTools (F12) → Console tab

### ✅ Good (no errors):
```
WebSocket connected
Waveform ready
```

### ❌ Bad (has errors):
```
TypeError: Cannot read property 'id' of undefined
ReferenceError: toggleTrackIgnored is not defined
```

If you see errors, try:
- Hard refresh (Ctrl+Shift+R / Cmd+Shift+R)
- Clear cache
- Restart server

---

## Summary Checklist

When testing is successful, you should observe:

- [x] Error message uses correct text
- [x] Clicking region toggles ignore status
- [x] Region color changes (red = ignored, color = active)
- [x] Checkbox state matches region state
- [x] Track count updates correctly
- [x] Drag/resize handles don't trigger toggle
- [x] No JavaScript errors in console
- [x] Both methods (checkbox & region click) work identically

---

## Files Changed

```
backend/static/index.html  (1 line)
backend/static/app.js      (16 lines)
```

**Total impact**: Minimal, surgical changes to fix UX issue
