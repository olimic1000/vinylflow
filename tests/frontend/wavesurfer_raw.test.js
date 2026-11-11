// WaveSurfer objects stay out of Alpine's reactive proxies.  Through a proxy, WaveSurfer's
// identity checks fail: a removed region compares proxy with raw and stays in the plugin's list,
// so the next remove() throws.  In the app, Change release -> pick another -> Snap to Discogs
// aborted halfway ("this.element is null") and the track regions disappeared.  Alpine skips
// objects flagged __v_skip; this checks the flag, the browser check proves the effect.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

global.window = global.window || {};
// Peaks never arrive: initWaveform stops after creating the waveform and its regions plugin.
global.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: 'stop here' }) });
let created = null, regions = null;
global.WaveSurfer = {
    create: () => (created = { on() {}, registerPlugin: () => (regions = { on() {} }), destroy() {}, getMediaElement: () => null }),
    Regions: { create: () => ({}) },
};

(async () => {
    const app = vinylApp();
    app.uploadedFiles = [{ id: 'f1', filename: 'side.wav', status: 'analyzed' }];
    app.currentFileId = 'f1';
    app.currentFile = app.uploadedFiles[0];
    await app.initWaveform();
    assert.ok(created && regions, 'the fake waveform and regions plugin were created');
    assert.strictEqual(created.__v_skip, true, 'waveform marked raw');
    assert.strictEqual(regions.__v_skip, true, 'regions plugin marked raw');
    assert.ok(!Object.keys(created).includes('__v_skip'), 'non-enumerable: never serialised or iterated');
    console.log('wavesurfer_raw: ok');
})().catch(error => { console.error(error); process.exit(1); });
