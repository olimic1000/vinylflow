# Quick Testing Guide - Error Message & Region Click Fixes

## TL;DR - How to Test

### 🚀 Quick Start

```bash
# 1. Start VinylFlow
docker compose up -d
# OR
python -m uvicorn backend.api:app --host 0.0.0.0 --port 8000

# 2. Open browser
# Go to: http://localhost:8000

# 3. Upload a multi-track audio file (WAV or AIFF)

# 4. Analyze and search for Discogs release
```

---

## ✅ Test Checklist

### Test 1: Error Message Fix
- [ ] Upload and analyze an audio file
- [ ] Select a Discogs release with different track count
- [ ] Look for the "Track Count Mismatch" warning
- [ ] **VERIFY**: Message says "click Ignore to remove unwanted tracks"
- [ ] **VERIFY**: Message does NOT say "click track regions to remove them"

**Where to look**: Orange warning box below the waveform

---

### Test 2: Click Region to Toggle

- [ ] See track regions displayed in the waveform (colored bands)
- [ ] **Click directly on a track region** (e.g., Track 1)
- [ ] **VERIFY**: Region turns red (ignored state)
- [ ] **VERIFY**: "Ignore" checkbox becomes checked in track list
- [ ] **VERIFY**: Track count decreases by 1
- [ ] Click the same region again
- [ ] **VERIFY**: Region returns to original color (active state)
- [ ] **VERIFY**: "Ignore" checkbox becomes unchecked
- [ ] **VERIFY**: Track count increases by 1

**Where to look**: Waveform at top of page, track list below

---

### Test 3: Both Methods Work Identically

- [ ] Use "Ignore" checkbox to ignore Track 1
- [ ] **VERIFY**: Waveform region for Track 1 turns red
- [ ] Un-check "Ignore" checkbox
- [ ] **VERIFY**: Waveform region returns to original color
- [ ] Click Track 2's region in waveform
- [ ] **VERIFY**: Track 2's "Ignore" checkbox becomes checked
- [ ] Click Track 2's region again
- [ ] **VERIFY**: Track 2's "Ignore" checkbox becomes unchecked

---

### Test 4: Drag/Resize Still Works

- [ ] Hover over the **edge** of a track region
- [ ] Drag the resize handle left or right
- [ ] **VERIFY**: Track boundary moves WITHOUT toggling ignore
- [ ] Click and drag the **middle** of a region
- [ ] **VERIFY**: Region moves WITHOUT toggling ignore

---

## 🎯 What Success Looks Like

| Feature | Expected Behavior |
|---------|------------------|
| Error message | Says "click Ignore to remove unwanted tracks" |
| Click region | Toggles between ignored (red) and active (colored) |
| Checkbox method | Same result as clicking region |
| Drag handles | Does NOT toggle ignore status |
| Visual feedback | Ignored = red tint, Active = blue/green/orange/purple |

---

## 🐛 Common Issues

**"I don't see track regions"**
- Make sure you analyzed the file first (click "Analyze")
- Check that the file is a valid WAV or AIFF
- Scroll to the waveform section at the top

**"Clicks aren't working"**
- Click on the region body, not the resize handles at edges
- Hard refresh browser (Ctrl+Shift+R / Cmd+Shift+R)
- Check browser console for errors (F12 → Console)

**"Old error message still shows"**
- Hard refresh browser to clear cache
- If using Docker: `docker compose restart`

---

## 📸 Visual Reference

### What You're Looking For:

1. **Track Regions in Waveform**
   - Colored horizontal bands labeled "Track 1", "Track 2", etc.
   - Different colors for each track

2. **Ignored Track** (after clicking region or checkbox)
   - Red tint overlay on the region
   - Checkbox is checked
   - Track count decreases

3. **Active Track** (default or after un-ignoring)
   - Blue, green, orange, purple, or pink color
   - Checkbox is unchecked
   - Included in track count

4. **Error Message Location**
   - Orange/yellow warning box
   - Below waveform, above release info
   - Only appears when track counts don't match

---

## 💡 Pro Tips

- **Use Chrome DevTools**: Press F12 and go to Console tab to see any errors
- **Test with real data**: Use an actual vinyl recording with multiple tracks
- **Try different scenarios**: Ignore multiple tracks, un-ignore all, ignore every other track
- **Verify consistency**: Whatever you do with checkbox should match what happens with region clicks

---

## 📝 Code Changes Reference

If you want to see what actually changed:

```bash
git diff HEAD~2 HEAD backend/static/index.html
git diff HEAD~2 HEAD backend/static/app.js
```

**Files modified:**
- `backend/static/index.html` - Line 568 (error message text)
- `backend/static/app.js` - Lines 842-856 (region click handler)

---

## Need Help?

If something doesn't work as expected:
1. Take a screenshot
2. Check browser console (F12)
3. Note which test step failed
4. Share the details for debugging
