// Import from folder: listing, tab default, POST /api/import, queue push, skipped toast, desktop picker, refresh.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const row = (name, extra = {}) => ({ name, size: 1000, mtime: 1, stable: true, imported_id: null, ...extra });
let listing, posts, gets, postReply;
global.window = global.window || {};
global.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET';
    if (url === '/api/import' && method === 'GET') { gets.push(1); return { ok: true, status: 200, json: async () => listing }; }
    if (url === '/api/import' && method === 'POST') { const b = JSON.parse(opts.body); posts.push(b); return { ok: true, status: 200, json: async () => postReply(b) }; }
    return { ok: true, status: 200, json: async () => ({}) };
};
function mkApp() {
    const a = vinylApp();
    a.selectFile = (id) => { a.currentFileId = id; a.currentFile = a.uploadedFiles.find(f => f.id === id); };
    return a;
}
function reset(files = []) {
    listing = { enabled: true, dir: '/app/incoming', error: null, files };
    posts = []; gets = [];
    postReply = (b) => ({ files: (b.names || b.paths).map(n => ({ id: 'id-' + n, filename: n.split('/').pop(), size: 5, duration: 60, in_place: true })), skipped: [] });
}

(async () => {
    // ---- listing: enabled / disabled / error ----
    reset([row('a.wav'), row('b.wav')]);
    let a = mkApp();
    await a.loadImportList();
    assert.strictEqual(a.importInfo.enabled, true);
    assert.deepStrictEqual(a.importInfo.files.map(f => f.name), ['a.wav', 'b.wav']);
    assert.strictEqual(a.showFolderTab, true);

    listing = { enabled: false, dir: '', error: null, files: [] };
    a = mkApp(); await a.loadImportList();
    assert.strictEqual(a.importInfo.enabled, false);
    assert.strictEqual(a.showFolderTab, false, 'no folder, no bridge: tab hidden');
    global.window.pywebview = { api: { choose_import_files: async () => [] } };
    assert.strictEqual(a.detectDesktopPicker(), true);
    assert.strictEqual(a.showFolderTab, true, 'bridge alone shows the tab');
    delete global.window.pywebview;

    listing = { enabled: true, dir: '/x', error: 'Import folder is missing', files: [] };
    a = mkApp(); await a.loadImportList();
    assert.strictEqual(a.importInfo.error, 'Import folder is missing');

    // ---- requirement change: every stable row is selectable, including imported ones ----
    reset([row('a.wav'), row('b.wav', { imported_id: 'q1' }), row('c.wav', { stable: false })]);
    a = mkApp(); await a.loadImportList();
    assert.deepStrictEqual(a.importableFiles.map(f => f.name), ['a.wav', 'b.wav']);
    a.onImportRowClick(a.importInfo.files[2]);
    assert.deepStrictEqual(a.importSelected, []);
    a.onImportRowClick(a.importInfo.files[0]);
    assert.deepStrictEqual(a.importSelected, ['a.wav']);
    a.toggleImportAll(); assert.deepStrictEqual(a.importSelected, ['a.wav', 'b.wav']);
    a.toggleImportAll(); assert.deepStrictEqual(a.importSelected, []);
    a.toggleImportAll(); assert.deepStrictEqual(a.importSelected, ['a.wav', 'b.wav']);
    assert.strictEqual(a.allImportSelected, true);
    // clicking an imported row toggles its checkbox instead of disabling reimport
    a.uploadedFiles = [{ id: 'q1', filename: 'b.wav' }];
    a.onImportRowClick(a.importInfo.files[1]);
    assert.deepStrictEqual(a.importSelected, ['a.wav']);

    // ---- re-importing the recording on screen must not reselect it: selectFile clears its
    // splits and release, so a retried import would silently wipe the work ----
    reset([row('a.wav', { imported_id: 'id-a.wav' })]);
    a = mkApp(); await a.loadImportList();
    a.uploadedFiles = [{ id: 'id-a.wav', filename: 'a.wav', status: 'analyzed' }];
    a.currentFileId = 'id-a.wav'; a.currentFile = a.uploadedFiles[0];
    let reselected = 0;
    a.selectFile = () => { reselected += 1; };
    a.importSelected = ['a.wav'];
    await a.importSelectedFiles();
    assert.strictEqual(reselected, 0, 're-importing the current recording leaves it alone');

    // ...while re-importing another queued recording still jumps to it
    a.uploadedFiles.push({ id: 'id-b.wav', filename: 'b.wav', status: 'analyzed' });
    a.importSelected = ['b.wav'];
    await a.importSelectedFiles();
    assert.strictEqual(reselected, 1, 're-importing another queued recording selects it');

    // ---- a file that changed on disk came back as a new session: the old queue entry goes, and
    // the recording on screen moves to the new one (its splits belonged to the old audio) ----
    reset([row('a.wav', { imported_id: 'old' })]);
    a = mkApp(); await a.loadImportList();
    a.uploadedFiles = [{ id: 'old', filename: 'a.wav', status: 'analyzed' }];
    a.currentFileId = 'old'; a.currentFile = a.uploadedFiles[0];
    postReply = () => ({ files: [{ id: 'new', filename: 'a.wav', size: 9, duration: 90, in_place: true, replaces: 'old' }], skipped: [] });
    a.importSelected = ['a.wav'];
    await a.importSelectedFiles();
    assert.deepStrictEqual(a.uploadedFiles.map(f => f.id), ['new'], 'the stale entry is gone');
    assert.strictEqual(a.currentFileId, 'new');
    assert.ok(a.toasts.some(t => t.message.includes('changed')), 'and it says why the splits are gone');

    // ---- default tab: the folder only when it has something to show. An empty folder would
    // otherwise hide the drop zone from a first-time user ----
    reset([row('a.wav')]);
    a = mkApp(); await a.loadImportList();
    a.chooseDefaultImportTab();
    assert.strictEqual(a.importTab, 'folder', 'a folder with recordings opens on the folder tab');
    reset([]);
    a = mkApp(); await a.loadImportList();
    a.chooseDefaultImportTab();
    assert.strictEqual(a.importTab, 'upload', 'an empty folder opens on the drop zone');
    assert.strictEqual(a.showFolderTab, true, 'the folder tab stays available');

    // ---- labels: "In queue" and "Complete" are distinct; Complete follows the queue entry's status
    // (set by the export's completion message) or the server's imported_status ----
    reset([row('a.wav', { imported_id: 'q1' }), row('b.wav', { imported_id: 'q2', imported_status: 'completed' }), row('c.wav'), row('d.wav', { stable: false })]);
    a = mkApp(); await a.loadImportList();
    a.uploadedFiles = [{ id: 'q1', filename: 'a.wav', status: 'analyzed' }];
    const [fa, fb, fc, fd] = a.importInfo.files;
    assert.strictEqual(a.importFileLabel(fa), 'In queue');
    assert.strictEqual(a.importFileLabel(fb), 'Complete', 'server status survives a page reload');
    assert.strictEqual(a.importFileLabel(fc), '');
    assert.strictEqual(a.importFileLabel(fd), 'Still copying…');
    a.uploadedFiles[0].status = 'processing';
    assert.strictEqual(a.importFileLabel(fa), 'Processing…');
    a.uploadedFiles[0].status = 'completed';
    assert.strictEqual(a.importFileLabel(fa), 'Complete');

    // ---- import selected: POST body, push, no duplicates, in_place, auto-select, refresh ----
    reset([row('a.wav'), row('d/b.wav')]);
    a = mkApp(); await a.loadImportList();
    a.uploadedFiles = [{ id: 'id-a.wav', filename: 'a.wav' }];
    a.importSelected = ['a.wav', 'd/b.wav'];
    gets.length = 0;
    await a.importSelectedFiles();
    assert.deepStrictEqual(posts, [{ names: ['a.wav', 'd/b.wav'] }]);
    assert.deepStrictEqual(a.uploadedFiles.map(f => f.id), ['id-a.wav', 'id-d/b.wav']);
    const added = a.uploadedFiles[1];
    assert.strictEqual(added.in_place, true);
    assert.strictEqual(added.status, 'uploaded');
    assert.strictEqual(a.currentFileId, 'id-d/b.wav', 'first new file auto-selected');
    assert.strictEqual(gets.length, 1, 'list refreshed after import');
    assert.deepStrictEqual(a.importSelected, []);
    assert.strictEqual(a.importBusy, false);

    // does not steal selection when something is selected
    a = mkApp(); a.currentFile = { id: 'x' }; a.currentFileId = 'x';
    await a.postImport({ names: ['n.wav'] });
    assert.strictEqual(a.currentFileId, 'x');

    // ---- skipped -> one warning toast, capped at 3 ----
    postReply = () => ({ files: [], skipped: ['1', '2', '3', '4', '5'].map(n => ({ name: n + '.wav', reason: 'still copying' })) });
    a = mkApp();
    await a.postImport({ names: ['x'] });
    const warns = a.toasts.filter(t => t.type === 'warning');
    assert.strictEqual(warns.length, 1);
    assert.ok(warns[0].message.includes('1.wav — still copying') && warns[0].message.includes('3.wav'));
    assert.ok(!warns[0].message.includes('4.wav') && warns[0].message.includes('and 2 more'));

    // ---- desktop picker: paths -> POST; cancel -> no POST ----
    reset(); postReply = (b) => ({ files: [{ id: 'p1', filename: 'p.wav', size: 1, duration: 61, in_place: true }], skipped: [] });
    global.window.pywebview = { api: { choose_import_files: async () => ['/Users/you/p.wav'] } };
    a = mkApp();
    await a.chooseImportFiles();
    assert.deepStrictEqual(posts, [{ paths: ['/Users/you/p.wav'] }]);
    assert.strictEqual(a.uploadedFiles.length, 1);
    posts.length = 0;
    global.window.pywebview.api.choose_import_files = async () => [];
    await a.chooseImportFiles();
    assert.strictEqual(posts.length, 0);
    delete global.window.pywebview;

    // ---- in-place removal: wording + list refresh after DELETE only ----
    reset([row('a.wav', { imported_id: 'q1' })]);
    a = mkApp();
    a.uploadedFiles = [{ id: 'q1', filename: 'a.wav', in_place: true }, { id: 'q2', filename: 'u.wav' }];
    a.removeFile('q1', 40);
    assert.ok(a.toasts[0].message.includes('original file stays'));
    assert.strictEqual(gets.length, 0, 'no refresh inside the undo window');
    await sleep(120);
    assert.strictEqual(gets.length, 1, 'refresh after DELETE');
    // plain upload removal: plain wording, no refresh
    gets.length = 0;
    a.removeFile('q2', 40);
    assert.ok(!a.toasts.some(t => t.message.includes('original file stays') && t.message.includes('u.wav')));
    await sleep(120);
    assert.strictEqual(gets.length, 0);

    console.log('import_folder ok');
})().catch(e => { console.error(e); process.exit(1); });
