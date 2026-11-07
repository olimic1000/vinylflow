// Discogs-first flow: selecting a file searches by itself, cards fill in their tracklists,
// and a release can be picked before any track is detected.  Discogs is faked (fetch).
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'index.html'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();
const tick = () => new Promise(resolve => setImmediate(resolve));
const track = (position, duration) => ({ position, title: 't' + position, duration });
const detected = (count) => Array.from({ length: count }, (_, i) => ({ number: i + 1, start: i * 100, end: i * 100 + 100, duration: 100 }));

/**
 * Fake Discogs behind fetch.  Every reply is parked until the test releases it, so the
 * tests decide the order in which a search / tracklist / detection answers.
 */
function fakeServer() {
    const server = { calls: [], pending: [] };
    global.fetch = (url, options = {}) => new Promise((resolve) => {
        const body = options.body ? JSON.parse(options.body) : null;
        server.calls.push({ url, body });
        server.pending.push({ url, body, answer: (status, json) => resolve({ ok: status < 400, status, json: async () => json }) });
    });
    server.calledWith = (prefix) => server.calls.filter(call => call.url.startsWith(prefix));
    server.next = (prefix) => {
        const index = server.pending.findIndex(entry => entry.url.startsWith(prefix));
        assert.ok(index >= 0, `no pending request for ${prefix}`);
        return server.pending.splice(index, 1)[0];
    };
    return server;
}

function mkApp(files = ['Pink Floyd - Animals.wav']) {
    const app = vinylApp();
    app.$nextTick = async () => {};
    app.$refs = {};
    app.initWaveform = async () => {};
    app.destroyWaveform = () => {};
    app.discogsConfigured = true;
    app.uploadedFiles = files.map((filename, i) => ({ id: 'f' + (i + 1), filename, duration: 800, status: 'uploaded', musicDuration: 700 }));
    return app;
}
const card = (id) => ({ id, artist: 'A' + id, title: 'T' + id, year: '1977', uri: '/release/' + id, tracks: null });
const fullRelease = (id, tracks = [track('A1', '5:00'), track('A2', '6:00')]) => ({ ...card(id), label: 'L', cover_url: null, tracks });

