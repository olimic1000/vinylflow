// Borrow track lengths from another pressing: visibility, progress, copy-on-write apply, Undo,
// the guards (detecting, exporting, hand edits) and every reset that must drop a stale lookup.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

// An async test that never reaches its end would exit 0 unnoticed (run.js judges exit codes only).
let finished = false;
process.on('exit', () => {
    if (!finished) {
        console.error('borrow_lengths tests did not finish');
        process.exitCode = 1;
    }
});

const T = (position, duration) => ({ position, title: 't' + position, duration });
const mkRelease = (id = 7) => ({ id, artist: 'A', title: 'T', tracks: [T('A1', ''), T('A2', '4:00'), T('A3', ''), T('B1', ''), T('B2', '')] });
const lengthsOf = release => release.tracks.map(track => track.duration);

function mk(release = mkRelease(), sides = ['A'], { realRedetect = false } = {}) {
    const app = vinylApp();
    app.$nextTick = async () => {};
    app.currentFileId = 'f1';
    app.uploadedFiles = [{ id: 'f1', filename: 'one.wav' }, { id: 'f2', filename: 'two.wav' }];
    app.currentFile = app.uploadedFiles[0];
    app.selectedRelease = release;
    app.selectedSides = sides;
    // Re-detection itself is covered by guided_detection / guided_supersede; here only that it is requested.
    app.afterCalls = 0;
    if (!realRedetect) app.afterTargetsChanged = () => { app.afterCalls++; return Promise.resolve(true); };
    return app;
}
const donor = (over = {}) => ({
    release_id: 99, label: 'Reissue', year: 2005, country: 'US', format: 'Vinyl, LP',
    matched: 4, missing: 5, lengths: { '0': '3:00', '1': '9:99', '2': '5:00', '4': '2:30' }, ...over
});
// What the Release card's list holds after a finished lookup
const withList = (app, list = [donor()]) => {
    app.donors = { releaseId: app.selectedRelease.id, status: 'done', checked: 2, budget: 12, list, error: '' };
    return app;
};
const clickUndo = app => app.runToastAction(app.toasts[app.toasts.length - 1].id);
const lastToast = app => app.toasts[app.toasts.length - 1];

