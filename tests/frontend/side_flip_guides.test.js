const fs = require('fs');
const path = require('path');
const assert = require('assert');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + '; return vinylApp;')();

(async () => {
    const app = vinylApp();
    app.currentFileId = 'recording';
    app.currentFile = { id: 'recording', duration: 466 };
    app.uploadedFiles = [app.currentFile];
    app.selectedRelease = { id: 1, tracks: ['A1', 'A2', 'B1', 'B2', 'C1'].map(position => ({ position, duration: '1:40' })) };
    app.selectedSides = ['A', 'B'];
    app.initWaveform = async () => {};
    app.$nextTick = async () => {};
    app.$refs = {};   // Alpine provides it; redetectForTargets pauses the preview player through it
    const requests = [];
    global.fetch = async (url, options) => {
        assert.strictEqual(url, '/api/analyze-guided');
        requests.push(JSON.parse(options.body));
        return { ok: true, json: async () => ({ method: 'durations', tracks: [], boundaries: [] }) };
    };
    await app.redetectForTargets();
    assert.deepStrictEqual(requests[0].side_breaks, [2]);
    assert.deepStrictEqual(requests[0].durations, [100, 100, 100, 100]);
    app.selectedSides = ['B'];
    await app.redetectForTargets();
    assert.deepStrictEqual(requests[1].side_breaks, []);
    app.selectedRelease.tracks = ['1', '2', '3'].map(position => ({ position, duration: '1:40' }));
    app.selectedSides = [''];
    await app.redetectForTargets();
    assert.deepStrictEqual(requests[2].side_breaks, [], 'numeric positions do not identify vinyl flips');
})().catch(error => { console.error(error); process.exit(1); });
