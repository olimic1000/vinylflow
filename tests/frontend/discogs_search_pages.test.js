// Discogs search: 12 results per fetch, "Load more" adds the next 12, at most 3 fetches per query.
let finished = false;   // the runner judges exit codes only: a test stuck on an unanswered fake fetch must not pass
process.on('exit', () => { if (!finished) { console.error('discogs_search_pages.test.js did not finish (an unexpected request is still pending)'); process.exitCode = 1; } });
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + ';return vinylApp;')();
const cases = [];
const test = (name, run) => cases.push({ name, run });
const settle = async app => {
    for (let turn = 0; turn < 1000 && !app.detailsSettled; turn++) await new Promise(resolve => setImmediate(resolve));
};

/**
 * Fake server. /api/search pages like the real endpoint: has_more for a full page below
 * the 3-page limit. Pages overlap by one result (Discogs ordering can shift between
 * requests), which must not produce a duplicate card. `total` = Discogs matches.
 * `scriptedSearch(page)` returns the exact reply body, for replies the paging model can't produce
 * (has_more true on the last page we may fetch, or a full page of already shown ids).
 */
function fakeServer({ total = 100, gate = null, searchGate = null, releaseGate = null, scriptedSearch = null } = {}) {
    const searches = [], releases = [];
    global.fetch = async (url, options = {}) => {
        if (url === '/api/search') {
            const { query, max_results, page } = JSON.parse(options.body);
            searches.push({ query, max_results, page });
            if (gate) await gate.promise;
            if (searchGate) await searchGate(query, page);   // per-call gating: replies can be answered out of order
            if (scriptedSearch) return { ok: true, json: async () => scriptedSearch(page) };   // exact reply, bypassing the paging model
            const first = page === 1 ? 1 : (page - 1) * max_results;   // later pages start one early
            const ids = [];
            for (let id = first; id < first + max_results && id <= total; id++) ids.push(id);
            const results = ids.map(id => ({ id, artist: 'Artist', title: `Pressing ${id}` }));
            return { ok: true, json: async () => ({ results, has_more: page < 3 && ids.length === max_results }) };
        }
        releases.push(url);
        const id = Number(url.split('/').pop());
        if (releaseGate) await releaseGate(id);
        return { ok: true, json: async () => ({ id, tracks: [{ position: 'A1', title: 'Song', duration: '3:00' }] }) };
    };
    return { searches, releases };
}
async function searched(options) {
    const server = fakeServer(options);
    const app = vinylApp();
    app.searchQuery = 'Artist Album';
    await app.searchDiscogs();
    await settle(app);
    return { app, server };
}

test('a search shows 12 results and loads their lengths', async () => {
    const { app, server } = await searched();
    assert.deepStrictEqual(server.searches, [{ query: 'Artist Album', max_results: 12, page: 1 }]);
    assert.strictEqual(app.searchResults.length, 12);
    assert.strictEqual(server.releases.length, 12);
    assert.strictEqual(app.searchHasMore, true);
});

test('Load more adds the next 12, fetching lengths only for the new ones, and stops after 3 fetches', async () => {
    const { app, server } = await searched();
    await app.loadMoreResults(); await settle(app);
    assert.strictEqual(server.searches[1].page, 2);
    assert.strictEqual(app.searchResults.length, 23, 'the result repeated from page 1 is not shown twice');
    assert.strictEqual(new Set(app.searchResults.map(result => result.id)).size, 23);
    assert.strictEqual(server.releases.length, 23, 'no release page fetched twice');
    assert.ok(app.searchResults.every(result => result.detailStatus === 'loaded'));
    assert.strictEqual(app.searchHasMore, true);
    await app.loadMoreResults(); await settle(app);
    assert.strictEqual(server.searches[2].page, 3);
    assert.strictEqual(app.searchHasMore, false, 'third fetch is the last');
    await app.loadMoreResults();
    assert.strictEqual(server.searches.length, 3, 'never a fourth fetch');
});

test('no Load more when Discogs has no further results', async () => {
    const { app, server } = await searched({ total: 7 });
    assert.strictEqual(app.searchResults.length, 7);
    assert.strictEqual(app.searchHasMore, false);
    await app.loadMoreResults();
    assert.strictEqual(server.searches.length, 1);
});

/** A promise whose resolution the test controls. */
function deferred() {
    let open;
    const promise = new Promise(resolve => { open = resolve; });
    return { promise, open };
}

test('a Load more answered after a new search is dropped', async () => {
    const { app } = await searched();
    // Two gated replies: the NEW search is answered first, the OLD Load more (page 2) second.
    const moreGate = deferred(), freshGate = deferred();
    const server = fakeServer({ searchGate: query => (query === 'Artist Album' ? moreGate : freshGate).promise });
    const more = app.loadMoreResults();
    app.searchQuery = 'Another Album';
    const fresh = app.searchDiscogs();
    freshGate.open(); await fresh; await settle(app);
    const releaseFetchesBefore = server.releases.length;
    moreGate.open(); await more;
    for (let turn = 0; turn < 50; turn++) await new Promise(resolve => setImmediate(resolve));
    assert.deepStrictEqual(app.searchResults.map(result => result.id), Array.from({ length: 12 }, (_, i) => i + 1),
        'exactly the new search\'s results');
    assert.strictEqual(app.searchPage, 1);
    assert.strictEqual(app.searchedQuery, 'Another Album');
    assert.strictEqual(server.releases.length, releaseFetchesBefore, 'no tracklist fetched for the old page 2');
});