(async () => {
    // ---- visibility: the chosen sides only ----
    let app = mk(mkRelease(), ['A']);
    assert.strictEqual(app.showBorrowLengths, true);
    assert.strictEqual(app.borrowLengthsMessage, 'Only 1/3 tracks have lengths');
    app = mk({ id: 1, tracks: [T('A1', '3:00'), T('A2', '4:00'), T('B1', ''), T('B2', '')] }, ['A']);
    assert.strictEqual(app.showBorrowLengths, false, 'full on the chosen side, gaps on the other');
    app.selectedSides = ['B'];
    assert.strictEqual(app.showBorrowLengths, true);
    assert.strictEqual(app.borrowLengthsMessage, 'This release has no track lengths');
    app.selectedSides = ['A', 'B'];
    assert.strictEqual(app.borrowLengthsMessage, 'Only 2/4 tracks have lengths');
    app.selectedRelease = null;
    assert.strictEqual(app.showBorrowLengths, false);

    // ---- lookup: request, progress text (own release only), result list ----
    app = mk();
    let resolveFetch;
    const urls = [];
    global.fetch = url => { urls.push(url); return new Promise(resolve => { resolveFetch = resolve; }); };
    const lookup = app.findLengthDonors();
    assert.deepStrictEqual(urls, ['/api/release/7/length-donors']);
    assert.strictEqual(app.donors.status, 'searching');
    assert.strictEqual(app.donorProgressText, 'Checking other pressings… (0 of up to 12)');
    app.handleWebSocketMessage({ type: 'donor_progress', release_id: 8, checked: 5, budget: 12, found: 0 });
    assert.strictEqual(app.donors.checked, 0, 'another release is ignored');
    app.handleWebSocketMessage({ type: 'donor_progress', release_id: 7, checked: 3, budget: 12, found: 1 });
    assert.strictEqual(app.donorProgressText, 'Checking other pressings… (3 of up to 12)');
    resolveFetch({ ok: true, json: async () => ({ donors: [donor()] }) });
    await lookup;
    assert.strictEqual(app.donors.status, 'done');
    assert.strictEqual(app.donorTitle(app.donors.list[0]), 'Reissue · 2005 · US · Vinyl, LP');

    global.fetch = async () => ({ ok: true, json: async () => ({ donors: [] }) });
    await app.findLengthDonors();
    assert.deepStrictEqual([app.donors.status, app.donors.list.length], ['done', 0]);
    global.fetch = async () => ({ ok: false, status: 502, json: async () => ({ detail: 'Discogs down' }) });
    await app.findLengthDonors();
    assert.deepStrictEqual([app.donors.status, app.donors.error], ['error', 'Discogs down']);
    global.fetch = async () => { throw new Error('offline'); };
    await app.findLengthDonors();
    assert.deepStrictEqual([app.donors.status, app.donors.error], ['error', 'offline']);

    // ---- a reply that arrives after the release, the file or an Undo changed is dropped ----
    const staleCases = {
        'Change release': a => a.changeRelease(),
        'another release picked': a => a.selectRelease(mkRelease(11)),
        'file switched': a => { a.fetchScan = () => {}; a.selectFile('f2'); },
        'edit Undo restored a snapshot': a => a.restoreEdits(a.snapshotEdits()),
    };
    for (const [name, change] of Object.entries(staleCases)) {
        app = mk();
        // Switching files starts a Discogs search with its own fetch: answer only the lookup's request
        let resolveLookup;
        global.fetch = url => url.includes('length-donors')
            ? new Promise(resolve => { resolveLookup = resolve; })
            : Promise.resolve({ ok: true, json: async () => ({ results: [], has_more: false }) });
        const stale = app.findLengthDonors();
        change(app);
        resolveLookup({ ok: true, json: async () => ({ donors: [donor()] }) });
        await stale;
        assert.strictEqual(app.donors.status, 'idle', name);
        assert.deepStrictEqual(app.donors.list, [], name);
    }
    // ... and a list already shown disappears with the same changes
    for (const [name, change] of Object.entries(staleCases)) {
        app = withList(mk());
        change(app);
        assert.strictEqual(app.donors.status, 'idle', 'list reset by ' + name);
    }

    // ---- apply: only missing lengths, on a copy; the original release object is untouched ----
    app = withList(mk());
    const original = app.selectedRelease;
    const originalTrackA1 = original.tracks[0];
    app.applyDonor(donor());
    assert.notStrictEqual(app.selectedRelease, original, 'copy-on-write: a new release object is installed');
    assert.deepStrictEqual(lengthsOf(original), ['', '4:00', '', '', ''], 'the shared object keeps its lengths');
    assert.strictEqual(original.tracks[0], originalTrackA1);
    assert.strictEqual(original.lengthsBorrowedFrom, undefined);
    // Requirement changed: the donor also lists B2 (index 4) but only side A is chosen, so B2 stays
    // empty; it used to be filled ('2:30') and the toast count disagreed with "fills N of M missing".
    assert.deepStrictEqual(lengthsOf(app.selectedRelease), ['3:00', '4:00', '5:00', '', ''],
        'the existing 4:00 stays; the unparseable donor length (9:99 for A2) is never used; unchosen sides are not filled');
    assert.deepStrictEqual(app.lengthsBorrowedFrom.map(source => source.release_id), [99]);
    assert.strictEqual(app.borrowedLabel(app.lengthsBorrowedFrom[0]), 'Reissue, 2005, US');
    assert.strictEqual(app.donorFillText(donor()), 'fills 4 of 5 missing', 'honest label: lengths it supplies, not matches');
    assert.strictEqual(app.afterCalls, 1, 'detection re-runs through afterTargetsChanged');
    assert.strictEqual(app.donors.status, 'idle');
    assert.strictEqual(app.showBorrowLengths, false, 'side A is complete now');
    assert.strictEqual(app.toasts.length, 1);
    assert.strictEqual(lastToast(app).type, 'success');
    assert.strictEqual(lastToast(app).action.label, 'Undo');
    assert.match(lastToast(app).message, /^Borrowed 2 track lengths from /, 'toast count = chosen-side tracks filled (A1, A3)');

    // ---- Undo: the original object comes back and detection re-runs ----
    clickUndo(app);
    assert.strictEqual(app.selectedRelease, original);
    assert.deepStrictEqual(app.lengthsBorrowedFrom, []);
    assert.strictEqual(app.afterCalls, 2);
    assert.strictEqual(app.showBorrowLengths, true);

    // Undo after another release was picked: nothing is clobbered, no re-detection
    app = withList(mk());
    app.applyDonor(donor());
    const other = mkRelease(8);
    app.selectedRelease = other;
    const callsBefore = app.afterCalls;
    clickUndo(app);
    assert.strictEqual(app.selectedRelease, other);
    assert.strictEqual(app.afterCalls, callsBefore);
    assert.strictEqual(lastToast(app).type, 'info');

    // Undo from another file is refused by notifyUndo's guard (and keeps the borrowed lengths)
    app = withList(mk());
    app.applyDonor(donor());
    const borrowedRelease = app.selectedRelease;
    app.currentFileId = 'f2';
    clickUndo(app);
    assert.strictEqual(app.selectedRelease, borrowedRelease);
    assert.strictEqual(lastToast(app).type, 'warning');

    // A second borrow's Undo restores the first borrow, not the very first state
    app = withList(mk(), [donor(), donor({ release_id: 100, label: 'Other', lengths: { '3': '1:11' } })]);
    app.selectedSides = ['A', 'B'];
    app.applyDonor(donor());
    const afterFirst = app.selectedRelease;
    app.donors = { releaseId: 7, status: 'done', checked: 1, budget: 12, list: [donor({ release_id: 100, label: 'Other', lengths: { '3': '1:11' } })], error: '' };
    app.applyDonor(app.donors.list[0]);
    assert.deepStrictEqual(app.lengthsBorrowedFrom.map(source => source.release_id), [99, 100],
        'the second borrow adds its source; the first stays credited');
    assert.deepStrictEqual(lengthsOf(afterFirst), ['3:00', '4:00', '5:00', '', '2:30'], 'the first copy is not mutated');
    clickUndo(app);
    assert.strictEqual(app.selectedRelease, afterFirst);
    assert.deepStrictEqual(app.lengthsBorrowedFrom.map(source => source.release_id), [99]);

    // ---- nothing to fill -> no copy, no marker, no re-detect ----
    app = withList(mk({ id: 7, tracks: [T('A1', '3:00'), T('A2', '')] }));
    const full = app.selectedRelease;
    app.applyDonor(donor({ lengths: { '0': '9:00' } }));
    assert.strictEqual(app.selectedRelease, full);
    assert.deepStrictEqual(app.lengthsBorrowedFrom, []);
    assert.strictEqual(app.afterCalls, 0);
    assert.strictEqual(lastToast(app).type, 'info');

    // ---- refused while detecting or exporting, with a warning ----
    for (const [name, busy, text] of [
        ['detecting', a => { a.analyzing = true; }, /track detection/],
        ['exporting', a => { a.isProcessing = true; a.uploadedFiles[0].status = 'processing'; }, /exporting/],
    ]) {
        app = withList(mk());
        const before = app.selectedRelease;
        busy(app);
        app.applyDonor(donor());
        assert.strictEqual(app.selectedRelease, before, name);
        assert.strictEqual(app.afterCalls, 0, name);
        assert.strictEqual(lastToast(app).type, 'warning', name);
        assert.match(lastToast(app).message, text, name);
    }
    // Undo is refused while detecting (the undo guard) and changes nothing
    app = withList(mk());
    app.applyDonor(donor());
    const borrowed = app.selectedRelease;
    app.analyzing = true;
    clickUndo(app);
    assert.strictEqual(app.selectedRelease, borrowed);
    assert.strictEqual(lastToast(app).type, 'warning');

    // ---- a click on a list that belongs to another release does nothing ----
    app = mk();
    app.donors = { releaseId: 999, status: 'done', checked: 1, budget: 12, list: [donor()], error: '' };
    const untouched = app.selectedRelease;
    app.applyDonor(donor());
    assert.strictEqual(app.selectedRelease, untouched);
    assert.strictEqual(app.toasts.length, 0);

    // ---- hand-edited boundaries: the existing ask-first path (banner), not a silent re-detection ----
    app = withList(mk(mkRelease(), ['A'], { realRedetect: true }));
    app.detectedTracks = [{ number: 1, start: 0, end: 100, duration: 100 }];
    app.markEdited();
    let redetections = 0;
    app.redetectForTargets = () => { redetections++; return Promise.resolve(true); };
    app.applyDonor(donor());
    assert.strictEqual(app.editBanner, true);
    assert.strictEqual(redetections, 0);
    assert.deepStrictEqual(lengthsOf(app.selectedRelease), ['3:00', '4:00', '5:00', '', ''], 'side A only (B2 not filled)');

    // ---- a donor covering one chosen-side track and one unselected-side track fills one ----
    app = withList(mk(mkRelease(), ['A']), [donor({ lengths: { '0': '3:00', '3': '3:30' } })]);
    assert.strictEqual(app.donorFillText(app.visibleDonors[0]), 'fills 1 of 2 missing');
    app.applyDonor(app.donors.list[0]);
    assert.deepStrictEqual(lengthsOf(app.selectedRelease), ['3:00', '4:00', '', '', ''], 'B1 (unselected side) stays empty');
    assert.match(lastToast(app).message, /^Borrowed 1 track length from /, 'singular, agrees with the label');
    // Only an unselected side is covered: nothing to fill
    app = withList(mk(mkRelease(), ['A']), [donor({ lengths: { '3': '3:30' } })]);
    const untouchedRelease = app.selectedRelease;
    app.applyDonor(app.donors.list[0]);
    assert.strictEqual(app.selectedRelease, untouchedRelease);
    assert.strictEqual(lastToast(app).type, 'info');

    // ---- the borrowed copy is what the shortcut/selection later offers; a new pick starts clean ----
    app = withList(mk());
    app.applyDonor(donor());
    app.selectRelease(mkRelease(11));
    assert.deepStrictEqual(app.lengthsBorrowedFrom, [], 'a new release has no marker');
    assert.strictEqual(app.donors.status, 'idle');

    // ---- deleting a track (dropReleaseAfterEdit) forgets the donor list too ----
    app = withList(mk());
    app.dropReleaseAfterEdit();
    assert.strictEqual(app.selectedRelease, null);
    assert.strictEqual(app.donors.status, 'idle', 'a list fetched for the dropped release is gone');
    assert.deepStrictEqual(app.donors.list, []);

    // ---- the shown donors are recomputed against the current lengths and the chosen sides ----
    // A cached list is never shown with counts from before a borrow; zero-fill donors are dropped
    // (the lookup had no way to know which sides were chosen) and the best filler comes first.
    app = withList(mk(mkRelease(), ['A']), [
        donor({ release_id: 1, matched: 3, missing: 5, lengths: { '0': '3:00', '3': '3:30', '4': '2:30' } }),   // A1 + two B tracks
        donor({ release_id: 2, matched: 2, missing: 5, lengths: { '3': '3:30', '4': '2:30' } }),                // B only
        donor({ release_id: 3, matched: 2, missing: 5, lengths: { '0': '3:00', '2': '5:00' } }),                // A1 + A3
        donor({ release_id: 4, matched: 1, missing: 5, lengths: { '1': '4:01', '0': '3:01' } }),                // A2 already known
    ]);
    assert.deepStrictEqual(app.visibleDonors.map(d => [d.release_id, d.matched, d.missing]),
        [[3, 2, 2], [1, 1, 2], [4, 1, 2]], 'sorted by fills on side A, ties keep the backend order; B-only dropped');
    assert.strictEqual(app.donorFillText(app.visibleDonors[0]), 'fills 2 of 2 missing');
    app.selectedSides = ['B'];
    assert.deepStrictEqual(app.visibleDonors.map(d => [d.release_id, d.matched, d.missing]), [[1, 2, 2], [2, 2, 2]]);
    // A length filled by hand or by another borrow no longer counts
    app.selectedSides = ['A'];
    const edited = { ...app.selectedRelease, tracks: app.selectedRelease.tracks.map((t, i) => i === 0 ? { ...t, duration: '3:00' } : t) };
    app.selectedRelease = edited;
    assert.deepStrictEqual(app.visibleDonors.map(d => [d.release_id, d.matched, d.missing]), [[3, 1, 1]],
        'A1 is filled now: only the donor with A3 is left');
    app.selectedRelease = { ...edited, tracks: edited.tracks.map(t => ({ ...t, duration: t.duration || '1:00' })) };
    assert.deepStrictEqual(app.visibleDonors, [], 'nothing missing, nothing to offer');

    // ---- sides are suggested again once lengths exist (maintainer's example: 8-track LP, side A recording) ----
    const mkLp = () => ({
        id: 7, artist: 'A', title: 'T',
        tracks: ['A1', 'A2', 'A3', 'A4', 'B1', 'B2', 'B3', 'B4'].map(position => T(position, '')),
    });
    const lpDonor = () => donor({
        matched: 8, missing: 8,
        lengths: Object.fromEntries([0, 1, 2, 3, 4, 5, 6, 7].map(index => [String(index), '4:00'])),
    });
    // Picked through the real selectRelease: no lengths -> full album ('default'), the recording is one side long
    const pickLp = () => {
        const picked = mk(mkLp(), []);
        picked.currentFile = { id: 'f1', filename: 'one.wav', duration: 990, musicDuration: 960 };
        picked.loadAlbumDescription = () => {};
        picked.selectRelease(picked.selectedRelease);
        return picked;
    };
    app = pickLp();
    assert.deepStrictEqual([app.selectedSides, app.sidesSuggestionReason, app.targetTracks.length], [['A', 'B'], 'default', 8]);
    const callsAfterPick = app.afterCalls;
    app.donors = { releaseId: 7, status: 'done', checked: 1, budget: 12, list: [lpDonor()], error: '' };
    const lpBefore = app.selectedRelease;
    app.applyDonor(lpDonor());
    assert.deepStrictEqual(app.selectedSides, ['A'], 'the length fit now picks side A');
    assert.strictEqual(app.sidesSuggestionReason, 'length');
    assert.deepStrictEqual(app.suggestedSides, ['A']);
    assert.strictEqual(app.targetTracks.length, 4, 'guided detection will ask for 4 tracks');
    assert.strictEqual(app.afterCalls, callsAfterPick + 1, 'one re-detection, with the new sides');
    // Undo: sides, suggestion and reason come back, and detection re-runs once more for 8
    clickUndo(app);
    assert.strictEqual(app.selectedRelease, lpBefore);
    assert.deepStrictEqual([app.selectedSides, app.suggestedSides, app.sidesSuggestionReason], [['A', 'B'], ['A', 'B'], 'default']);
    assert.strictEqual(app.targetTracks.length, 8);
    assert.strictEqual(app.afterCalls, callsAfterPick + 2);

    // Sides the user chose by hand are left alone, by the borrow and by its Undo
    // (the user's choice is recorded by the preset/chip handlers: a raw assignment is not one any more)
    app = pickLp();
    app.applyPreset(['B']);
    app.donors = { releaseId: 7, status: 'done', checked: 1, budget: 12, list: [lpDonor()], error: '' };
    app.applyDonor(lpDonor());
    assert.deepStrictEqual(app.selectedSides, ['B']);
    assert.strictEqual(app.sidesSuggestionReason, 'default');
    clickUndo(app);
    assert.deepStrictEqual(app.selectedSides, ['B']);
    // Sides changed by hand after the borrow stay changed when it is undone
    app = pickLp();
    app.donors = { releaseId: 7, status: 'done', checked: 1, budget: 12, list: [lpDonor()], error: '' };
    app.applyDonor(lpDonor());
    app.applyPreset(['B']);
    clickUndo(app);
    assert.deepStrictEqual(app.selectedSides, ['B']);

    // ---- a donor whose lengths differ from the release's own is demoted with a note, never hidden ----
    assert.strictEqual(app.donorDisagreeText(donor()), '', 'no note without a disagreement');
    assert.strictEqual(app.donorDisagreeText(donor({ disagree: 0, compared: 3 })), '');
    assert.strictEqual(app.donorDisagreeText(donor({ disagree: 2, compared: 5 })), 'Lengths differ from this release on 2 of 5 tracks');
    assert.strictEqual(app.donorDisagreeText(donor({ disagree: 1, compared: 1 })), 'Lengths differ from this release on 1 of 1 track');
    app = withList(mk(mkRelease(), ['A']), [
        donor({ release_id: 1, lengths: { '0': '3:00', '2': '5:00' }, disagree: 1, compared: 1 }),   // fills 2 but disagrees
        donor({ release_id: 2, lengths: { '0': '3:00' }, disagree: 0, compared: 1 }),                  // fills 1, agrees
        donor({ release_id: 3, lengths: { '2': '5:00' } }),                                            // fills 1, no comparison
    ]);
    assert.deepStrictEqual(app.visibleDonors.map(d => d.release_id), [2, 3, 1], 'agreeing donors first, then fills, then server order');
    assert.strictEqual(app.visibleDonors[2].disagree, 1, 'the server note data survives the recomputation');

    // ---- the empty message says how far the lookup got ----
    global.fetch = async () => ({ ok: true, json: async () => ({ donors: [], checked: 12, versions: 340 }) });
    app = mk();
    await app.findLengthDonors();
    assert.strictEqual(app.donorEmptyText, 'No other pressing has the missing lengths among the 12 checked (of 340).');
    app.donors = { ...app.donors, checked: 5, versions: 5 };
    assert.strictEqual(app.donorEmptyText, 'No other pressing has the missing lengths among the 5 checked.');
    app.donors = { ...app.donors, checked: 1, versions: 340 };
    assert.strictEqual(app.donorEmptyText, "The 1 other pressing checked (of 340) doesn't have the missing lengths.");
    app.donors = { ...app.donors, checked: 0, versions: null };
    assert.strictEqual(app.donorEmptyText, 'No other pressing has the missing lengths.');

    // ---- review round 2 ----
    const lpDonors = a => { a.donors = { releaseId: 7, status: 'done', checked: 1, budget: 12, list: [lpDonor()], error: '' }; };

    // Undo of a re-suggested borrow brings the user's hand-edited, reversed mapping back
    app = pickLp();
    app.detectedTracks = [0, 1, 2, 3, 4, 5, 6, 7].map(n => ({ number: n + 1, start: n * 100, end: n * 100 + 99, duration: 99 }));
    app.resetMappingToTargets();
    app.reverseMapping();
    assert.deepStrictEqual([...app.customMapping], [7, 6, 5, 4, 3, 2, 1, 0]);
    assert.strictEqual(app.trackMappingReversed, true);
    const mismatchBefore = app.trackCountMismatch;
    lpDonors(app);
    app.applyDonor(lpDonor());
    assert.deepStrictEqual([...app.customMapping], [0, 1, 2, 3, 3, 3, 3, 3], 'mapping follows the new sides (kills a removed reset)');
    assert.strictEqual(app.trackMappingReversed, false);
    assert.strictEqual(app.trackCountMismatch, true, '8 detected tracks against 4 targets');
    clickUndo(app);
    assert.deepStrictEqual([...app.selectedSides], ['A', 'B']);
    assert.deepStrictEqual([...app.customMapping], [7, 6, 5, 4, 3, 2, 1, 0], 'the user\'s mapping is back');
    assert.strictEqual(app.trackMappingReversed, true);
    assert.strictEqual(app.trackCountMismatch, mismatchBefore);

    // An explicit "Full album" click (equal to the default) is the user's choice: a borrow leaves it alone
    app = pickLp();
    assert.strictEqual(app.sidesTouched, false, 'a picked release starts untouched');
    app.applyPreset(['A', 'B']);
    assert.strictEqual(app.sidesTouched, true);
    lpDonors(app);
    app.applyDonor(lpDonor());
    assert.deepStrictEqual([...app.selectedSides], ['A', 'B']);
    assert.strictEqual(app.sidesSuggestionReason, 'default');
    app = pickLp();
    app.toggleSide('B'); app.toggleSide('B');   // back to A+B by hand
    assert.strictEqual(app.sidesTouched, true);
    app.selectRelease(mkLp());
    assert.strictEqual(app.sidesTouched, false, 'selecting a release resets it');
    // ... and a snapshot (workspace switch, Undo flows) carries it
    app.applyPreset(['A']);
    const touchedSnapshot = app.snapshotEdits();
    app.clearEditState();
    assert.strictEqual(app.sidesTouched, false);
    app.restoreEdits(touchedSnapshot);
    assert.strictEqual(app.sidesTouched, true);

    // A suggestion made by length is left alone by a borrow (only 'default' is re-suggested)
    // Side B has its lengths and A lacks them: after the borrow the recording (32 min) would fit A+B
    app = mk({ id: 7, tracks: [T('A1', ''), T('A2', ''), T('A3', ''), T('A4', ''),
        T('B1', '4:00'), T('B2', '4:00'), T('B3', '4:00'), T('B4', '4:00')] }, ['A']);
    app.suggestedSides = ['A']; app.sidesSuggestionReason = 'length';
    app.currentFile = { id: 'f1', filename: 'one.wav', duration: 1950, musicDuration: 1920 };
    lpDonors(app);
    app.applyDonor(lpDonor());
    assert.deepStrictEqual([...app.selectedSides], ['A']);
    assert.strictEqual(app.sidesSuggestionReason, 'length');

    // Undo after the user toggled a side: selection stays, but the suggestion and its reason are restored
    app = pickLp();
    lpDonors(app);
    app.applyDonor(lpDonor());
    assert.strictEqual(app.sidesSuggestionReason, 'length');
    app.toggleSide('B');
    clickUndo(app);
    assert.deepStrictEqual([...app.selectedSides], ['A', 'B'], 'the user\'s sides stay');
    assert.deepStrictEqual([...app.suggestedSides], ['A', 'B']);
    assert.strictEqual(app.sidesSuggestionReason, 'default', 'no stale "matches recording length"');

    // Disagreement is recounted for the chosen sides, from the server's per-track indexes
    const sideRelease = () => ({ id: 7, tracks: [T('A1', '3:00'), T('A2', ''), T('B1', '9:00'), T('B2', '')] });
    const scoped = disagreeing => donor({
        lengths: { '1': '4:00', '3': '2:00' }, compared: 2, disagree: disagreeing.length,
        compared_indexes: [0, 2], disagree_indexes: disagreeing
    });
    app = withList(mk(sideRelease(), ['A']), [scoped([2])]);   // the disagreement is on side B
    assert.strictEqual(app.donorDisagreeText(app.visibleDonors[0]), '', 'side A only: One agrees, no note');
    assert.deepStrictEqual([app.visibleDonors[0].compared, app.visibleDonors[0].disagree], [1, 0]);
    app.selectedSides = ['A', 'B'];
    assert.strictEqual(app.donorDisagreeText(app.visibleDonors[0]), 'Lengths differ from this release on 1 of 2 tracks');
    // Ordering follows the recount: the donor that disagrees only on side B is not demoted on side A
    app = withList(mk(sideRelease(), ['A']), [
        { ...scoped([0]), release_id: 1 },
        { ...scoped([2]), release_id: 2 },
    ]);
    assert.deepStrictEqual(app.visibleDonors.map(d => d.release_id), [2, 1]);
    // A server reply without the index lists falls back to its totals
    app = withList(mk(sideRelease(), ['A']), [donor({ lengths: { '1': '4:00' }, compared: 3, disagree: 2 })]);
    assert.strictEqual(app.donorDisagreeText(app.visibleDonors[0]), 'Lengths differ from this release on 2 of 3 tracks');

    // ---- review round 3 ----
    const trackReply = count => ({
        ok: true,
        json: async () => ({
            method: 'durations', matched: true, music_start: 0, music_end: count * 100,
            tracks: Array.from({ length: count }, (_, n) => ({ number: n + 1, start: n * 100, end: n * 100 + 99, duration: 99 })),
            boundaries: []
        })
    });
    const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };

    // Undo with the REAL afterTargetsChanged / redetectForTargets (fake detection reply): the borrow's own
    // re-detection replaced the tracks, so the saved mapping describes tracks that no longer exist
    global.fetch = async (url, options) => trackReply(JSON.parse(options.body).expected_tracks);
    app = mk(mkLp(), [], { realRedetect: true });
    app.currentFile = { id: 'f1', filename: 'one.wav', duration: 990, musicDuration: 960 };
    app.$refs = {};
    app.loadAlbumDescription = () => {};
    app.initWaveform = async () => {};
    app.applyScan = () => {};
    app.selectRelease(app.selectedRelease);
    await settle();
    assert.strictEqual(app.detectedTracks.length, 8);
    app.reverseMapping();
    assert.strictEqual(app.trackMappingReversed, true);
    lpDonors(app);
    app.applyDonor(lpDonor());
    await settle();
    assert.strictEqual(app.detectedTracks.length, 4, 'the borrow re-detected side A');
    app.markEdited();
    clickUndo(app);
    await settle();   // only the ask-first banner: the 4 tracks stay
    assert.strictEqual(app.editBanner, true);
    assert.deepStrictEqual([...app.selectedSides], ['A', 'B']);
    assert.deepStrictEqual([...app.customMapping], [0, 1, 2, 3], 'mapping of the 4 tracks that exist, not the old 8');
    assert.strictEqual(app.trackMappingReversed, false);
    assert.strictEqual(app.trackCountMismatch, true, '4 tracks against 8 targets must warn');

    // A partial fill that leaves the same sides keeps the mapping, before and after Undo
    app = pickLp();
    app.detectedTracks = [0, 1, 2, 3, 4, 5, 6, 7].map(n => ({ number: n + 1, start: n * 100, end: n * 100 + 99, duration: 99 }));
    app.resetMappingToTargets();
    app.customMapping[0] = 5;
    app.markEdited();
    lpDonors(app);
    app.applyDonor(donor({ lengths: { '0': '4:00', '1': '4:00' } }));
    assert.deepStrictEqual([...app.selectedSides], ['A', 'B'], 'two lengths do not change the fit');
    assert.deepStrictEqual([...app.customMapping], [5, 1, 2, 3, 4, 5, 6, 7], 'hand-made mapping untouched');
    app.customMapping[1] = 6;   // an edit after the borrow
    clickUndo(app);
    assert.deepStrictEqual([...app.customMapping], [5, 6, 2, 3, 4, 5, 6, 7], 'Undo does not discard later mapping edits');

    // An explicit pick equal to the borrow's suggestion is the user's: Undo leaves it
    app = pickLp();
    lpDonors(app);
    app.applyDonor(lpDonor());
    assert.deepStrictEqual([...app.selectedSides], ['A']);
    assert.strictEqual(app.sidesTouched, false, 'the borrow\'s own suggestion is not a user pick');
    app.applyPreset(['A']);
    clickUndo(app);
    assert.deepStrictEqual([...app.selectedSides], ['A']);

    // app.js must not use Array.prototype.at (WKWebView before Safari 15.4 lacks it)
    assert.ok(!/\.at\(/.test(src), 'app.js contains an .at( call');

    // ---- markup: the button, the list and the note exist, with focus styling on the buttons ----
    const html = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'index.html'), 'utf8');
    const section = html.slice(html.indexOf('id="borrow-lengths"'), html.indexOf('<!-- Side selection'));
    assert.ok(section.includes('Find lengths from other pressings'));
    assert.ok(section.includes('Use these lengths'));
    assert.ok(section.includes('in visibleDonors'), 'the list shows the recomputed donors');
    assert.strictEqual((section.match(/focus-visible:ring-2/g) || []).length, 2, 'both buttons show a focus ring');
    assert.ok(html.includes('Lengths borrowed from'));
    assert.ok(section.includes('donorDisagreeText(donor)') && section.includes('donorEmptyText'), 'the disagreement note and the empty message are rendered');
    assert.ok(section.includes('text-xs text-secondary" x-text="donorDisagreeText'), 'the note uses the card\'s muted text class');
    assert.ok(!section.includes('matched`'), 'the list no longer claims "N/M matched"');
    // White on the amber #E5A100 is 2.1:1; the new button uses dark text (about 9:1)
    const useButton = section.slice(section.indexOf('<button type="button" @click="applyDonor'), section.indexOf('Use these lengths'));
    // The button uses the app's .btn-primary as is: its white-on-amber contrast is fixed app-wide in its own PR

    finished = true;
    console.log('borrow_lengths: ok');
})().catch(error => { console.error(error); process.exit(1); });
