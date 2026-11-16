// Pick the exact Discogs release by ID or pasted link, skipping search.
let finished = false;   // the runner judges exit codes only: a test stuck on an unanswered fake fetch must not pass
process.on('exit', () => { if (!finished) { console.error('release_by_id.test.js did not finish (an unexpected request is still pending)'); process.exitCode = 1; } });
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vinylApp = new Function(fs.readFileSync(path.join(__dirname, '../../backend/static/app.js'), 'utf8') + ';return vinylApp;')();

const a = vinylApp();
const parse = a.parseDiscogsRelease;
assert.deepStrictEqual(parse('2874426'), { id: 2874426 });
assert.deepStrictEqual(parse('  r2874426 '), { id: 2874426 });
assert.deepStrictEqual(parse('[r2874426]'), { id: 2874426 });
assert.deepStrictEqual(parse('https://www.discogs.com/release/2874426-The-Fifth-Dimension-The-Age-Of-Aquarius'), { id: 2874426 });
assert.deepStrictEqual(parse('discogs.com/de/release/123'), { id: 123 });
assert.deepStrictEqual(parse('https://www.discogs.com/sell/release/77?ev=rb'), { id: 77 });
assert.deepStrictEqual(parse('https://www.discogs.com/master/45678-Some-Album'), { master: 45678 });
assert.deepStrictEqual(parse('[m45678]'), { master: 45678 });
assert.deepStrictEqual(parse('https://www.discogs.com/pt_BR/release/123-x'), { id: 123 });
assert.deepStrictEqual(parse('https://www.discogs.com/Pink-Floyd-Animals/release/2874426'), { id: 2874426 }, 'old slug-first link');
assert.deepStrictEqual(parse('https://www.discogs.com/release/123/edit'), { id: 123 });
assert.deepStrictEqual(parse('https://www.discogs.com/master/45678-Some-Album?release=9'), { master: 45678 });
assert.deepStrictEqual(parse('https://www.discogs.com/zh-hans/sell/release/77?ev=rb'), { id: 77 });
for (const bad of ['', 'abc', 'https://example.com/release/1', 'discogs.com/artist/12']) assert.strictEqual(parse(bad), null, bad);

// The whole input must be the link (anchored), ids are 1-10 digits without a leading zero.
const accepted = [
    ['https://www.discogs.com/release/2874426-Foo-Bar', { id: 2874426 }],
    ['https://www.discogs.com/pt_BR/release/2874426', { id: 2874426 }],
    ['https://www.discogs.com/Artist-Title/release/2874426', { id: 2874426 }],
    ['https://www.discogs.com/sell/release/2874426?ev=rb', { id: 2874426 }],
    ['https://m.discogs.com/release/5#notes', { id: 5 }],
    ['http://discogs.com/release/5/', { id: 5 }],
    ['[r2874426]', { id: 2874426 }],
    ['r2874426', { id: 2874426 }],
    ['m123', { master: 123 }],
    ['https://www.discogs.com/master/123-Foo', { master: 123 }],
    ['https://api.discogs.com/releases/2874426', { id: 2874426 }],
    ['https://api.discogs.com/releases/2874426?x=1', { id: 2874426 }],
    ['api.discogs.com/masters/45', { master: 45 }],
    ['1234567890', { id: 1234567890 }],
    // pasted from prose: one wrapping layer and trailing punctuation are stripped
    ['<https://www.discogs.com/release/5>', { id: 5 }],
    ['"https://www.discogs.com/release/5"', { id: 5 }],
    ["'https://www.discogs.com/release/5'", { id: 5 }],
    ['(https://www.discogs.com/release/5)', { id: 5 }],
    ['https://www.discogs.com/release/5.', { id: 5 }],
    ['https://www.discogs.com/release/5)', { id: 5 }],
    ['https://www.discogs.com/release/5-Foo,', { id: 5 }],
    ['"https://www.discogs.com/release/5-Foo",', { id: 5 }],
    ['(https://www.discogs.com/master/9).', { master: 9 }],
    ['https://www.discogs.com/master/9!', { master: 9 }],
    ['https://api.discogs.com/releases/5;', { id: 5 }],
];
for (const [input, expected] of accepted) assert.deepStrictEqual(parse(input), expected, input);
const rejected = [
    'notdiscogs.com/release/5', 'https://notdiscogs.com/release/5',
    'example.com/?u=discogs.com/release/8', 'see https://www.discogs.com/release/8',
    'https://www.discogs.com/release/12345678901',   // 11 digits: not truncated to 10
    '12345678901', 'r12345678901',
    '0', 'm0', 'r0', 'release/0', 'https://www.discogs.com/release/0', 'https://www.discogs.com/master/0',
    'https://api.discogs.com/releases/0', 'https://api.discogs.com/releases/5x', 'https://api.discogs.com/releases/12345678901',
    'https://www.discogs.com/release/5-Foo is the one I own', 'https://www.discogs.com/release/5/edit now',
    'https://api.discogs.com/releases/5 please', 'see <https://www.discogs.com/release/5>',
    'https://www.discogs.com/release/5x', 'https://www.discogs.com/release/05',
];
for (const bad of rejected) assert.strictEqual(parse(bad), null, bad);

(async () => {
    const release = { id: 2874426, artist: 'The 5th Dimension', title: 'The Age Of Aquarius', year: 1969, label: 'Soul City',
        tracks: [{ position: 'A1', title: 'Aquarius', duration: '4:46' }] };
    const fetches = [];
    let reply = { ok: true, status: 200, body: release };
    global.fetch = async url => { fetches.push(url); return { ok: reply.ok, status: reply.status, json: async () => reply.body }; };
    const app = vinylApp();
    const picked = [];
    app.selectRelease = r => picked.push(r);

    app.releaseIdInput = 'https://www.discogs.com/release/2874426-x';
    await app.openReleaseById();
    assert.deepStrictEqual(fetches, ['/api/release/2874426']);
    assert.strictEqual(picked.length, 1); assert.strictEqual(picked[0].id, 2874426); assert.strictEqual(picked[0].tracks.length, 1);
    assert.strictEqual(app.releaseIdInput, ''); assert.strictEqual(app.releaseIdNotice, '');

    app.releaseIdInput = 'https://www.discogs.com/master/45678';
    await app.openReleaseById();
    assert.strictEqual(fetches.length, 1, 'a master release is explained, not fetched');
    assert.match(app.releaseIdNotice, /master release/);

    app.releaseIdInput = 'not a release';
    await app.openReleaseById();
    assert.strictEqual(fetches.length, 1); assert.match(app.releaseIdNotice, /Paste a Discogs release link/);

    reply = { ok: false, status: 404, body: { detail: 'Release not found or Discogs unavailable' } };
    app.releaseIdInput = '999';
    await app.openReleaseById();
    assert.match(app.releaseIdNotice, /999 was not found/); assert.strictEqual(picked.length, 1);
    assert.strictEqual(app.releaseIdInput, '999', 'kept for correction');

    reply = { ok: true, status: 200, body: { ...release, id: 5, tracks: [] } };
    app.releaseIdInput = '5';
    await app.openReleaseById();
    assert.match(app.releaseIdNotice, /no tracklist/); assert.strictEqual(picked.length, 1);

    app.analyzing = true; app.releaseIdInput = '2874426';
    await app.openReleaseById();
    assert.strictEqual(fetches.length, 3, 'nothing starts while detection runs');
    finished = true;
    console.log('release by id ok');
})().catch(error => { console.error(error); process.exit(1); });
