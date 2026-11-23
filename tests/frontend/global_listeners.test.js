// The page calls init() twice (Alpine runs it, and index.html has x-init="init()" too): every document
// listener for Space and pointer-focus tracking must still be registered once.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

let finished = false;
process.on('exit', () => { if (!finished) { console.error('global_listeners: async part did not finish'); process.exitCode = 1; } });

(async () => {
    const registered = [];
    global.document = { addEventListener: (name, fn, capture) => registered.push(name + (capture ? ':capture' : '')) };
    global.window = { addEventListener() {} };
    const app = vinylApp();
    app.loadConfig = app.loadFormats = app.checkStatus = app.loadImportList = async () => {};
    app.connectWebSocket = app.detectDesktopPicker = app.chooseDefaultImportTab = () => {};
    app.$watch = () => {};
    for (let call = 0; call < 2; call++) {
        try { await app.init(); } catch (error) { /* the rest of init is not under test */ }
    }
    for (const name of ['keydown', 'keyup', 'pointerdown:capture', 'pointerup:capture', 'pointercancel:capture', 'focusin:capture']) {
        assert.strictEqual(registered.filter(entry => entry === name).length, 1, `${name} registered once`);
    }
    finished = true;
    console.log('global_listeners: ok');
})().catch(error => { console.error(error); process.exit(1); });
