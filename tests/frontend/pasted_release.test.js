// A release opened by pasted link/number is a pick like a card click: the latest pick wins, and a
// reply still loading never selects on another recording or after the open file was removed.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + ';return vinylApp;')();

const track = (position, duration) => ({ position, title: `Song ${position}`, duration });
const fullRelease = (id) => ({ id, artist: 'Artist', title: `Pressing ${id}`, tracks: [track('A1', '5:00'), track('A2', '6:00')] });

/** Release fetches stay pending until the test answers them, so a lookup can be left "loading". */
function fakeServer() {
    const pending = {};
    global.fetch = (url) => {
        assert.ok(/^\/api\/release\/\d+$/.test(url), `unexpected request ${url} (tests must never reach Discogs)`);
        return new Promise(resolve => { pending[url.split('/').pop()] = resolve; });
    };
    return { answer: (id) => pending[id]({ ok: true, status: 200, json: async () => fullRelease(id) }) };
}

function mkApp() {
    const app = vinylApp();
    app.$nextTick = callback => callback?.();
    app.uploadedFiles = [{ id: 'f1', filename: 'side.wav', status: 'analyzed', duration: 600 }];
    app.currentFileId = 'f1';
    app.currentFile = app.uploadedFiles[0];
    // Detection / waveform side effects of a pick are not under test here.
    app.analyzeFile = async () => {};
    app.fetchScan = () => {};
    app.destroyWaveform = app.pauseWaveform = app.initWaveform = () => {};
    app.searchDiscogs = async () => {};
    app.notify = () => 1;
    app.notifyUndo = () => 1;
    return app;
}

const failures = [];
async function scenario(name, body) {
    try { await body(); console.log(`ok - ${name}`); } catch (error) { failures.push(name); console.error(`FAIL ${name}: ${error.stack}`); }
}

(async () => {
    await scenario('a card picked while a pasted release loads wins', async () => {
        const server = fakeServer();
        const app = mkApp();
        app.searchResults = [{ ...fullRelease(2), detailStatus: 'loaded' }];
        app.releaseIdInput = '7';
        const byId = app.openReleaseById();
        app.chooseResult(app.searchResults[0]);
        assert.strictEqual(app.selectedRelease.id, 2);
        server.answer(7);
        await byId;
        assert.strictEqual(app.selectedRelease.id, 2, 'the pasted release does not replace the later card pick');
        assert.strictEqual(app.releaseIdLoading, false);
    });

    await scenario('Change release while a pasted release loads drops its reply', async () => {
        const server = fakeServer();
        const app = mkApp();
        app.releaseIdInput = '7';
        const byId = app.openReleaseById();
        app.changeRelease();
        server.answer(7);
        await byId;
        assert.strictEqual(app.selectedRelease, null);
        assert.strictEqual(app.releaseIdLoading, false);
    });

    await scenario('Change release frees the paste button at once, before the fetch returns', async () => {
        fakeServer();
        const app = mkApp();
        app.releaseIdInput = '7';
        app.openReleaseById();
        assert.strictEqual(app.releaseIdLoading, true);
        app.changeRelease();
        assert.strictEqual(app.releaseIdLoading, false);
    });

    await scenario('a pasted release still loading never selects on another recording', async () => {
        const server = fakeServer();
        const app = mkApp();
        app.uploadedFiles.push({ id: 'f2', filename: 'other.wav', status: 'uploaded', duration: 600 });
        app.releaseIdInput = '7';
        const byId = app.openReleaseById();
        app.selectFile('f2');
        assert.strictEqual(app.releaseIdLoading, false, 'the new recording can paste a release at once');
        server.answer(7);
        await byId;
        assert.strictEqual(app.selectedRelease, null, 'release 7 is not selected on f2');
    });

    await scenario('removing the open recording drops a pasted release still loading', async () => {
        const server = fakeServer();
        const app = mkApp();
        app.releaseIdInput = '7';
        const byId = app.openReleaseById();
        app.removeFile('f1');
        assert.strictEqual(app.currentFileId, null);
        assert.strictEqual(app.releaseIdLoading, false);
        server.answer(7);
        await byId;
        assert.strictEqual(app.selectedRelease, null, 'no release is selected with no recording open');
    });

    if (failures.length) { console.error(`${failures.length} scenario(s) failed`); process.exit(1); }
    console.log('pasted_release ok');
})().catch(error => { console.error(error); process.exit(1); });
