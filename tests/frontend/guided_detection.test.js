// Guided detection in the UI: the ghost markers of the Discogs-expected breaks, the hand-edit
// flag, the "you edited by hand" banner and Keep my edits.  Snap to Discogs has its own file.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'index.html'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();
const tick = () => new Promise(resolve => setImmediate(resolve));
const track = (position, title, duration) => ({ position, title, duration });
const release = { id: 1, tracks: [track('A1', 'One', '4:00'), track('A2', 'Two', '5:00'), track('B1', 'Three', '3:00'), track('B2', 'Four', '')] };
const detected = (n, start = 0) => ({ number: n, start, end: start + 100, duration: 100, editing: false, ignored: false });

// Fake regions plugin: setOptions() fires 'region-updated' in the real one, which is what
// the tests below must not mistake for a hand edit.
function fakeRegions() {
    const list = [];
    return {
        list, getRegions: () => list, clearRegions() { list.length = 0; },
        addRegion(options) {
            const region = { ...options, end: options.end ?? options.start, element: { style: {} },
                remove() { list.splice(list.indexOf(region), 1); }, setOptions(change) { Object.assign(region, change); } };
            list.push(region);
            return region;
        }
    };
}
function mkApp() {
    const app = vinylApp();
    app.$nextTick = async () => {};
    app.$refs = {};
    app.initWaveform = async () => { app.waveform = { getDuration: () => 600, destroy() {}, pause() {} }; app.waveformRegions = fakeRegions(); app.addTrackRegions(); };
    app.currentFileId = 'f';
    app.uploadedFiles = [{ id: 'f', filename: 'a.wav' }];
    app.currentFile = { duration: 600 };
    app.waveform = { getDuration: () => 600, destroy() {}, pause() {} };
    app.waveformRegions = fakeRegions();
    return app;
}
// Fake /api/analyze-guided: `count` tracks of 100 s, expected breaks at 95, 190, ...
function guidedReply(count, method = 'durations') {
    return {
        ok: true,
        json: async () => ({
            tracks: Array.from({ length: count }, (_, i) => ({ number: i + 1, start: i * 100, end: i * 100 + 100, duration: 100 })),
            method, matched: true, music_start: 0, music_end: count * 100,
            boundaries: Array.from({ length: count - 1 }, (_, i) => ({ index: i + 1, expected: (i + 1) * 95, snapped: true, position: (i + 1) * 100, gap: null }))
        })
    };
}

