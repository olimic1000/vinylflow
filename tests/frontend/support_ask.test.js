// Support note in the "Processing Complete" card: shown after an export until turned off.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const root = path.join(__dirname, '..', '..', 'backend', 'static');
const src = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

(async () => {
    const a = vinylApp();

    // Nothing exported yet: no note
    assert.strictEqual(a.showSupportAsk, false);

    // An export finished: note shows
    a.successMessage = '3 tracks saved';
    assert.strictEqual(a.showSupportAsk, true);

    // "Don't show this again" hides it at once and saves only that key
    const calls = [];
    global.fetch = async (url, opts) => { calls.push({ url, opts }); return { json: async () => ({}) }; };
    await a.hideSupportAsk();
    assert.strictEqual(a.showSupportAsk, false);
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].url, '/api/config');
    assert.strictEqual(calls[0].opts.method, 'PUT');
    assert.deepStrictEqual(JSON.parse(calls[0].opts.body), { hide_support_ask: true });

    // A failed save still hides it for this session
    const b = vinylApp();
    b.successMessage = 'done';
    global.fetch = async () => { throw new Error('offline'); };
    const realError = console.error;
    console.error = () => {};
    await b.hideSupportAsk();
    console.error = realError;
    assert.strictEqual(b.showSupportAsk, false);

    // A saved choice (from /api/config) keeps it hidden after the next export
    const c = vinylApp();
    c.config = { ...c.config, hide_support_ask: true };
    c.successMessage = 'done';
    assert.strictEqual(c.showSupportAsk, false);

    // Markup: the note sits in the success card with the donate link and the opt-out
    assert.ok(/x-show="showSupportAsk"/.test(html));
    assert.ok(/@click="hideSupportAsk\(\)"/.test(html));
    assert.ok((html.match(/buymeacoffee\.com\/olimic1000/g) || []).length >= 2);

    console.log('support_ask ok');
})().catch(e => { console.error(e); process.exit(1); });
