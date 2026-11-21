// Typing a track length (m:ss) moves its end to start + length; a touching next track follows.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + ';return vinylApp;')();
const trk = (n, s, e) => ({ number: n, start: s, end: e, duration: e - s, ignored: false, editing: false });
function app(tracks) {
    const a = vinylApp();
    a.detectedTracks = tracks;
    a.toasts = []; a.notify = (m, o = {}) => { a.toasts.push({ m, ...o }); return a.toasts.length; };
    a.syncRegionsAround = () => {};
    return a;
}
const a0 = vinylApp();
assert.strictEqual(a0.parseLengthInput('3:45'), 225);
assert.strictEqual(a0.parseLengthInput(' 3:45.5 '), 225.5);
assert.strictEqual(a0.parseLengthInput('1:02:03'), 3723);
assert.strictEqual(a0.parseLengthInput('245'), 245);
assert.strictEqual(a0.parseLengthInput('12.5'), 12.5);
for (const bad of ['', 'abc', '3:75', '3:', ':45', '1:2:3:4']) assert.strictEqual(a0.parseLengthInput(bad), null, bad);

// Contiguous cuts (the usual case): shortening track 1 moves track 2's start with it
let a = app([trk(1, 10, 300), trk(2, 300, 600), trk(3, 600, 900)]);
assert.ok(a.setTrackLength(0, '4:00'));
assert.deepStrictEqual([a.detectedTracks[0].end, a.detectedTracks[1].start], [250, 250]);
assert.strictEqual(a.detectedTracks[1].duration, 350);
assert.strictEqual(a.boundariesEdited, true);
// Lengthening pushes the shared cut forward too
assert.ok(a.setTrackLength(0, '5:20'));
assert.deepStrictEqual([a.detectedTracks[0].end, a.detectedTracks[1].start], [330, 330]);

// A gap stays a gap when shortening (that silence was dropped on purpose)
a = app([trk(1, 10, 300), trk(2, 304, 600)]);
a.setTrackLength(0, '4:00');
assert.deepStrictEqual([a.detectedTracks[0].end, a.detectedTracks[1].start], [250, 304]);

// Too long for the space before the next track's end ("10:00" meant "1:00"): refused, not clamped; the next
// track must not shrink to a second.  (This used to assert the clamp: the typo collapsed track 2.)
a = app([trk(1, 10, 100), trk(2, 100, 200)]);
let lengthInput = { value: '10:00' };
assert.strictEqual(a.setTrackLength(0, '10:00', lengthInput), false);
assert.deepStrictEqual(a.detectedTracks.map(t => [t.start, t.end]), [[10, 100], [100, 200]]);
assert.strictEqual(a.boundariesEdited, false);
assert.strictEqual(a.toasts.at(-1).type, 'warning'); assert.match(a.toasts.at(-1).m, /track 2/i);
assert.strictEqual(lengthInput.value, '1:30', 'the field shows the current length');
// ... but a length that still leaves the next track a second or more is fine
a = app([trk(1, 10, 100), trk(2, 100, 200)]);
assert.ok(a.setTrackLength(0, '3:00'));
assert.deepStrictEqual([a.detectedTracks[0].end, a.detectedTracks[1].start], [190, 190]);

// The last track grows up to the end of the recording, never past it
a = app([trk(1, 10, 300)]); a.playDuration = 340;
assert.ok(a.setTrackLength(0, '6:00'));
assert.strictEqual(a.detectedTracks[0].end, 340); assert.match(a.toasts.at(-1).m, /end of the recording/);
a = app([trk(1, 10, 300)]); a.playDuration = 400;
assert.ok(a.setTrackLength(0, '6:00')); assert.strictEqual(a.detectedTracks[0].end, 370);

// Unreadable input: nothing changes, the field shows the length again, a hint appears
a = app([trk(1, 10, 300), trk(2, 300, 600)]);
const input = { value: 'abc' };
assert.strictEqual(a.setTrackLength(0, 'abc', input), false);
assert.strictEqual(input.value, '4:50'); assert.strictEqual(a.detectedTracks[0].end, 300);
assert.match(a.toasts.at(-1).m, /3:45/);
// While the export of this recording runs the length input is disabled in the markup; the function itself
// stays a plain edit (like dragging), so nothing here depends on the exporting state.
console.log('track length ok');
