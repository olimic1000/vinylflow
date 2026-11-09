// An imported file changed on disk (409): analyze, export and the waveform show the server's
// reason as an error toast, not a generic failure.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

const DETAIL = 'side.wav changed on disk since it was imported. Import it again to split the new recording.';
global.window = global.window || {};
global.fetch = async () => ({ ok: false, status: 409, json: async () => ({ detail: DETAIL }) });
global.WaveSurfer = {
    create: () => ({ on() {}, registerPlugin: () => ({}), destroy() {} }),
    Regions: { create: () => ({}) },
};

function mkApp() {
    const a = vinylApp();
    a.uploadedFiles = [{ id: 'f1', filename: 'side.wav', status: 'uploaded', in_place: true }];
    a.currentFileId = 'f1';
    a.currentFile = a.uploadedFiles[0];
    return a;
}

function lastError(a) {
    const errors = a.toasts.filter(t => t.type === 'error');
    assert.strictEqual(errors.length, 1, 'one error toast');
    return errors[0].message;
}

(async () => {
    let a = mkApp();
    await a.analyzeFile();
    assert.strictEqual(lastError(a), `Analysis failed: ${DETAIL}`);

    a = mkApp();
    a.selectedRelease = { id: 1, tracks: [{ position: 'A1' }] };
    a.detectedTracks = [{ number: 1, start: 0, end: 60, duration: 60, ignored: false }];
    a.customMapping = { 0: 0 };
    await a.processFile();
    assert.strictEqual(lastError(a), `Processing failed: ${DETAIL}`);
    assert.strictEqual(a.isProcessing, false);
    assert.strictEqual(a.uploadedFiles[0].status, 'uploaded', 'status restored');

    a = mkApp();
    await a.initWaveform();
    assert.strictEqual(lastError(a), `Failed to load the waveform: ${DETAIL}`);
    assert.strictEqual(a.waveformLoading, false);

    console.log('source_changed: ok');
})().catch(e => { console.error(e); process.exit(1); });
