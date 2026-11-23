// Touching cuts move together (applyTrackBounds, typed End/Start, region drag) and `3.45` in Length is refused.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + ';return vinylApp;')();
const trk = (n, s, e) => ({ number: n, start: s, end: e, duration: e - s, ignored: false, editing: false });
function app(tracks, recordingLength = 1000) {
    const a = vinylApp();
    a.detectedTracks = tracks;
    a.playDuration = recordingLength;
    a.toasts = []; a.notify = (m, o = {}) => { a.toasts.push({ m, ...o }); return a.toasts.length; };
    a.syncRegionsAround = () => {};
    return a;
}
const cuts = a => a.detectedTracks.map(t => [t.start, t.end]);
// What /api/process would receive: the boundaries are the detected tracks' start/end
const sent = a => cuts(a);

// Maintainer's case: 2-200, 200-400, 400-600; End of track 1 typed as 190 -> track 2 starts at 190
let a = app([trk(1, 2, 200), trk(2, 200, 400), trk(3, 400, 600)]);
assert.ok(a.updateTrackEnd(0, '190', { value: '' }));
assert.deepStrictEqual(sent(a), [[2, 190], [190, 400], [400, 600]]);
assert.strictEqual(a.detectedTracks[1].duration, 210);

// Same through the region path (drag the end left)
a = app([trk(1, 2, 200), trk(2, 200, 400), trk(3, 400, 600)]);
a.syncRegionsAround = () => {};
a.updateTrackFromRegion({ id: 'track-1', start: 2, end: 190 });
assert.deepStrictEqual(sent(a), [[2, 190], [190, 400], [400, 600]]);

// Dragging the end right moves the touching neighbour's start too
a = app([trk(1, 2, 200), trk(2, 200, 400)]);
a.updateTrackFromRegion({ id: 'track-1', start: 2, end: 210 });
assert.deepStrictEqual(sent(a), [[2, 210], [210, 400]]);

// Start moved right with a touching previous track: prev.end follows
a = app([trk(1, 2, 200), trk(2, 200, 400)]);
assert.ok(a.updateTrackStart(1, '210', { value: '' }));
assert.deepStrictEqual(sent(a), [[2, 210], [210, 400]]);
a = app([trk(1, 2, 200), trk(2, 200, 400)]);
a.updateTrackFromRegion({ id: 'track-2', start: 210, end: 400 });
assert.deepStrictEqual(sent(a), [[2, 210], [210, 400]]);

// A pre-existing 5 s gap stays a gap when the end moves left (and is not closed)
a = app([trk(1, 2, 200), trk(2, 205, 400)]);
assert.ok(a.updateTrackEnd(0, '190', { value: '' }));
assert.deepStrictEqual(sent(a), [[2, 190], [205, 400]]);
// ... and the start of the next track moving right leaves the gap bigger
a = app([trk(1, 2, 200), trk(2, 205, 400)]);
assert.ok(a.updateTrackStart(1, '215', { value: '' }));
assert.deepStrictEqual(sent(a), [[2, 200], [215, 400]]);

// Whole-region drag with touching neighbours on both sides
a = app([trk(1, 0, 100), trk(2, 100, 200), trk(3, 200, 300)]);
a.updateTrackFromRegion({ id: 'track-2', start: 90, end: 190 });
assert.deepStrictEqual(sent(a), [[0, 90], [90, 190], [190, 300]]);
a = app([trk(1, 0, 100), trk(2, 100, 200), trk(3, 200, 300)]);
a.updateTrackFromRegion({ id: 'track-2', start: 110, end: 210 });
assert.deepStrictEqual(sent(a), [[0, 110], [110, 210], [210, 300]]);

// Whole-region drag past the recording's end shifts the region (keeps its length)
a = app([trk(1, 0, 100), trk(2, 100, 200)], 250);
a.updateTrackFromRegion({ id: 'track-2', start: 180, end: 280 });
assert.deepStrictEqual(sent(a), [[0, 150], [150, 250]]);

// A neighbour already under MIN_TRACK_LENGTH: the edit stops at its cut instead of jumping back
a = app([trk(1, 0, 100), trk(2, 105, 105.5)]);
a.updateTrackFromRegion({ id: 'track-1', start: 0, end: 150 });
assert.deepStrictEqual(sent(a), [[0, 105], [105, 105.5]]);

// A follower never drops under MIN: end right limited by next.end - MIN
a = app([trk(1, 0, 100), trk(2, 100, 200)]);
a.updateTrackFromRegion({ id: 'track-1', start: 0, end: 300 });
assert.deepStrictEqual(sent(a), [[0, 199], [199, 200]]);
assert.strictEqual(a.boundariesEdited, true);
a = app([trk(1, 0, 100), trk(2, 100, 200)]);
assert.strictEqual(a.applyTrackBounds(0, 0, 100), false);
assert.notStrictEqual(a.boundariesEdited, true);

// --- Length: m.ss-shaped input ---
a = app([trk(1, 10, 235), trk(2, 235, 470)]);
let field = { value: '3.45' };
assert.strictEqual(a.setTrackLength(0, '3.45', field), false);
assert.deepStrictEqual(sent(a), [[10, 235], [235, 470]]);
assert.strictEqual(field.value, '3:45');
const toast = a.toasts.at(-1);
assert.strictEqual(toast.type, 'warning');
assert.strictEqual(toast.m, 'Type 3:45 for minutes and seconds.');
assert.strictEqual(toast.action.label, 'Use 3:45');
a = app([trk(1, 10, 100), trk(2, 100, 470)]);
a.setTrackLength(0, '3.45', { value: '' });
a.toasts.at(-1).action.fn();
assert.deepStrictEqual(sent(a), [[10, 235], [235, 470]]);
a = app([trk(1, 10, 100), trk(2, 100, 470)]);
assert.ok(a.setTrackLength(0, '225'));
assert.strictEqual(a.detectedTracks[0].end, 235);
a = app([trk(1, 10, 100), trk(2, 100, 470)]);
assert.ok(a.setTrackLength(0, '19.5'));   // decimal seconds ("9.5" is refused as a likely m.s slip)
assert.strictEqual(a.detectedTracks[0].end, 29.5);
a = app([trk(1, 10, 100), trk(2, 100, 470)]);
assert.ok(a.setTrackLength(0, '225.5'));
console.log('touching cuts ok');