test('Load more continues the searched query, not the edited box', async () => {
    const { app, server } = await searched();
    app.searchQuery = 'something typed afterwards';
    await app.loadMoreResults();
    assert.deepStrictEqual(server.searches[1], { query: 'Artist Album', max_results: 12, page: 2 });
});

test('the details stay unsettled while page 2 tracklists load, then settle', async () => {
    const { app } = await searched();
    assert.strictEqual(app.detailsSettled, true);
    const gate = deferred();
    fakeServer({ releaseGate: id => (id > 12 ? gate.promise : null) });
    await app.loadMoreResults();
    assert.strictEqual(app.detailsSettled, false, 'new cards are still loading');
    gate.open(); await settle(app);
    assert.strictEqual(app.detailsSettled, true);
    assert.ok(app.searchResults.every(result => result.detailStatus === 'loaded'));
});

test('Load more beside the first page\'s loader does not settle early', async () => {
    // Page 1's last card is held; page 2's loader finishes first and must not declare "settled".
    const hold = deferred();
    const server = fakeServer({ releaseGate: id => (id === 12 ? hold.promise : null) });
    const app = vinylApp();
    app.searchQuery = 'Artist Album';
    await app.searchDiscogs();
    for (let turn = 0; turn < 50; turn++) await new Promise(resolve => setImmediate(resolve));
    await app.loadMoreResults();
    for (let turn = 0; turn < 200; turn++) await new Promise(resolve => setImmediate(resolve));
    assert.strictEqual(app.searchResults.length, 23);
    assert.strictEqual(app.searchResults.find(result => result.id === 23).detailStatus, 'loaded');
    assert.strictEqual(app.detailsSettled, false, 'card 12 is still pending');
    hold.open(); await settle(app);
    assert.strictEqual(app.detailsSettled, true);
    assert.ok(server.releases.length >= 23);
});

test('a Load more that adds nothing says so', async () => {
    const { app } = await searched({ total: 12 });   // page 2 only repeats result 12
    assert.strictEqual(app.searchHasMore, true);
    await app.loadMoreResults();
    assert.strictEqual(app.searchResults.length, 12);
    assert.strictEqual(app.searchHasMore, false);
    assert.strictEqual(app.searchMoreNotice, 'No more results.');
    assert.strictEqual(app.searchMoreError, '');
    app.resetSearchResults();
    assert.strictEqual(app.searchMoreNotice, '');
});

// Page of ids first..first+11, as the real endpoint's cards.
const pageOf = first => Array.from({ length: 12 }, (_, offset) => ({ id: first + offset, artist: 'Artist', title: `Pressing ${first + offset}` }));

test('a Load more whose page only repeats shown results, with more pages behind it, keeps Load more and says so', async () => {
    // Page 2 is full (has_more stays true) but every id on it was on page 1.
    const { app } = await searched({ scriptedSearch: () => ({ results: pageOf(1), has_more: true }) });
    await app.loadMoreResults();
    assert.strictEqual(app.searchResults.length, 12);
    assert.strictEqual(app.searchHasMore, true, 'page 3 may still have something');
    assert.strictEqual(app.searchMoreNotice, 'No new results on this page.');
});

test('the client stops at the page limit even when the server says there is more', async () => {
    const { app, server } = await searched({ scriptedSearch: page => ({ results: pageOf(page * 100), has_more: true }) });
    await app.loadMoreResults(); await settle(app);
    assert.strictEqual(app.searchHasMore, true);
    await app.loadMoreResults(); await settle(app);
    assert.strictEqual(server.searches.length, 3);
    assert.strictEqual(app.searchHasMore, false, 'no Load more after the third fetch');
});

/** An app with file f1 open, ready for removeFile / Undo. */
function appWithOpenFile(app) {
    app.uploadedFiles = [{ id: 'f1', filename: 'a.wav', status: 'analyzed', duration: 600 }];
    app.currentFileId = 'f1'; app.currentFile = app.uploadedFiles[0];
    app.detectedTracks = [{ number: 1, start: 0, end: 300, duration: 300, ignored: false }];
    app.fetchScan = app.destroyWaveform = app.initWaveform = () => {};
    app.notifyUndo = () => 1;
    return app;
}

