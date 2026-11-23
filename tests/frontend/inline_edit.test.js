// Hand edits of the cuts from the track table: inline Start/End fields, and the row's Discogs mapping
// helpers (mapping state is customMapping: indices into selectedRelease.tracks).
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

const track = (number, start, end) => ({ number, start, end, duration: end - start, ignored: false, editing: false });
function mkApp(tracks) {
    const app = vinylApp();
    app.detectedTracks = tracks;
    app.toasts = []; app.notify = (message, options = {}) => { app.toasts.push({ message, ...options }); return app.toasts.length; };
    app.waveformRegions = {
        regions: [],
        getRegions() { return this.regions; },
    };
    // One fake region per track, as addTrackRegions makes them; setOptions records what was pushed
    app.waveformRegions.regions = tracks.map(t => {
        const region = { id: 'track-' + t.number, start: t.start, end: t.end, pushed: 0, setOptions(o) { Object.assign(region, o); region.pushed++; } };
        return region;
    });
    return app;
}
const ends = app => app.detectedTracks.map(t => [t.start, t.end]);

// ---- Start field: moves the cut, keeps the neighbours' gaps, marks the hand edit ----
let app = mkApp([track(1, 10, 100), track(2, 100, 200)]);
app.updateTrackStart(1, '104');
// These cuts touch, so the previous end follows (a stretch in no file otherwise); a real gap
// stays a gap, see touching_cuts.test.js.
assert.deepStrictEqual(ends(app), [[10, 104], [104, 200]], 'touching cuts move together');
assert.strictEqual(app.detectedTracks[1].duration, 96);
assert.strictEqual(app.boundariesEdited, true);
assert.strictEqual(app.waveformRegions.regions[1].start, 104, 'the region follows the field');
app.updateTrackStart(1, '95');
assert.deepStrictEqual(ends(app), [[10, 95], [95, 200]], 'overlapping the previous track pushes its end back');
assert.strictEqual(app.waveformRegions.regions[0].end, 95);

// ---- End field ----
app = mkApp([track(1, 10, 100), track(2, 100, 200)]);
app.updateTrackEnd(0, '120');
assert.deepStrictEqual(ends(app), [[10, 120], [120, 200]], 'the next track starts where this one ends when they overlap');
app.updateTrackEnd(0, '90');
assert.deepStrictEqual(ends(app), [[10, 90], [90, 200]], 'touching cuts move together when shortening');
// A real gap that already existed stays a gap
app = mkApp([track(1, 10, 100), track(2, 105, 200)]);
app.updateTrackEnd(0, '90');
assert.deepStrictEqual(ends(app), [[10, 90], [105, 200]], 'shortening leaves an existing gap');

// ---- limits: every track keeps MIN_TRACK_LENGTH (1 s); nonsense changes nothing ----
app = mkApp([track(1, 10, 100), track(2, 100, 200)]);
const before = JSON.stringify(app.detectedTracks);
app.updateTrackStart(0, 'abc'); app.updateTrackEnd(0, '-5'); app.updateTrackStart(0, '');
assert.strictEqual(JSON.stringify(app.detectedTracks), before);
assert.strictEqual(app.boundariesEdited, false, 'no edit, no flag');
// A typo must not collapse a track: an end at or before the start (or a start at or after the end) is
// refused with a warning, the field shows the current value again, and nothing moves.
const snapshot = () => JSON.stringify([app.detectedTracks, app.boundariesEdited]);
const refused = (label, run, pattern, expectedFieldValue) => {
    const input = { value: 'typed' };
    const state = snapshot(); const toastsBefore = app.toasts.length;
    run(input);
    assert.strictEqual(snapshot(), state, label + ': nothing changes');
    assert.strictEqual(app.toasts.length, toastsBefore + 1, label + ': one toast');
    assert.strictEqual(app.toasts.at(-1).type, 'warning', label);
    assert.match(app.toasts.at(-1).message, pattern, label);
    assert.strictEqual(String(input.value), String(expectedFieldValue), label + ': field reset');
};
app = mkApp([track(1, 10, 100), track(2, 100, 200)]);
refused('End 5 on a track starting at 10', input => app.updateTrackEnd(0, '5', input), /after (its|the) start/i, 100);
refused('End equal to start', input => app.updateTrackEnd(0, '10', input), /after (its|the) start/i, 100);
refused('End 10 on track 2 (starts at 100)', input => app.updateTrackEnd(1, '10', input), /after (its|the) start/i, 200);
refused('Start after the end', input => app.updateTrackStart(0, '150', input), /before (its|the) end/i, 10);
refused('Start equal to the end', input => app.updateTrackStart(0, '100', input), /before (its|the) end/i, 10);
refused('End that leaves the next track under a second', input => app.updateTrackEnd(0, '199.5', input), /track 2/i, 100);
refused('Start that leaves the previous track under a second', input => app.updateTrackStart(1, '10.5', input), /track 1/i, 100);
// accepted edits also write the stored value back into the field (nothing typed stays on screen)
let input = { value: '120' };
app.updateTrackEnd(0, '120', input);
assert.strictEqual(String(input.value), '120');
// not a track / nothing typed: no change, no flag, no toast
app = mkApp([track(1, 10, 100), track(2, 100, 200)]);
app.updateTrackStart(7, '5'); app.updateTrackEnd(7, '5');
assert.strictEqual(app.boundariesEdited, false);
assert.strictEqual(app.toasts.length, 0);
assert.deepStrictEqual(ends(app), [[10, 100], [100, 200]]);

