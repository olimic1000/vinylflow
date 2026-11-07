// Pure helpers behind the Discogs result cards: track-length coverage, sides, the
// side suggestion and the fit indicator against the recording's music length.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

const track = (position, duration) => ({ position, title: 't' + position, duration });
const twoSides = { id: 1, tracks: [track('A1', '4:00'), track('A2', '5:00'), track('A3', '3:00'), track('B1', '5:00'), track('B2', '6:00')] }; // A=720 B=660
const partial = { id: 2, tracks: [track('A1', '4:00'), track('A2', ''), track('B1', '5:00')] };
const numeric = { id: 3, tracks: [track('1', '3:00'), track('2', '4:00'), track('3', '')] };
const empty = { id: 4, tracks: [] };
const app = vinylApp();

// ---- length coverage ----
assert.deepStrictEqual(app.releaseDurationInfo(twoSides), { total: 5, withDuration: 5, status: 'full' });
assert.deepStrictEqual(app.releaseDurationInfo(partial), { total: 3, withDuration: 2, status: 'partial' });
assert.strictEqual(app.releaseDurationInfo(numeric).status, 'partial');
assert.deepStrictEqual(app.releaseDurationInfo(empty), { total: 0, withDuration: 0, status: 'none' });
assert.strictEqual(app.releaseDurationInfo({ tracks: [track('A1', '')] }).status, 'none');
assert.strictEqual(app.releaseDurationInfo({ tracks: null }).status, 'none', 'an unloaded card has no lengths, and must not throw');
assert.strictEqual(app.parseTrackDuration('1:02:03'), 3723);
assert.strictEqual(app.parseTrackDuration('0:00'), null);
assert.strictEqual(app.parseTrackDuration('5:2x'), null);

// ---- sides ----
const groups = app.releaseSides(twoSides);
assert.deepStrictEqual(groups.map(g => [g.side, g.tracks.length, g.totalSeconds]), [['A', 3, 720], ['B', 2, 660]]);
assert.strictEqual(groups[1].tracks[0].releaseIdx, 3);
assert.strictEqual(app.sidesSummary(twoSides), 'A·3  B·2');
assert.deepStrictEqual(app.releaseSides(partial).map(g => g.totalSeconds), [null, 300]);
assert.strictEqual(app.sidesSummary(numeric), 'All·3');
assert.deepStrictEqual(app.releaseSides(empty), []);

// ---- which sides does a recording of N seconds hold? ----
assert.deepStrictEqual(app.suggestSides(twoSides, 700), { sides: ['A'], matched: true, reason: 'length' });
assert.deepStrictEqual(app.suggestSides(twoSides, 650), { sides: ['B'], matched: true, reason: 'length' });
assert.deepStrictEqual(app.suggestSides(twoSides, 1400), { sides: ['A', 'B'], matched: true, reason: 'length' });
assert.deepStrictEqual(app.suggestSides(twoSides, 3000), { sides: ['A', 'B'], matched: false, reason: 'default' });
assert.deepStrictEqual(app.suggestSides(partial, 300), { sides: ['B'], matched: true, reason: 'length' });
assert.deepStrictEqual(app.suggestSides(partial, 0), { sides: ['A', 'B'], matched: false, reason: 'default' });
assert.deepStrictEqual(app.suggestSides(numeric, 100), { sides: [''], matched: false, reason: 'single' });
assert.deepStrictEqual(app.suggestSides(empty, 100), { sides: [], matched: false, reason: 'single' });

const fourSides = { id: 10, tracks: [track('A1', '10:00'), track('A2', '10:00'), track('B1', '10:00'), track('B2', '10:00'),
    track('C1', '12:00'), track('C2', '12:00'), track('D1', '12:00'), track('D2', '12:00')] }; // A=B=1200, C=D=1440
