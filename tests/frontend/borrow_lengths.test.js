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

    // ---- markup: the button, the list and the note exist, with focus styling on the buttons ----
    const html = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'index.html'), 'utf8');
    const section = html.slice(html.indexOf('id="borrow-lengths"'), html.indexOf('<!-- Side selection'));
    assert.ok(section.includes('Find lengths from other pressings'));
    assert.ok(section.includes('Use these lengths'));
    assert.ok(section.includes('in visibleDonors'), 'the list shows the recomputed donors');
    assert.strictEqual((section.match(/focus-visible:ring-2/g) || []).length, 2, 'both buttons show a focus ring');
    assert.ok(html.includes('Lengths borrowed from'));
    assert.ok(!section.includes('matched`'), 'the list no longer claims "N/M matched"');
    // White on the amber #E5A100 is 2.1:1; the new button uses dark text (about 9:1)
    const useButton = section.slice(section.indexOf('<button type="button" @click="applyDonor'), section.indexOf('Use these lengths'));
    // The button uses the app's .btn-primary as is: its white-on-amber contrast is fixed app-wide in its own PR

    finished = true;
    console.log('borrow_lengths: ok');
})().catch(error => { console.error(error); process.exit(1); });
