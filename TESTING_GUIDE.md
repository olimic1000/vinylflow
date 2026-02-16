# Testing Guide for Region Click and Error Message Fixes

This guide explains how to test the recent fixes to the error message and region click-to-toggle functionality.

## What Changed

### 1. Fixed Error Message
- **Location**: Track count mismatch warning
- **Before**: "click track regions to remove them"
- **After**: "click Ignore to remove unwanted tracks"

### 2. Added Region Click Functionality
- Users can now click directly on track regions in the waveform to toggle their ignored status
- Clicking a region toggles it between ignored/active states
- Visual feedback: ignored tracks appear with a red tint

## Prerequisites

Before testing, you need:
- Docker Desktop installed, OR Python 3.11+ with FFmpeg
- A Discogs API token ([get one here](https://www.discogs.com/settings/developers))
- A WAV or AIFF audio file with multiple tracks

## Setup Instructions

### Option A: Using Docker (Recommended)

```bash
# Navigate to the vinylflow directory
cd /path/to/vinylflow

# Start VinylFlow
docker compose up -d

# Open in browser
# Navigate to http://localhost:8000
```

### Option B: Manual Setup (Non-Docker)

```bash
# Navigate to the vinylflow directory
cd /path/to/vinylflow

# Create virtual environment (if not already done)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start the server
python -m uvicorn backend.api:app --host 0.0.0.0 --port 8000

# Open in browser
# Navigate to http://localhost:8000
```

## Testing Instructions

### Test 1: Verify Error Message Text

**Purpose**: Confirm the error message now correctly instructs users to use the Ignore button

**Steps**:
1. Start VinylFlow and complete the initial setup with your Discogs token
2. Upload a WAV or AIFF file with multiple tracks
3. Click "Analyze" to detect tracks
4. Search for and select a release from Discogs that has a **different number of tracks** than detected
   - For example: if 5 tracks were detected, select a release with 4 or 6 tracks
5. Look for the orange/yellow warning box that says "Track Count Mismatch"

**Expected Result**:
- The warning message should say: "💡 Scroll up to the waveform and right-click to add splits, or click Ignore to remove unwanted tracks."
- The old text "click track regions to remove them" should NOT appear

**Screenshot Location**: The warning appears below the waveform, above the release information

---

### Test 2: Click to Toggle Track Regions

**Purpose**: Verify that clicking on track regions in the waveform toggles their ignored status

**Steps**:
1. Complete Test 1 steps 1-4 to have a file analyzed with visible track regions
2. Identify a track region in the waveform (colored bands labeled "Track 1", "Track 2", etc.)
3. **Click directly on a track region** (not on the resize handles at the edges)
4. Observe the visual changes

**Expected Results**:
- ✅ The clicked region should change color to a red tint (indicating it's now ignored)
- ✅ In the track list below, the corresponding track's "Ignore" checkbox should become checked
- ✅ The track count should decrease by 1
- ✅ Click the same region again - it should return to its original color (blue/green/orange/purple)
- ✅ The "Ignore" checkbox should become unchecked
- ✅ The track count should increase by 1

**Screenshot Location**: Waveform area at the top of the page, track list below

---

### Test 3: Verify Consistency Between Methods

**Purpose**: Confirm both methods (checkbox and region click) work identically

**Steps**:
1. Click the "Ignore" checkbox for Track 1 in the track list
2. Observe the waveform region for Track 1
3. Click the "Ignore" checkbox again to un-ignore
4. Now click directly on the Track 2 region in the waveform
5. Check the Track 2 "Ignore" checkbox in the track list
6. Click the Track 2 region again to un-ignore

**Expected Results**:
- ✅ Both methods produce the same visual result
- ✅ The waveform region color changes match (red tint when ignored)
- ✅ The checkbox state matches the region's state
- ✅ No errors appear in the browser console (press F12 to check)

---

### Test 4: Verify Drag/Resize Still Works

**Purpose**: Ensure the new click handler doesn't interfere with existing drag/resize functionality

**Steps**:
1. With tracks visible in the waveform, hover over the **edge** of a track region
2. You should see resize handles (small vertical lines at the start/end of each region)
3. Click and drag a resize handle to adjust the track boundary
4. Click and drag the **middle** of a track region to move it

**Expected Results**:
- ✅ Dragging resize handles changes track boundaries WITHOUT toggling ignore status
- ✅ Dragging the middle of a region moves it WITHOUT toggling ignore status
- ✅ Only clicking on the region body (not handles) should toggle ignore status

---

## Troubleshooting

### Region clicks aren't working
- **Check browser console** (F12 → Console tab) for JavaScript errors
- Ensure you're clicking on the region itself, not the resize handles at the edges
- Try refreshing the page and re-analyzing the file

### Error message still shows old text
- **Hard refresh** the browser (Ctrl+Shift+R on Windows/Linux, Cmd+Shift+R on Mac)
- Clear browser cache
- If using Docker, restart the container: `docker compose restart`

### Waveform not displaying
- Ensure the uploaded file is a valid WAV or AIFF file
- Check that analysis completed successfully
- Verify no errors in the browser console

---

## What to Look For in Tests

### Visual Indicators:
- **Active track regions**: Blue, green, orange, purple, or pink tint
- **Ignored track regions**: Red tint with reduced opacity
- **Track count**: Decreases when tracks are ignored
- **Checkbox state**: Matches the region's ignored status

### Functional Indicators:
- Clicks on region body toggle ignore status
- Clicks on resize handles do NOT toggle (only resize)
- Both methods (checkbox and region click) produce identical results
- No JavaScript errors in console

---

## Quick Reference

| Action | Expected Behavior |
|--------|------------------|
| Click track region | Toggle ignored status, change color |
| Click Ignore checkbox | Toggle ignored status, change region color |
| Drag resize handle | Adjust track boundary, NO ignore toggle |
| Drag region middle | Move region, NO ignore toggle |
| Click region when ignored | Un-ignore (return to original color) |

---

## Reporting Issues

If you find any problems during testing:
1. Note which test failed
2. Take a screenshot showing the issue
3. Check browser console for errors (F12 → Console)
4. Report with the error details and steps to reproduce
