// Review of the touching-cut slice: the "Use m:ss" toast action must not hit another track or recording,
// applyTrackBounds never leaves the recording or moves a short track against the drag, a whole-region
// drag keeps its length at the left edge, and the mutants that survived the first suite.
// Toasts are the real ones (runToastAction), regions are fakes that record setOptions.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const root = path.join(__dirname, '..', '..', 'backend', 'static');
const vinylApp = new Function(fs.readFileSync(path.join(root, 'app.js'), 'utf8') + ';return vinylApp;')();
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const trk = (n, s, e) => ({ number: n, start: s, end: e, duration: e - s, ignored: false, editing: false });
const cuts = a => a.detectedTracks.map(t => [t.start, t.end]);
function fakeRegions(app) {
    const list = app.detectedTracks.map(t => ({ id: `track-${t.number}`, start: t.start, end: t.end,
        setOptions(change) { Object.assign(this, change); } }));
    return { list, getRegions: () => list };
}
function app(tracks, recordingLength = 1000) {
    const a = vinylApp();
    a.detectedTracks = tracks;
    a.playDuration = recordingLength;
    a.currentFileId = 'f';
    a.uploadedFiles = [{ id: 'f', filename: 'a.wav', status: 'analyzed' }];
    a.syncRegionsAround = () => {};   // the real one is exercised where regions are faked
    return a;
}
const useAction = a => {
    const toast = a.toasts.find(t => t.action);
    assert.ok(toast, 'the toast offers an action');
    a.runToastAction(toast.id);
};

// ---- 1. the toast action applies to the track the user typed in, or does nothing ----
let a = app([trk(1, 10, 100), trk(2, 100, 470), trk(3, 470, 900)], 900);
a.setTrackLength(1, '3.45', null);
a.detectedTracks.splice(0, 1);   // track 1 deleted meanwhile: the typed track is now row 0
useAction(a);
assert.deepStrictEqual(cuts(a), [[100, 325], [325, 900]], 'the typed track, not the one now at its old index');

a = app([trk(1, 10, 100), trk(2, 100, 470), trk(3, 470, 900)], 900);
a.setTrackLength(1, '3.45', null);
a.detectedTracks.splice(1, 1);   // the typed track itself is gone
useAction(a);
assert.deepStrictEqual(cuts(a), [[10, 100], [470, 900]], 'a deleted track is not edited');

a = app([trk(1, 10, 100), trk(2, 100, 470)], 900);
a.setTrackLength(0, '1.50', null);   // would make track 1 110 s
a.currentFileId = 'g';   // another recording is open now
useAction(a);
assert.deepStrictEqual(cuts(a), [[10, 100], [100, 470]], 'a file switch drops the action');

a = app([trk(1, 10, 100), trk(2, 100, 470)], 900);
a.setTrackLength(0, '1.50', null);
a.isProcessing = true; a.uploadedFiles[0].status = 'processing';   // an export started
useAction(a);
assert.deepStrictEqual(cuts(a), [[10, 100], [100, 470]], 'a locked recording is not edited');
// the typed edits themselves are refused while locked
assert.strictEqual(a.setTrackLength(0, '2:00', null), false);
assert.strictEqual(a.updateTrackEnd(0, '50', { value: '' }), false);
assert.strictEqual(a.updateTrackStart(1, '120', { value: '' }), false);
assert.deepStrictEqual(cuts(a), [[10, 100], [100, 470]]);
// the still-valid case works through the real toast
a = app([trk(1, 10, 100), trk(2, 100, 470)], 900);
a.setTrackLength(0, '1.30', null); useAction(a);
assert.deepStrictEqual(cuts(a), [[10, 100], [100, 470]], '1:30 = 90 s from 10 = 100: already that long');
a = app([trk(1, 10, 100), trk(2, 100, 470)], 900);
a.setTrackLength(0, '2.00', null); useAction(a);
assert.deepStrictEqual(cuts(a), [[10, 130], [130, 470]]);

// the offer lapses when the track's length was changed since the toast
a = app([trk(1, 10, 100), trk(2, 100, 470)], 900);
a.setTrackLength(0, '3.45', null);
a.setTrackLength(0, '2:00', null);   // retyped meanwhile: 10-130
useAction(a);
assert.deepStrictEqual(cuts(a), [[10, 130], [130, 470]], 'a stale offer does nothing');

// ---- 2. a track already shorter than MIN never leaves the recording or moves against the drag ----
a = app([trk(1, 0, 99.5), trk(2, 99.5, 100)], 100);
assert.strictEqual(a.applyTrackBounds(1, 99.5, 99.8), false, 'end past the recording is refused');
assert.deepStrictEqual(cuts(a), [[0, 99.5], [99.5, 100]]);
a = app([trk(1, 0, 99.2), trk(2, 99.2, 99.5)], 100);
assert.strictEqual(a.applyTrackBounds(1, 99.2, 99.8), false, 'the 1 s floor would pass the recording end');
assert.deepStrictEqual(cuts(a), [[0, 99.2], [99.2, 99.5]]);
a = app([trk(1, 0, 0.5), trk(2, 0.5, 100)]);
assert.strictEqual(a.applyTrackBounds(0, 0, 0.3), false, 'a short track does not grow when its end is dragged left');
assert.deepStrictEqual(cuts(a), [[0, 0.5], [0.5, 100]]);
a = app([trk(1, 0, 100), trk(2, 100, 100.5)]);
assert.strictEqual(a.applyTrackBounds(1, 100.3, 100.5), false, 'a short track does not grow when its start is dragged right');
assert.deepStrictEqual(cuts(a), [[0, 100], [100, 100.5]]);
a = app([trk(1, 0, 100), trk(2, 100, 100.5)], 100.5);
assert.strictEqual(a.applyTrackBounds(1, 99, 100.5), true, 'a drag that makes it longer still works');
assert.deepStrictEqual(cuts(a), [[0, 99], [99, 100.5]]);