const fourNoLengths = { id: 11, tracks: ['A1', 'A2', 'B1', 'B2', 'C1', 'C2', 'D1', 'D2'].map(p => track(p, '')) };
assert.deepStrictEqual(app.suggestSides(fourSides, 5280).sides, ['A', 'B', 'C', 'D']);
assert.deepStrictEqual(app.suggestSides(fourSides, 2400).sides, ['A', 'B'], 'disc 1');
assert.deepStrictEqual(app.suggestSides(fourSides, 2880).sides, ['C', 'D'], 'disc 2');
assert.deepStrictEqual(app.suggestSides(fourSides, 50000).sides, ['A', 'B', 'C', 'D'], 'no run within 15 %');
assert.deepStrictEqual(app.suggestSides(fourNoLengths, 3000), { sides: ['A', 'B', 'C', 'D'], matched: false, reason: 'default' });
assert.deepStrictEqual(app.suggestSides(fourNoLengths, 3000, ['A', 'B']).sides, ['C', 'D'], 'sides used elsewhere are skipped');
const equalSides = { id: 13, tracks: [track('A1', '10:00'), track('B1', '10:00'), track('C1', '10:00'), track('D1', '10:00')] };
assert.deepStrictEqual(app.suggestSides(equalSides, 1200).sides, ['A', 'B'], 'a tie prefers the disc-aligned run');
const nearFull = { id: 14, tracks: [track('A1', '10:00'), track('B1', '10:00'), track('C1', '10:00'), track('D1', '0:30')] };
assert.deepStrictEqual(app.suggestSides(nearFull, 1815).sides, ['A', 'B', 'C', 'D'], 'a near-tie prefers the full album');

// ---- labels ----
assert.strictEqual(app.scopeLabel(fourSides, ['A', 'B', 'C', 'D']), 'Full album');
assert.strictEqual(app.scopeLabel(fourSides, ['C', 'D']), 'Disc 2 (C/D)');
assert.strictEqual(app.scopeLabel(fourSides, ['B']), 'Side B');
assert.strictEqual(app.scopeLabel(fourSides, ['B', 'C']), 'Sides B+C');

// ---- fit indicator: uses the music span when the scan has one, else the file length ----
const card = vinylApp();
card.currentFile = { duration: 800, musicDuration: 700 };
let fit = card.cardFit(twoSides);
assert.strictEqual(fit.state, 'fit'); assert.strictEqual(fit.label, 'Side A'); assert.strictEqual(fit.usesMusic, true);
assert.strictEqual(card.fitText(fit), 'Side A 12:00 · your music 11:40 ✓');
card.currentFile = { duration: 650, musicDuration: null };
fit = card.cardFit(twoSides);
assert.strictEqual(fit.label, 'Side B'); assert.strictEqual(fit.usesMusic, false);
card.currentFile = { duration: 5000, musicDuration: 4900 };
fit = card.cardFit(twoSides);
assert.strictEqual(fit.state, 'nofit');
assert.ok(card.fitText(fit).startsWith("Lengths don't fit this file"));
assert.strictEqual(card.cardFit(partial), null, 'unknown lengths: no verdict');
assert.strictEqual(card.cardFit({ id: 9, tracks: null }), null, 'tracklist not loaded yet');
card.currentFile = { duration: 0, musicDuration: null };
assert.strictEqual(card.cardFit(twoSides), null, 'no recording length yet');
card.currentFile = { duration: 1380, musicDuration: 1380 };
assert.strictEqual(card.cardFit(numeric), null);
assert.strictEqual(card.cardFit({ id: 5, tracks: [track('1', '10:00'), track('2', '13:00')] }).label, 'Full album', 'single-sided release');

// ---- Best fit and ordering once every tracklist is in ----
const results = vinylApp();
results.currentFile = { duration: 700, musicDuration: 700 };
const nearer = { id: 21, tracks: [track('A1', '11:00'), track('B1', '30:00')] };   // A = 660 s (5.7 % off)
const exact = { id: 22, tracks: [track('A1', '11:40'), track('B1', '30:00')] };    // A = 700 s
const noFit = { id: 23, tracks: [track('A1', '40:00')] };
results.searchResults = [noFit, nearer, exact];
assert.deepStrictEqual(results.filteredResults.map(r => r.id), [23, 21, 22], 'Discogs order until details settle');
assert.strictEqual(results.bestFitId, null);
results.detailsSettled = true;
assert.deepStrictEqual(results.filteredResults.map(r => r.id), [21, 22, 23], 'fits first, relative order kept');
assert.strictEqual(results.bestFitId, 22);

// ---- "Only show releases with track lengths" keeps unloaded cards (not known to lack lengths) ----
const filter = vinylApp();
filter.searchResults = [twoSides, partial, numeric, empty, { id: 30, tracks: null }];
assert.strictEqual(filter.filteredResults.length, 5);
filter.onlyWithLengths = true;
assert.deepStrictEqual(filter.filteredResults.map(r => r.id), [1, 30]);

console.log('release helpers ok');