(async () => {
    const calls = [];
    let method = 'durations';
    global.fetch = async (url, options) => { calls.push(url); return guidedReply(JSON.parse(options.body).expected_tracks, method); };

    // ---- a pick places the suggested sides' tracks; durations give ghost markers, a count does not ----
    let app = mkApp();
    app.currentFile = { duration: 0 };
    app.selectRelease(release);
    await tick(); await tick();
    assert.deepStrictEqual(calls, ['/api/analyze-guided']);
    assert.deepStrictEqual(app.selectedSides, ['A', 'B']);
    assert.strictEqual(app.detectedTracks.length, 4);
    assert.deepStrictEqual(app.expectedBoundaries, [95, 190, 285]);
    assert.deepStrictEqual(app.waveformRegions.list.filter(r => r.id.startsWith('expected-')).map(r => r.start), [95, 190, 285]);
    assert.strictEqual(app.boundariesEdited, false);
    method = 'count';
    await app.redetectForTargets();
    assert.deepStrictEqual(app.expectedBoundaries, [], 'a count-based placement has no expected breaks');
    assert.strictEqual(app.waveformRegions.list.filter(r => r.id.startsWith('expected-')).length, 0);
    method = 'durations';
    await app.redetectForTargets();

    // ---- ghost markers are not tracks: dashed, fixed, and a region event on one changes nothing ----
    app.addTrackRegions();
    const ghosts = app.waveformRegions.list.filter(r => r.id.startsWith('expected-'));
    assert.strictEqual(ghosts.length, 3);
    assert.ok(ghosts.every(g => g.drag === false && g.resize === false && g.end === g.start));
    assert.ok(ghosts.every(g => g.element.style.borderLeft.includes('dashed')));
    const before = JSON.stringify(app.detectedTracks);
    ghosts[0].start = ghosts[0].end = 10;
    app.updateTrackFromRegion(ghosts[0]);
    assert.strictEqual(JSON.stringify(app.detectedTracks), before);
    assert.strictEqual(app.boundariesEdited, false);
    app.clearExpectedMarkers();
    assert.strictEqual(app.waveformRegions.list.filter(r => r.id.startsWith('expected-')).length, 0);
    assert.strictEqual(app.waveformRegions.list.length, 4, 'the track regions stay');
    assert.deepStrictEqual(app.expectedBoundaries, []);
    await app.redetectForTargets();

    // ---- what is a hand edit: a real move of a cut, not the echo of setOptions() ----
    const region = n => app.waveformRegions.list.find(r => r.id === 'track-' + n);
    app.updateTrackFromRegion(region(1));
    assert.strictEqual(app.boundariesEdited, false, 'an unchanged region (colour refresh, pushed neighbour) is no edit');
    app.toggleTrackIgnored(app.detectedTracks[3]);
    app.toggleTrackIgnored(app.detectedTracks[3]);
    assert.strictEqual(app.boundariesEdited, false, 'ignoring a track does not move a cut');
    region(1).end = 60;
    app.updateTrackFromRegion(region(1));
    assert.strictEqual(app.boundariesEdited, true, 'dragging a cut is an edit');
    for (const edit of [
        a => a.updateTrackStart(1, '101'),
        a => a.updateTrackEnd(0, '99'),
        a => { a.contextMenu = { time: 50 }; a.splitAtContextMenu(); },
        a => { a.regionContextMenu = { time: 50 }; a.splitAtRegionContextMenu(); },
        a => a.deleteTrack(2)
    ]) {
        const fresh = mkApp();
        fresh.detectedTracks = [detected(1, 0), detected(2, 100)];
        fresh.addTrackRegions();
        edit(fresh);
        assert.strictEqual(fresh.boundariesEdited, true);
    }

    // ---- after a hand edit, a sides change keeps the cuts and asks (banner), no request ----
    calls.length = 0;
    app.selectedSides = ['A'];
    assert.strictEqual(app.editBanner, false);
    app.toggleSide('B');
    await tick();
    assert.deepStrictEqual(calls, []);
    assert.strictEqual(app.editBanner, true);
    assert.deepStrictEqual(app.expectedBoundaries, [], 'the markers belong to the old placement');
    assert.strictEqual(app.waveformRegions.list.filter(r => r.id.startsWith('expected-')).length, 0);
    assert.strictEqual(app.customMapping.length, 4, 'the mapping follows the sides at once');
    app.applyPreset(['A']);
    await tick();
    app.editBanner = false;
    app.selectRelease(release);   // a release change asks as well
    await tick();
    assert.strictEqual(app.editBanner, true, 'picking a release after hand edits asks');
    assert.deepStrictEqual(calls, []);
    app.keepMyEdits();
    assert.strictEqual(app.editBanner, false);
    assert.strictEqual(app.boundariesEdited, true);
    assert.deepStrictEqual(calls, []);

    // ---- a new detection forgets the edit flag, the banner and the markers ----
    app.expectedBoundaries = [1, 2];
    app.editBanner = true;
    app.setDetectedTracks([{ number: 1, start: 0, end: 5, duration: 5 }]);
    assert.deepStrictEqual(app.expectedBoundaries, []);
    assert.strictEqual(app.boundariesEdited, false);
    assert.strictEqual(app.editBanner, false);

    // ---- a first pick on tracks nobody edited (e.g. silence detection) places them again ----
    calls.length = 0;
    app = mkApp();
    app.detectedTracks = [detected(1)];
    app.selectRelease(release);
    await tick(); await tick();
    assert.deepStrictEqual(calls, ['/api/analyze-guided']);

    // ---- Change release drops the sides, the banner and the markers ----
    app.expectedBoundaries = [95];
    app.editBanner = true;
    app.changeRelease();
    assert.deepStrictEqual(app.selectedSides, []);
    assert.deepStrictEqual(app.expectedBoundaries, []);
    assert.strictEqual(app.editBanner, false);

    // ---- a pick during a running detection is refused, not queued behind it ----
    app = mkApp();
    app.analyzing = true;
    app.selectRelease(release);
    assert.strictEqual(app.selectedRelease, null);
    assert.ok(app.toasts.some(t => t.type === 'warning'));

    // ---- Change release is refused while a detection runs (its reply would install tracks with no release) ----
    app = mkApp();
    app.selectedRelease = release; app.selectedSides = ['A'];
    app.analyzing = true;
    app.changeRelease();
    assert.strictEqual(app.selectedRelease, release);
    assert.deepStrictEqual(app.selectedSides, ['A']);

    // ---- a refused pick still cancels an older card that is loading ----
    app = mkApp();
    app.analyzing = true;
    app._pendingPickId = 7;
    app.selectRelease(release);
    assert.strictEqual(app._pendingPickId, null);

    // ---- split / delete clear the release: its markers, sides and mapping go too; Undo of a delete restores them ----
    for (const clear of [a => { a.contextMenu = { time: 50 }; a.splitAtContextMenu(); }, a => { a.regionContextMenu = { time: 50 }; a.splitAtRegionContextMenu(); }, a => a.deleteTrack(2)]) {
        app = mkApp();
        app.selectedRelease = release; app.selectedSides = ['A', 'B'];
        app.detectedTracks = [detected(1, 0), detected(2, 100)];
        app.expectedBoundaries = [95];
        app.resetMappingToTargets();
        app.addTrackRegions();
        clear(app);
        assert.strictEqual(app.selectedRelease, null);
        assert.deepStrictEqual(app.expectedBoundaries, []);
        assert.strictEqual(app.waveformRegions.list.filter(r => r.id.startsWith('expected-')).length, 0);
        assert.deepStrictEqual(app.selectedSides, []);
        assert.deepStrictEqual(app.customMapping, []);
    }
    app.runToastAction(app.toasts[app.toasts.length - 1].id);   // Undo of the delete
    assert.strictEqual(app.selectedRelease, release);
    assert.deepStrictEqual(app.selectedSides, ['A', 'B']);
    assert.deepStrictEqual(app.expectedBoundaries, [95]);
    assert.deepStrictEqual(app.customMapping, [0, 1]);
    assert.strictEqual(app.waveformRegions.list.filter(r => r.id.startsWith('expected-')).length, 1);

    // ---- a failed re-detection drops the old sides' markers ----
    app = mkApp();
    app.selectedRelease = release; app.selectedSides = ['A'];
    app.detectedTracks = [detected(1, 0), detected(2, 100)];
    app.expectedBoundaries = [95];
    app.addTrackRegions();
    global.fetch = async () => ({ ok: false, json: async () => ({ detail: 'boom' }) });
    await app.redetectForTargets();
    assert.deepStrictEqual(app.expectedBoundaries, []);
    assert.strictEqual(app.waveformRegions.list.filter(r => r.id.startsWith('expected-')).length, 0);

    // ---- the markup: no trace of the removed duration-based splitting ----
    assert.ok(!html.includes('offerDurationBasedSplitting') && !html.includes('analyze-duration-based'));
    assert.ok(!src.includes('analyze-duration-based'));
    console.log('guided detection ok');
})().catch(error => { console.error(error); process.exit(1); });