test('a card pick still loading is dropped when its file is removed and Undone', async () => {
    const hold = deferred();
    fakeServer({ releaseGate: id => (id === 12 ? hold.promise : null) });
    const app = appWithOpenFile(vinylApp());
    app.searchQuery = 'Artist Album';
    await app.searchDiscogs();
    for (let turn = 0; turn < 50; turn++) await new Promise(resolve => setImmediate(resolve));
    const selected = [];
    app.selectRelease = release => selected.push(release);
    const card = app.searchResults.find(result => result.id === 12);
    assert.strictEqual(card.tracks, null, 'its tracklist is still loading');
    const pick = app.chooseResult(card);
    app.removeFile('f1', 5000);
    assert.strictEqual(app._pendingPickId, null);
    await app.undoRemoveFile('f1');
    hold.open(); await pick; await settle(app);
    assert.deepStrictEqual(selected, [], 'the reply of a pick made before the removal selects nothing');
});

test('a pasted-link lookup still loading is dropped when its file is removed and Undone', async () => {
    // The same file is current again after Undo, so only the cleared pending pick can drop the reply.
    const hold = deferred();
    global.fetch = async () => { await hold.promise; return { ok: true, status: 200, json: async () => ({ id: 7, tracks: [{ position: 'A1', title: 'S', duration: '3:00' }] }) }; };
    const app = appWithOpenFile(vinylApp());
    const selected = [];
    app.selectRelease = release => selected.push(release);
    app.releaseIdInput = '7';
    const lookup = app.openReleaseById();
    app.removeFile('f1', 5000);
    await app.undoRemoveFile('f1');
    assert.strictEqual(app.currentFileId, 'f1');
    hold.open(); await lookup;
    assert.deepStrictEqual(selected, []);
});

test('a superseded pasted lookup finishing late does not stop the loading state of the newer one', async () => {
    const gates = { 1: deferred(), 2: deferred() };
    global.fetch = async url => {
        const id = Number(url.split('/').pop());
        await gates[id].promise;
        return { ok: true, status: 200, json: async () => ({ id, tracks: [{ position: 'A1', title: 'S', duration: '3:00' }] }) };
    };
    const app = vinylApp();
    app.currentFileId = 'f1';
    app.selectRelease = () => {};
    app.releaseIdInput = '1';
    const first = app.openReleaseById();
    assert.strictEqual(app.releaseIdLoading, true);
    app.changeRelease();   // supersedes lookup 1 and resets the loading flag
    assert.strictEqual(app.releaseIdLoading, false);
    app.releaseIdInput = '2';
    const second = app.openReleaseById();
    assert.strictEqual(app.releaseIdLoading, true);
    gates[1].open(); await first;
    assert.strictEqual(app.releaseIdLoading, true, 'lookup 2 is still running');
    gates[2].open(); await second;
    assert.strictEqual(app.releaseIdLoading, false);
});

test('Load more does nothing once the page limit is reached', async () => {
    const { app, server } = await searched();
    app.searchHasMore = true; app.searchPage = 3;   // a stale "more" flag must not fetch a fourth page
    await app.loadMoreResults();
    assert.strictEqual(server.searches.length, 1, 'no further fetch');
});

test('Undo of removing the open file brings back the Load more state', async () => {
    const { app, server } = await searched();
    await app.loadMoreResults(); await settle(app);
    app.uploadedFiles = [{ id: 'f1', filename: 'a.wav', status: 'analyzed', duration: 600 }];
    app.currentFileId = 'f1'; app.currentFile = app.uploadedFiles[0];
    app.detectedTracks = [{ number: 1, start: 0, end: 300, duration: 300, ignored: false }];
    app.fetchScan = app.destroyWaveform = app.initWaveform = () => {};
    app.notifyUndo = () => 1;
    app.removeFile('f1', 5000);
    assert.strictEqual(app.searchPage, 0);
    await app.undoRemoveFile('f1');
    await settle(app);
    assert.strictEqual(app.searchPage, 2);
    assert.strictEqual(app.searchHasMore, true);
    assert.strictEqual(app.searchedQuery, 'Artist Album');
    app.searchQuery = 'edited meanwhile';
    await app.loadMoreResults();
    assert.deepStrictEqual(server.searches[server.searches.length - 1], { query: 'Artist Album', max_results: 12, page: 3 });
});

test('switching recordings forgets Load more with the results', async () => {
    const { app } = await searched();
    app.uploadedFiles = [{ id: 'one', filename: 'one.wav' }, { id: 'two', filename: 'two.wav' }];
    app.currentFileId = 'one'; app.currentFile = app.uploadedFiles[0];
    app.discogsConfigured = false;   // no automatic search for the next recording
    app.fetchScan = app.destroyWaveform = () => {};
    app.selectFile('two');
    assert.deepStrictEqual(app.searchResults, []);
    assert.strictEqual(app.searchHasMore, false);
});

(async () => {
    let failed = 0;
    for (const { name, run } of cases) {
        try { await run(); console.log(`ok - ${name}`); } catch (error) { failed++; console.error(`${name}:`, error); }
    }
    assert.strictEqual(failed, 0, `${failed} Discogs search paging regressions failed`);
    finished = true;
})().catch(error => { console.error(error); process.exit(1); });