// ---- the end is capped at the recording's end (Start/End/Length/region updates share applyTrackBounds) ----
app = mkApp([track(1, 10, 100), track(2, 100, 200)]); app.playDuration = 300;
input = { value: '99999' };
app.updateTrackEnd(1, '99999', input);
assert.strictEqual(app.detectedTracks[1].end, 300, 'typed End past the recording');
assert.strictEqual(app.detectedTracks[1].duration, 200);
assert.strictEqual(String(input.value), '300', 'the field shows the capped value');
app.waveformRegions.regions[1].end = 450; app.updateTrackFromRegion(app.waveformRegions.regions[1]);
assert.strictEqual(app.detectedTracks[1].end, 300, 'a region update is capped too');
app = mkApp([track(1, 10, 100), track(2, 100, 200)]); app.currentFile = { duration: 250 };   // no waveform duration yet
app.updateTrackEnd(1, '99999');
assert.strictEqual(app.detectedTracks[1].end, 250, 'falls back to the file duration');

// ---- a region dragged in the waveform goes through the same rules; an echo is no edit ----
app = mkApp([track(1, 10, 100), track(2, 100, 200)]);
const region = app.waveformRegions.regions[0];
app.updateTrackFromRegion(region);
assert.strictEqual(app.boundariesEdited, false, 'unchanged region (colour refresh) is not a hand edit');
region.end = 150; app.updateTrackFromRegion(region);
assert.strictEqual(app.boundariesEdited, true);
assert.deepStrictEqual(ends(app), [[10, 150], [150, 200]]);
app.updateTrackFromRegion({ id: 'expected-1', start: 5, end: 5 });
assert.deepStrictEqual(ends(app), [[10, 150], [150, 200]], 'ghost markers are not tracks');
assert.strictEqual(app.detectedTracks[0].editing, false, 'a drag no longer opens the Adjust fields');

// ---- the Discogs mapping of a row ----
app = vinylApp();
app.detectedTracks = [track(1, 0, 100), track(2, 100, 230), track(3, 230, 300)];
assert.strictEqual(app.mappedTrack(0), null, 'no release: nothing to map');
assert.strictEqual(app.discogsLength(0), null);
assert.strictEqual(app.trackDelta(app.detectedTracks[0], 0), null);
assert.strictEqual(app.deltaText(null), '—');
app.selectedRelease = { id: 1, tracks: [
    { position: 'A1', title: 'One', duration: '1:40' },
    { position: 'A2', title: 'Two', duration: '2:00' },
    { position: 'B1', title: 'Three', duration: '' },
] };
app.customMapping = [2, 1, '0'];   // a number or its string (a <select> value): both are indices
assert.strictEqual(app.mappedTrack(0).position, 'B1');
assert.strictEqual(app.mappedTrack(2).position, 'A1');
assert.strictEqual(app.trackOptionLabel(app.selectedRelease.tracks[1]), 'A2 - Two');
assert.strictEqual(app.discogsLength(0), null, 'a release track without a length');
assert.strictEqual(app.discogsLength(1), 120);
assert.strictEqual(app.trackDelta(app.detectedTracks[1], 1), 10);
assert.strictEqual(app.deltaText(10), '+10s');
assert.strictEqual(app.deltaText(-2.6), '−3s');
assert.strictEqual(app.deltaColor(2), '#0B7A55');
assert.strictEqual(app.deltaColor(-8), '#8A5F00');
assert.strictEqual(app.deltaColor(30), '#B3261E');
assert.strictEqual(app.deltaColor(null), '#5E5E5A');
assert.strictEqual(app.mappedTrack(9), null);
assert.strictEqual(app.mappedTrack(-1), null);

app.trackMappingReversed = true;
app.setMapping(0, '1');
assert.deepStrictEqual(app.customMapping, [1, 1, '0']);
assert.strictEqual(app.trackMappingReversed, false, 'a hand mapping ends "reversed"');
app.analyzing = true; app.trackMappingReversed = true;
app.setMapping(0, '2');
assert.deepStrictEqual(app.customMapping, [1, 1, '0'], 'refused while detecting (a running detection replaces the mapping)');
assert.strictEqual(app.trackMappingReversed, true);

console.log('inline_edit ok');