// ---- 3. a whole-region move left of 0 shifts, it does not shrink ----
a = app([trk(1, 10, 110), trk(2, 110, 200)]);
assert.ok(a.applyTrackBounds(0, -5, 95));
assert.deepStrictEqual(cuts(a), [[0, 100], [100, 200]], 'length kept (100 s), shifted to 0');
a = app([trk(1, 0, 100)]);
a.applyTrackBounds(0, -5, 100.5);   // start -5, length 105.5 -> shifted to 0..105.5
assert.deepStrictEqual(cuts(a), [[0, 105.5]]);

// ---- 4. surviving mutants ----
// touching tolerance: 0.04 s follows, 0.06 s stays a gap
a = app([trk(1, 0, 100), trk(2, 100.04, 200)]);
a.applyTrackBounds(0, 0, 90);
assert.deepStrictEqual(cuts(a), [[0, 90], [90, 200]], '0.04 s apart is touching');
a = app([trk(1, 0, 100), trk(2, 100.06, 200)]);
a.applyTrackBounds(0, 0, 90);
assert.deepStrictEqual(cuts(a), [[0, 90], [100.06, 200]], '0.06 s apart is a real gap');
// a start-only edit leaves a touching NEXT neighbour alone; an end-only edit leaves a touching PREVIOUS one alone
a = app([trk(1, 0, 100), trk(2, 100.04, 200)]);
assert.ok(a.updateTrackStart(0, '10', { value: '' }));
assert.deepStrictEqual(cuts(a), [[10, 100], [100.04, 200]]);
a = app([trk(1, 0, 100), trk(2, 100.04, 200)]);
assert.ok(a.updateTrackEnd(1, '250', { value: '' }));
assert.deepStrictEqual(cuts(a), [[0, 100], [100.04, 250]]);
// an already-short PREVIOUS neighbour stops the edit at its cut (start side mirror)
a = app([trk(1, 100, 100.5), trk(2, 100.5, 200)]);
assert.strictEqual(a.applyTrackBounds(1, 90, 200), false);
assert.deepStrictEqual(cuts(a), [[100, 100.5], [100.5, 200]]);
// a start dragged into an already-short previous track stops at that track's end, even across a small gap
a = app([trk(1, 100, 100.5), trk(2, 100.9, 200)]);
assert.ok(a.applyTrackBounds(1, 100.2, 200));
assert.deepStrictEqual(cuts(a), [[100, 100.5], [100.5, 200]]);
// clamping that changes nothing is not a hand edit
a = app([trk(1, 0, 100), trk(2, 105, 105.5)]);
a.updateTrackFromRegion({ id: 'track-1', start: 0, end: 150 });
assert.deepStrictEqual(cuts(a), [[0, 105], [105, 105.5]]);
a = app([trk(1, 0, 105), trk(2, 105, 105.5)]);
a.updateTrackFromRegion({ id: 'track-1', start: 0, end: 150 });
assert.notStrictEqual(a.boundariesEdited, true, 'nothing changed, nothing marked');
// a typed Length keeps the waveform regions in step (not stubbed here)
a = app([trk(1, 10, 400), trk(2, 400, 600)]);
a.syncRegionsAround = vinylApp().syncRegionsAround;
a.syncRegion = vinylApp().syncRegion;
a.waveformRegions = fakeRegions(a);
assert.ok(a.setTrackLength(0, '3:45', { value: '' }));
assert.deepStrictEqual(cuts(a), [[10, 235], [235, 600]], 'a shortened track pulls its touching next one along');
assert.deepStrictEqual(a.waveformRegions.list.map(r => [r.start, r.end]), [[10, 235], [235, 600]], 'regions follow');
// ... and the offered 3.45 shortens too
a = app([trk(1, 10, 400), trk(2, 400, 600)]);
a.setTrackLength(0, '3.45', null); useAction(a);
assert.deepStrictEqual(cuts(a), [[10, 235], [235, 600]]);

// ---- the Length field is text: a decimal keypad has no ':' ----
const lengthInput = html.match(/<input type="text" :value="formatDuration\(track\.duration\)"[^>]*>/)[0];
assert.ok(!/inputmode="decimal"/.test(lengthInput), 'no decimal keypad on the Length input');
assert.ok(!/inputmode="decimal"/.test(html.slice(html.indexOf('track-grid text-sm'), html.indexOf('track.editing" x-cloak'))));

console.log('bounds_review ok');
process.exit(0);   // real toasts hold timers
