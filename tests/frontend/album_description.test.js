// Album description (Wikipedia) preview: when it is fetched, the stale-response guard, the display
// gate, Retry, and the export request. The browser only ever talks to our own backend.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + ';return vinylApp;')();

const release = id => ({ id, artist: 'A', title: 'T', year: 1970, label: 'L', tracks: [{ position: 'A1', title: 'One', duration: '3:00' }] });

/** An app whose fetch is a fake: `replies` maps a URL to a function returning a promise of a body. */
function mk({ enabled = true } = {}) {
    const app = vinylApp();
    app.$nextTick = async () => {};
    app.config.album_description = enabled;
    app.fetches = [];
    app.replies = {};
    global.fetch = async url => {
        app.fetches.push(url);
        const body = await (app.replies[url] || (async () => ({ status: 'none' })))();
        return { ok: body.httpStatus ? false : true, status: body.httpStatus || 200, json: async () => body };
    };
    // Isolate from detection: the description feature must never depend on it.
    app.analyzeFile = () => Promise.resolve();
    return app;
}
const url = id => `/api/release/${id}/description`;
const found = { status: 'found', text: 'An album.', title: 'T (album)', url: 'https://en.wikipedia.org/wiki/T_(album)' };

(async () => {
    // selectRelease fetches once when on; off -> no fetch
    let a = mk();
    a.replies[url(5)] = async () => found;
    a.selectRelease(release(5));
    await new Promise(r => setImmediate(r));
    assert.deepStrictEqual(a.fetches, [url(5)]);
    assert.strictEqual(a.albumDescription.status, 'found');
    assert.strictEqual(a.albumDescription.releaseId, 5);
    assert.strictEqual(a.albumDescription.text, 'An album.');
    assert.strictEqual(a.albumDescription.url, found.url);

    a = mk({ enabled: false });
    a.selectRelease(release(5));
    await new Promise(r => setImmediate(r));
    assert.deepStrictEqual(a.fetches, [], 'setting off: no request at all');
    assert.notStrictEqual(a.albumDescription.status, 'loading');

    // stale guard and display gate
    a = mk();
    let releaseSlowReply;
    a.replies[url(1)] = () => new Promise(resolve => { releaseSlowReply = () => resolve({ status: 'found', text: 'Album ONE', title: 'One', url: 'u1' }); });
    a.replies[url(2)] = async () => ({ status: 'found', text: 'Album TWO', title: 'Two', url: 'u2' });
    a.selectedRelease = release(1);
    const slow = a.loadAlbumDescription();
    a.selectedRelease = release(2);
    await a.loadAlbumDescription();
    assert.strictEqual(a.albumDescription.text, 'Album TWO');
    releaseSlowReply();
    await slow;
    assert.strictEqual(a.albumDescription.text, 'Album TWO', "A's late reply leaves B's state alone");
    assert.strictEqual(a.albumDescription.releaseId, 2);
    assert.strictEqual(a.albumDescriptionVisible, true);
    a.selectedRelease = release(3);
    assert.strictEqual(a.albumDescriptionVisible, false, 'state for another release id is hidden');
    a.selectedRelease = null;
    assert.strictEqual(a.albumDescriptionVisible, false);

    // A reply that lands after the setting was switched off (Settings Save resets to idle) stays dropped
    a = mk();
    let releaseLateReply;
    a.replies[url(4)] = () => new Promise(resolve => { releaseLateReply = () => resolve(found); });
    a.selectedRelease = release(4);
    const late = a.loadAlbumDescription();
    a.config.album_description = false;
    await a.loadAlbumDescription();
    releaseLateReply();
    await late;
    assert.strictEqual(a.albumDescription.status, 'idle', 'switched off while loading: the late reply is ignored');
    assert.strictEqual(a.albumDescriptionVisible, false);

    // errors: a failed fetch or a 404 reads as error; disabled hides
    a = mk();
    a.replies[url(4)] = async () => ({ httpStatus: 404, detail: 'nope' });
    a.selectedRelease = release(4);
    await a.loadAlbumDescription();
    assert.strictEqual(a.albumDescription.status, 'error');
    a.replies[url(4)] = async () => ({ status: 'disabled' });
    await a.loadAlbumDescription();
    assert.strictEqual(a.albumDescription.status, 'disabled');
    global.fetch = async () => { throw new Error('offline'); };
    await a.loadAlbumDescription();
    assert.strictEqual(a.albumDescription.status, 'error');

    // Retry after error refetches
    a = mk();
    let attempt = 0;
    a.replies[url(6)] = async () => (++attempt === 1 ? { status: 'error' } : found);
    a.selectedRelease = release(6);
    await a.loadAlbumDescription();
    assert.strictEqual(a.albumDescription.status, 'error');
    await a.loadAlbumDescription();
    assert.strictEqual(a.albumDescription.status, 'found');
    assert.strictEqual(a.fetches.length, 2);

    // show more / less is local view state, reset for a new lookup
    a.albumDescriptionExpanded = true;
    a.selectedRelease = release(8);
    a.replies[url(8)] = async () => found;
    await a.loadAlbumDescription();
    assert.strictEqual(a.albumDescriptionExpanded, false);

    // The export request leaves the choice to the server's saved setting: a box ticked in Settings
    // and then cancelled (config edited in place, never saved) must not turn the lookups on.
    const stubFetch = () => {
        const sent = [];
        global.fetch = async (u, options) => {
            sent.push({ u, body: options && options.body ? JSON.parse(options.body) : null });
            // No job_id: processFile stops right after sending, so no polling timer is started.
            return { ok: false, status: 400, json: async () => ({ detail: 'stub' }) };
        };
        return sent;
    };
    for (const enabled of [true, false, undefined]) {
        const app = mk({ enabled });
        if (enabled === undefined) delete app.config.album_description;
        app.selectedRelease = release(5);
        app.currentFileId = 'f1';
        app.uploadedFiles = [{ id: 'f1', filename: 'one.wav', duration: 400 }];
        app.currentFile = app.uploadedFiles[0];
        app.detectedTracks = [{ number: 1, start: 0, end: 100, ignored: false }];
        app.customMapping = [0];
        const sent = stubFetch();
        app.notifyError = () => {};
        const consoleError = console.error;
        console.error = () => {};  // the stubbed failure is logged by processFile
        await app.processFile();
        console.error = consoleError;
        const request = sent.find(entry => entry.u === '/api/process');
        assert.ok(request, 'export request sent');
        assert.ok(!('album_description' in request.body), 'no album_description in the export request');
    }

    // An Undo that puts back another release shows that release's preview
    {
        const app = mk({ enabled: true });
        app.selectedRelease = release(5);
        app.replies[url(5)] = async () => found;
        await app.loadAlbumDescription();
        const snapshot = { ...app.snapshotEdits(), selectedRelease: release(6) };
        app.replies[url(6)] = async () => ({ ...found, title: 'Six' });
        app.restoreEdits(snapshot);
        await new Promise(resolve => setImmediate(resolve));
        assert.strictEqual(app.albumDescription.releaseId, 6);
        assert.strictEqual(app.albumDescriptionVisible, true);
    }

    console.log('album description ok');
})().catch(error => { console.error(error); process.exit(1); });
