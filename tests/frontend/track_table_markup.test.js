// The merged track table, the Export card and the waveform controls in index.html: every name they use
// exists on the Alpine object, the header and the rows share one grid, and the removed preview/Stop
// controls are gone for good.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'static', 'index.html'), 'utf8');
const vinylApp = new Function(src + ';return vinylApp;')();

const from = html.indexOf('<!-- Waveform Visualization');
const to = html.indexOf('<!-- Success Message -->');
assert.ok(from > 0 && to > from, 'sections found');
const section = html.slice(from, to);

// ---- every name used by the markup exists on the Alpine object ----
const expressions = [...section.matchAll(/\s(?:x-[\w:.-]+|@[\w:.-]+|:[\w-]+)="([^"]*)"/g)].map(match => match[1]);
// x-for variables (track, idx, dt, dtIdx) and JS words
const loopAndKeywords = new Set(['true', 'false', 'null', 'undefined', 'Math', 'Number', 'event', '$event', 'this', 'in',
    'track', 'idx', 'dt', 'dtIdx', 't', 'fmt', 'console', 'fallbackAudio']);
const app = vinylApp();
const unknown = new Set();
for (const expression of expressions) {
    const code = expression
        .replace(/'[^']*'/g, "''")
        .replace(/`([^`]*)`/g, (_, body) => [...body.matchAll(/\$\{([^}]*)\}/g)].map(match => match[1]).join(' + '));
    for (const match of code.matchAll(/(^|[^\w.$])([A-Za-z_]\w*)/g)) {
        const name = match[2];
        if (!loopAndKeywords.has(name) && !(name in app)) unknown.add(name);
    }
}
assert.deepStrictEqual([...unknown], [], 'markup uses names the Alpine object lacks');
assert.ok(expressions.length > 60, 'the extraction found the bindings');

// ---- one Play/Pause, no Stop, no preview player ----
assert.ok(!/stopWaveform|playWaveform|playPreview|stopPreview|audioPlayer|currentPlayingTrack/.test(html + src), 'preview/Stop code removed');
assert.ok(!html.includes('⏹'), 'no Stop button');
assert.ok(section.includes('@click="togglePlay()"'));
assert.strictEqual(section.match(/@click="togglePlay\(\)"/g).length, 1, 'one transport toggle');

// ---- the header and a row are separate grids with the same shape: every column but one fixed ----
const css = html.match(/\.track-grid\s*\{([^}]*)\}/)[1];
const columns = css.match(/grid-template-columns:\s*([^;]*);/)[1].trim();
const tokens = columns.replace(/minmax\([^)]*\)/g, m => m.replace(/\s/g, '')).split(/\s+/);
const flexible = tokens.filter(token => !/^\d+px$/.test(token));
assert.strictEqual(flexible.length, 1, 'only one non-fixed column: ' + columns);
assert.ok(/^minmax\(\d+px,\d+fr\)$/.test(flexible[0]), flexible[0]);
assert.ok(!/auto|max-content|min-content|fit-content/.test(columns));
assert.ok(/\.track-table-scroll\s*\{\s*overflow-x:\s*auto/.test(html), 'a narrow window scrolls the table, not the page');
const headerCells = section.match(/<div class="track-grid text-xs[^>]*>([\s\S]*?)\n\s{28}<\/div>/)[1];
const rowStart = section.indexOf('<div class="track-grid text-sm"');
assert.ok(rowStart > 0, 'row grid');
const topLevelCount = (markup) => {
    let depth = 0, count = 0;
    for (const tag of markup.matchAll(/<(\/?)(div|select|input|span|button|template|label|svg|path)\b[^>]*?(\/?)>/g)) {
        const closing = tag[1] === '/', selfClosing = tag[3] === '/' || ['input', 'path'].includes(tag[2]);
        if (closing) { depth--; continue; }
        if (depth === 0 && tag[2] === 'div') count++;
        if (!selfClosing) depth++;
    }
    return count;
};
assert.strictEqual(topLevelCount(headerCells), tokens.length, 'header cells = columns');
// the row: its direct children up to the Adjust panel
const rowMarkup = section.slice(section.indexOf('>', rowStart) + 1, section.indexOf('<div x-show="track.editing"'));
assert.strictEqual(topLevelCount(rowMarkup.replace(/\n\s{32}<\/div>\s*$/, '')), tokens.length, 'row cells = columns');

// ---- a static style plus a string :style on one element: Alpine 3.17 applies a string :style with
// setAttribute("style", ...), wiping the static one (the row separators vanished).  Use the object form. ----
const openingTags = [...section.matchAll(/<[a-z][^<>]*?>/gs)].map(match => match[0]);
const clashing = openingTags.filter(tag => /\sstyle="/.test(tag) && /\s:style="\s*[^{\s]/.test(tag));
assert.deepStrictEqual(clashing, [], 'static style + string :style on the same element');

// ---- row layout: the playing row must not shift its cells (every row and the header reserve the same
// left border), and the separators live in classes ----
const rule = selector => {
    // The rule whose selector list is exactly `selector`, at the start of a line
    const found = html.match(new RegExp('\\n\\s*' + selector.replace(/[.]/g, '\\.') + '\\s*\\{([^}]*)\\}'));
    assert.ok(found, selector + ' rule');
    return found[1];
};
// Header and rows share one wrapper rule: the same transparent left border and the same width (the
// grid's min-width plus that border), so their cells line up even when the table scrolls sideways
const shared = rule('.track-head, .track-row');
const borderPx = Number((shared.match(/border-left:\s*(\d+)px\s+solid\s+transparent/) || [])[1]);
assert.ok(borderPx > 0, 'header and rows reserve a transparent left border');
const gridMin = Number(rule('.track-grid').match(/min-width:\s*(\d+)px/)[1]);
assert.strictEqual(Number((shared.match(/min-width:\s*(\d+)px/) || [])[1]), gridMin + borderPx, 'wrappers are as wide as the grid plus the border');
assert.ok(/border-bottom/.test(rule('.track-row')) && /border-bottom/.test(rule('.track-head')), 'separators are in the classes');
const playingRule = rule('.track-row-playing');
assert.ok(!/padding|border-left:\s*\d+px\s+solid/.test(playingRule.replace(/border-left-color/g, '')), 'the playing row only recolours, it never changes the box');
assert.ok(/border-left-color/.test(playingRule));
assert.ok(/class="[^"]*\btrack-row\b[^"]*"[^>]*:class="[^"]*track-row-playing[^"]*playheadIdx === idx|:class="[^"]*playheadIdx === idx[^"]*track-row-playing/.test(section), 'playing row class toggled by the playhead');
assert.ok(/<div class="track-head[^"]*">\s*<div class="track-grid/.test(section), 'the header grid sits in a track-head wrapper, like each row in a track-row');

// ---- an ignored row dims its data cells only: the enabled buttons keep their contrast ----
const rowGridTag = section.match(/<div class="track-grid text-sm"[^>]*>/)[0];
assert.ok(!/opacity-50/.test(rowGridTag), 'the row grid itself is not dimmed');
const buttonsAt = rowMarkup.indexOf('flex gap-1.5 justify-end');
assert.ok(buttonsAt > 0);
assert.ok(!/opacity-50/.test(rowMarkup.slice(buttonsAt)), 'no dimming on or inside the button cell');
assert.ok((rowMarkup.slice(0, buttonsAt).match(/track\.ignored \}"/g) || []).length >= 5, 'number, mapping, times, length and Discogs cells dim');

// ---- no inline dark-text override on .btn-primary (the amber button colour is fixed app-wide elsewhere) ----
assert.ok(!/\.play-btn\s*\{[^}]*color/.test(html), '.play-btn must not override the button text colour');
assert.deepStrictEqual([...html.matchAll(/<[a-z][^<>]*?>/gs)].map(match => match[0]).filter(tag => /\bbtn-primary\b/.test(tag) && /\sstyle="[^"]*(^|[;\s"])color:/.test(tag)), [], 'no inline colour on a btn-primary');

// ---- Start/End fields get reset when the edit is refused ----
assert.ok(/updateTrackStart\(idx, \$event\.target\.value, \$event\.target\)/.test(section));
assert.ok(/updateTrackEnd\(idx, \$event\.target\.value, \$event\.target\)/.test(section));

// ---- the controls the brief asks for are there; leftovers of the old cards are not ----
for (const needle of ['setTrackLength(', 'auditionCut(idx, \'start\')', 'auditionCut(idx, \'end\')', 'toggleTrackPlay(idx)', 'toggleTrackIgnored(track)',
    'deleteTrack(track.number)', 'reverseMapping()', 'snapToDiscogs()', 'showTrackCountMismatch', 'processFile()', 'availableFormats', 'restorationLevel',
    'setMapping(idx']) {
    assert.ok(section.includes(needle), needle);
}
assert.ok(html.includes('x-show="showSupportAsk"') && html.includes('hideSupportAsk()'), 'the support note stays in the success card');
assert.ok(!section.includes('Track Mapping') && !section.includes('Detected Tracks'), 'the two old cards are gone');
assert.ok(html.includes('<!-- Smart Rescan'), 'the Smart Rescan comment stays where it is');
console.log('track_table_markup ok');