(async () => {
    // ---- a pick starts detection: no count warning while the first detection runs, since
    // "0 detected" is not a finding yet; it does show if that detection ends with nothing ----
    {
        const server = fakeServer();
        const app = mkApp();
        app.selectFile('f1');
        app.selectRelease(fullRelease(1));
        assert.strictEqual(app.analyzing, true);
        assert.strictEqual(app.showTrackCountMismatch, false, 'no warning during the first detection');
        server.next('/api/analyze').answer(500, { detail: 'boom' });
        await tick(); await tick();
        assert.strictEqual(app.analyzing, false);
        assert.strictEqual(app.showTrackCountMismatch, true, 'a failed detection shows the warning and its options');

        // ...and a real mismatch stays visible during a Smart Rescan, whose spinner lives in it
        app.detectedTracks = detected(1);
        app.resetMappingToDetected();
        app.analyzing = true;
        assert.strictEqual(app.showTrackCountMismatch, true);
    }

    // ---- duration splitting from the "release picked, nothing detected" state: the new tracks
    // need a mapping, or every one would export as position "Unknown" ----
    {
        const server = fakeServer();
        const app = mkApp();
        app.selectFile('f1');
        app.selectRelease(fullRelease(1));
        server.next('/api/analyze').answer(200, { tracks: [] });
        await tick(); await tick();
        assert.strictEqual(app.detectedTracks.length, 0);
        assert.strictEqual(app.showTrackCountMismatch, true);

        const waveformCalls = [];
        app.destroyWaveform = () => waveformCalls.push('destroy');
        app.initWaveform = async () => { waveformCalls.push('init'); };
        const splitting = app.offerDurationBasedSplitting();
        await tick();
        server.next('/api/analyze-duration-based').answer(200, { tracks: detected(2) });
        await splitting;
        assert.deepStrictEqual(app.customMapping, [0, 1], 'each new track maps to its release track');
        assert.strictEqual(app.trackCountMismatch, false, 'the counts match now');
        assert.deepStrictEqual(waveformCalls, ['destroy', 'init'], 'initWaveform keeps an existing waveform, so it goes first');
    }

    // ---- a detection that finds nothing leaves no waveform behind: initWaveform returns at once
    // when one exists, so the retry would show its tracks without draggable regions ----
    {
        const server = fakeServer();
        const app = mkApp();
        app.selectFile('f1');
        let waveformInits = 0;
        app.initWaveform = async () => { waveformInits += 1; };
        app.selectRelease(fullRelease(1));
        server.next('/api/analyze').answer(200, { tracks: [] });
        await tick(); await tick();
        assert.strictEqual(waveformInits, 0, 'no tracks: no waveform');
        app.analyzeFile();   // the "Retry track detection" button
        server.next('/api/analyze').answer(200, { tracks: detected(2) });
        await tick(); await tick();
        assert.strictEqual(waveformInits, 1, 'the retry builds it, with its regions');
    }

    // ---- "Try Discogs again" with results on screen goes back to them: no new Discogs request ----
    {
        const server = fakeServer();
        const app = mkApp();
        app.selectFile('f1');
        server.next('/api/search').answer(200, { results: [card(1)] });
        await tick(); await tick();
        app.detectedTracks = detected(2);
        const searches = server.calledWith('/api/search').length;
        app.tryDiscogsAgain();
        assert.strictEqual(server.calledWith('/api/search').length, searches, 'results kept, nothing fetched');

        // ...and with nothing to show (the search failed, or found nothing) it searches again
        app.resetSearchResults();
        app.tryDiscogsAgain();
        assert.strictEqual(server.calledWith('/api/search').length, searches + 1);
    }

    // ---- clicking the recording already open keeps its work and searches nothing ----
    {
        const server = fakeServer();
        const app = mkApp();
        app.selectFile('f1');
        server.next('/api/search').answer(200, { results: [] });
        await tick(); await tick();
        const release = fullRelease(1);
        app.detectedTracks = detected(2);
        app.selectRelease(release);
        const searchesBefore = server.calledWith('/api/search').length;
        app.selectFile('f1');
        assert.strictEqual(app.selectedRelease, release, 'the chosen release stays');
        assert.strictEqual(app.detectedTracks.length, 2, 'the splits stay');
        assert.strictEqual(server.calledWith('/api/search').length, searchesBefore, 'no new search');
    }

    // ---- a file name that cleans to nothing: no search, and no notice about a query nobody typed ----
    {
        const server = fakeServer();
        const app = mkApp(['.wav']);
        app.cleanFilename = () => '';
        app.selectFile('f1');
        assert.strictEqual(server.calledWith('/api/search').length, 0);
        assert.strictEqual(app.searchNotice, '');
    }

    // ---- selecting a file starts the search from the cleaned filename, without blocking ----
    let server = fakeServer();
    let app = mkApp();
    app.selectFile('f1');
    assert.strictEqual(app.searchQuery, 'Pink Floyd Animals');
    assert.strictEqual(app.searchLoading, true, 'selectFile returns while the search runs');
    const search = server.calledWith('/api/search');
    assert.strictEqual(search.length, 1);
    assert.deepStrictEqual(search[0].body, { query: 'Pink Floyd Animals', max_results: 12 });

    // ---- trap 3: calling searchDiscogs again while it runs does not send a second request ----
    app.searchDiscogs();
    assert.strictEqual(server.calledWith('/api/search').length, 1);

    // ---- results arrive as cards without tracklists, then fill in two at a time ----
    server.next('/api/search').answer(200, { results: [card(1), card(2), card(3)] });
    await tick();
    assert.strictEqual(app.searchLoading, false);
    assert.deepStrictEqual(app.searchResults.map(r => r.tracks), [null, null, null]);
    assert.deepStrictEqual(server.calledWith('/api/release/').map(c => c.url), ['/api/release/1', '/api/release/2'], 'a limited number at a time');
    server.next('/api/release/1').answer(200, fullRelease(1));
    await tick();
    assert.deepStrictEqual(server.calledWith('/api/release/').map(c => c.url), ['/api/release/1', '/api/release/2', '/api/release/3']);
    assert.strictEqual(app.detailsSettled, false);
    server.next('/api/release/2').answer(200, fullRelease(2, [track('A1', ''), track('A2', '')]));
    server.next('/api/release/3').answer(500, { detail: 'boom' });
    await tick(); await tick();
    assert.deepStrictEqual(app.searchResults.map(r => r.detailStatus), ['loaded', 'loaded', 'error']);
    assert.strictEqual(app.searchResults[2].tracks, null);
    assert.strictEqual(app.detailsSettled, true);

    // ---- trap 4: a card whose tracklist failed is retried by the click, and selected only once loaded ----
    app.analyzing = true;   // keep the pick from also starting detection: this part tests selection only
    const failing = app.searchResults[2];
    const click = app.chooseResult(failing);
    assert.strictEqual(failing.opening, true);
    assert.strictEqual(app.selectedRelease, null, 'not selected while the tracklist is missing');
    server.next('/api/release/3').answer(500, { detail: 'still down' });
    await click;
    assert.strictEqual(app.selectedRelease, null, 'a failed load never selects tracks: null');
    assert.strictEqual(failing.detailStatus, 'error'); assert.strictEqual(failing.opening, false);
    const retry = app.chooseResult(failing);
    server.next('/api/release/3').answer(200, fullRelease(3));
    await retry;
    assert.strictEqual(app.selectedRelease.id, 3);
    assert.ok(Array.isArray(app.selectedRelease.tracks));
    app.selectedRelease = null; app.analyzing = false;
    app.selectRelease(card(9));      // no tracklist
    assert.strictEqual(app.selectedRelease, null, 'selectRelease refuses a release without tracks');
    app.selectRelease({ id: 9, tracks: undefined });
    assert.strictEqual(app.selectedRelease, null);

    // ---- trap 3: switching files mid-search drops the old file's replies (results and tracklists) ----
    server = fakeServer();
    app = mkApp(['First Record.wav', 'Second Record.wav']);
    app.selectFile('f1');
    const firstSearch = server.next('/api/search');
    app.selectFile('f2');                        // the first search is still waiting
    assert.strictEqual(app.searchLoading, true, 'the new file searched although the old search never answered');
    assert.deepStrictEqual(server.calledWith('/api/search').map(c => c.body.query), ['First Record', 'Second Record']);
    const secondSearch = server.next('/api/search');
    secondSearch.answer(200, { results: [card(7)] });
    await tick();
    assert.deepStrictEqual(app.searchResults.map(r => r.id), [7]);
    firstSearch.answer(200, { results: [card(1), card(2)] });   // late reply of the previous file
    await tick();
    assert.deepStrictEqual(app.searchResults.map(r => r.id), [7], 'a late reply must not show the old file\'s results');
    assert.strictEqual(app.searchLoading, false);
    assert.deepStrictEqual(server.calledWith('/api/release/').map(c => c.url), ['/api/release/7'], 'no tracklist loads for dropped results');

    // a tracklist reply of the previous search is dropped too, even for the same release id
    server = fakeServer();
    app = mkApp(['First Record.wav', 'Second Record.wav']);
    app.selectFile('f1');
    server.next('/api/search').answer(200, { results: [card(5)] });
    await tick();
    const oldTracklist = server.next('/api/release/5');
    app.selectFile('f2');
    server.next('/api/search').answer(200, { results: [card(5)] });
    await tick();
    const newTracklist = server.next('/api/release/5');   // its own request, not the old one's promise
    oldTracklist.answer(200, fullRelease(5, [track('A1', '1:00')]));
    await tick();
    assert.strictEqual(app.searchResults[0].tracks, null, 'the old reply did not fill the new card');
    newTracklist.answer(200, fullRelease(5, [track('A1', '9:00')]));
    await tick();
    assert.strictEqual(app.searchResults[0].tracks[0].duration, '9:00');
    assert.strictEqual(app.detailsSettled, true);

    // removing the open file also drops its running search
    server = fakeServer();
    app = mkApp();
    app.selectFile('f1');
    const orphan = server.next('/api/search');
    app.removeFile('f1', 5000);
    orphan.answer(200, { results: [card(1)] });
    await tick();
    assert.deepStrictEqual(app.searchResults, []);
    assert.strictEqual(app.searchLoading, false);
    app.dismissToast(app.toasts[0].id, 'dismiss');

    // ---- trap 5: no Discogs token -> no automatic search, no request ----
    server = fakeServer();
    app = mkApp();
    app.discogsConfigured = false;
    app.selectFile('f1');
    assert.strictEqual(server.calls.filter(c => c.url === '/api/search').length, 0);
    assert.strictEqual(app.searchLoading, false);
    assert.strictEqual(app.searchQuery, 'Pink Floyd Animals');
    const skip = app.analyzeFile();                 // "Skip Discogs" still works
    server.next('/api/analyze').answer(200, { tracks: detected(3) });
    await skip;
    assert.strictEqual(app.detectedTracks.length, 3);
    assert.strictEqual(server.calls.filter(c => c.url === '/api/search').length, 0, 'detecting does not start a search');

    // ---- traps 1 + 2: pick first, detect after -> mapping and mismatch come from the new tracks ----
    server = fakeServer();
    app = mkApp();
    app.selectFile('f1');
    server.next('/api/search').answer(200, { results: [] });
    await tick();
    const picked = fullRelease(11, [track('A1', '5:00'), track('A2', '5:00'), track('B1', '5:00')]);
    app.searchResults = [picked];
    app.chooseResult(picked);
    assert.strictEqual(app.selectedRelease, picked);
    assert.strictEqual(server.calledWith('/api/analyze').length, 1, 'picking a release with no tracks runs plain silence detection');
    assert.strictEqual(app.analyzing, true);
    assert.deepStrictEqual(app.customMapping, []);
    server.next('/api/analyze').answer(200, { tracks: detected(3) });
    await tick(); await tick();
    assert.strictEqual(app.analyzing, false);
    assert.strictEqual(app.selectedRelease, picked, 'detection must not wipe the chosen release');
    assert.deepStrictEqual(app.customMapping, [0, 1, 2], 'mapping recomputed when detection finishes');
    assert.strictEqual(app.trackCountMismatch, false);
    assert.strictEqual(server.calls.filter(c => c.url === '/api/search').length, 1, 'detection does not restart the search');
    assert.strictEqual(app.searchResults.length, 1);

    // fewer tracks than the release -> mismatch warning is computed from the detected tracks
    server = fakeServer();
    app = mkApp();
    app.selectFile('f1');
    server.next('/api/search').answer(200, { results: [] });
    await tick();
    app.selectRelease(picked);
    server.next('/api/analyze').answer(200, { tracks: detected(2) });
    await tick(); await tick();
    assert.deepStrictEqual(app.customMapping, [0, 1]);
    assert.strictEqual(app.trackCountMismatch, true);
    app.reverseMapping();
    assert.deepStrictEqual(app.customMapping, [1, 0], 'Reverse keeps working on the recomputed mapping');

    // a release picked while tracks already exist maps them at once and does not detect again
    const n = server.calls.length;
    app.changeRelease();
    assert.strictEqual(app.selectedRelease, null);
    assert.strictEqual(app.detectedTracks.length, 2, 'Change release keeps the detection');
    assert.strictEqual(app.searchResults.length, 0);   // (this app never loaded results; the next block checks they survive)
    app.selectRelease(picked);
    assert.deepStrictEqual(app.customMapping, [0, 1]);
    assert.strictEqual(app.trackCountMismatch, true);
    assert.strictEqual(server.calls.length, n, 'no second detection');

    // Change release keeps the search results for another pick
    app.searchResults = [picked, fullRelease(12)];
    app.changeRelease();
    assert.deepStrictEqual(app.searchResults.map(r => r.id), [11, 12]);

    // Smart Rescan after a pick keeps recomputing the mapping through the same helper
    app.selectRelease(picked);
    const rescan = app.smartRescan(3);
    server.next('/api/rescan').answer(200, { tracks: detected(3), matched: true });
    await rescan;
    assert.deepStrictEqual(app.customMapping, [0, 1, 2]);
    assert.strictEqual(app.trackCountMismatch, false);

    // ---- trap 6: detection fails after a pick -> usable screen ----
    server = fakeServer();
    app = mkApp();
    app.selectFile('f1');
    server.next('/api/search').answer(200, { results: [] });
    await tick();
    app.selectRelease(picked);
    server.next('/api/analyze').answer(500, { detail: 'ffmpeg exploded' });
    await tick(); await tick();
    assert.strictEqual(app.analyzing, false, 'the button is usable again');
    assert.strictEqual(app.selectedRelease, picked);
    assert.deepStrictEqual(app.detectedTracks, []);
    assert.strictEqual(app.trackCountMismatch, true, 'the mismatch card offers Smart Rescan / duration splitting');
    assert.ok(app.toasts.some(t => t.type === 'error'));
    app.analyzeFile();                                  // "Retry track detection"
    server.next('/api/analyze').answer(200, { tracks: detected(3) });
    await tick(); await tick();
    assert.deepStrictEqual(app.customMapping, [0, 1, 2]);
    assert.strictEqual(app.trackCountMismatch, false);

    // ...and detection that finds nothing
    server = fakeServer();
    app = mkApp();
    app.selectFile('f1');
    server.next('/api/search').answer(200, { results: [] });
    await tick();
    app.selectRelease(picked);
    server.next('/api/analyze').answer(200, { tracks: [] });
    await tick(); await tick();
    assert.strictEqual(app.analyzing, false);
    assert.strictEqual(app.selectedRelease, picked);
    assert.strictEqual(app.trackCountMismatch, true);
    assert.ok(app.toasts.some(t => t.type === 'warning'), 'says why nothing is shown');

    // ---- re-analyze keeps the search results and does not search again ----
    server = fakeServer();
    app = mkApp();
    app.selectFile('f1');
    server.next('/api/search').answer(200, { results: [card(1)] });
    await tick();
    server.next('/api/release/1').answer(200, fullRelease(1));
    await tick();
    app.detectedTracks = detected(2).map(t => ({ ...t, editing: false, ignored: false }));
    const reanalysis = app.reanalyzeFile();
    server.next('/api/analyze').answer(200, { tracks: detected(4) });
    await reanalysis;
    assert.strictEqual(app.detectedTracks.length, 4);
    assert.deepStrictEqual(app.searchResults.map(r => r.id), [1]);
    assert.strictEqual(server.calls.filter(c => c.url === '/api/search').length, 1);

    // ---- switching recordings waits for a running detection ----
    server = fakeServer();
    app = mkApp(['One.wav', 'Two.wav']);
    app.selectFile('f1');
    server.next('/api/search').answer(200, { results: [] });
    await tick();
    app.selectRelease(picked);                         // starts detection
    app.selectFile('f2');
    assert.strictEqual(app.currentFileId, 'f1', 'still on the recording being analyzed');
    assert.ok(app.toasts.some(t => t.type === 'warning'));
    server.next('/api/analyze').answer(200, { tracks: detected(3) });
    await tick(); await tick();
    app.selectFile('f2');
    assert.strictEqual(app.currentFileId, 'f2');
    assert.strictEqual(app.selectedRelease, null);

    // ---- the music span for the fit indicator: GET /api/scan and the scan_complete message ----
    server = fakeServer();
    app = mkApp();
    app.uploadedFiles[0].musicDuration = null;
    app.selectFile('f1');
    server.next('/api/search').answer(200, { results: [] });
    const scan = server.next('/api/scan/f1');
    scan.answer(200, { ready: true, music_start: 4, music_end: 704, music_duration: 700 });
    await tick();
    assert.strictEqual(app.currentFile.musicDuration, 700);
    app.handleWebSocketMessage({ type: 'scan_complete', file_id: 'f1', music_start: 10, music_end: 500, music_duration: 490 });
    assert.strictEqual(app.currentFile.musicDuration, 490);
    app.handleWebSocketMessage({ type: 'scan_complete', file_id: 'gone', music_start: 0, music_end: 5 });   // unknown file: ignored
    app.fetchScan('f1');
    server.next('/api/scan/f1').answer(200, { ready: false });
    await tick();
    assert.strictEqual(app.currentFile.musicDuration, 490, 'a scan that is not ready changes nothing');

    // ---- every name the new cards use in index.html exists on the Alpine object ----
    const blocks = ['id="discogs-search"', 'id="release-card"'].map((marker) => {
        const start = html.indexOf(marker);
        assert.ok(start > 0, marker);
        return html.slice(start, html.indexOf('<!-- Waveform Visualization', start));
    }).join('\n');
    const expressions = [...blocks.matchAll(/\s(?:x-[\w:.-]+|@[\w:.-]+|:[\w-]+)="([^"]*)"/g)].map(m => m[1]);
    const keywords = new Set(['true', 'false', 'null', 'undefined', 'Math', 'result', 'event', '$event', 'this', 'x']);
    const fresh = vinylApp();
    const unknown = new Set();
    for (const expression of expressions) {
        const code = expression
            .replace(/'[^']*'/g, "''")
            .replace(/`([^`]*)`/g, (_, body) => [...body.matchAll(/\$\{([^}]*)\}/g)].map(m => m[1]).join(' + '));
        for (const m of code.matchAll(/(^|[^\w.$])([A-Za-z_]\w*)/g)) {
            const name = m[2];
            if (!keywords.has(name) && !(name in fresh)) unknown.add(name);
        }
    }
    // x-for="result in filteredResults" declares `result`; `in` is not a name
    unknown.delete('in');
    assert.deepStrictEqual([...unknown], [], 'index.html uses names the Alpine object lacks');

    console.log('discogs-first ok');
})().catch((error) => { console.error(error); process.exit(1); });
