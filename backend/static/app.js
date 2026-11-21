/**
 * VinylFlow Frontend Application
 * Alpine.js application for managing vinyl digitization workflow
 */

// Toast stack cap: past it the oldest toast without an action makes room (an Undo is never evicted)
const MAX_TOASTS = 5;
// Most tracks one guided detection places; must match MAX_EXPECTED_TRACKS in backend/api.py
const MAX_GUIDED_TRACKS = 99;
// How long an Undo toast stays (ms)
const UNDO_TOAST_MS = 8000;
// Discogs results per search (one page; each card's tracklist loads separately)
const SEARCH_PAGE_SIZE = 12;
// Fetches per query, the first included; must match SEARCH_MAX_PAGES in backend/api.py.
const SEARCH_MAX_PAGES = 3;
// Tracklists fetched at once while the result cards fill in (the server rate-limits Discogs anyway)
const RELEASE_DETAIL_CONCURRENCY = 2;
// A side set "fits" the recording when its total length is within this fraction of the music length
const SIDE_FIT_TOLERANCE = 0.15;
// Candidates this close (as a fraction) to the best are ties; the full album / a whole disc wins them
const SIDE_FIT_TIE = 0.01;
// Dashed ghost markers on the waveform: where the Discogs lengths expect each break (not draggable).
// Opaque #9C2418; contrast (WCAG): 7.4:1 on the card (#FAF9F6), 5.1:1 at worst on a 30 % region tint
// over the card, and 3.3:1 at worst over a region tint on the unplayed bars (#93C5FD), so 3:1 for
// graphics holds there. Over the played bars (#1d4ed8) it is only 1.2-1.6:1; there the marker is told
// apart by its dashed line running above and between the bars, not by contrast with them.
// (#B02A1E gave only 2.8:1 over the pink and purple regions on the unplayed bars.)
const EXPECTED_MARKER_COLOR = '#9C2418';
// Shortest track a hand edit may leave (seconds)
const MIN_TRACK_LENGTH = 1;
// Focused elements that use Space themselves: the global Play/Pause shortcut leaves them alone
const SPACE_OWNING_TAGS = new Set(['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'SUMMARY']);
// A pointer that moved at most this far between mousedown and click on a region is a click, not a drag (px)
const REGION_CLICK_SLOP_PX = 3;
// Cuts closer than this are "touching": the next track follows when the previous one's end moves (seconds)
const TOUCHING_CUTS_S = 0.05;
// Detected-minus-Discogs length: within DELTA_GOOD_S is good, within DELTA_WARN_S a warning, else bad (seconds)
const DELTA_GOOD_S = 3;
const DELTA_WARN_S = 10;
// Cut-point audition (the row's start/end buttons) plays this far before and after the cut (seconds)
const AUDITION_PADDING_S = 3;

// Keep WaveSurfer objects out of Alpine's reactive proxies. Through a proxy,
// WaveSurfer's identity checks fail (a removed region compares proxy vs raw and
// stays in the plugin's list, so the next remove() throws and aborts the caller).
function markRaw(obj) {
    if (obj && typeof obj === 'object') {
        Object.defineProperty(obj, '__v_skip', { value: true, configurable: true });
    }
    return obj;
}

/** A search result card; tracks: null = tracklist not loaded yet (filled in progressively). */
function newSearchCard(result) {
    return { ...result, tracks: result.tracks ?? null, detailStatus: 'pending', opening: false };
}

// A Discogs id: 1-10 digits, no leading zero (so 0, m0 and release/0 are not references).
const DISCOGS_ID = '[1-9]\\d{0,9}';
const DISCOGS_BARE_RELEASE = new RegExp(`^\\[?r?(${DISCOGS_ID})\\]?$`, 'i');
const DISCOGS_BARE_MASTER = new RegExp(`^\\[?m(${DISCOGS_ID})\\]?$`, 'i');
// Website link: scheme and www./m. optional, any path prefix segments (language, sell, old
// Artist-Title slug), then release/<id> or master/<id> ending the input or followed by - / ? #.
const DISCOGS_WEB_LINK = new RegExp(
    `^(?:https?://)?(?:www\\.|m\\.)?discogs\\.com/(?:[^\\s/?#]+/)*(release|master)/(${DISCOGS_ID})(?:[-/?#]\\S*)?$`, 'i');
// API link: api.discogs.com/releases/<id> or /masters/<id>, ending the input or followed by / ? #.
const DISCOGS_API_LINK = new RegExp(
    `^(?:https?://)?api\\.discogs\\.com/(release|master)s/(${DISCOGS_ID})(?:[/?#]\\S*)?$`, 'i');

/**
 * A Discogs release reference typed or pasted by the user: "2874426", "r2874426",
 * "[r2874426]", "m123" (master), or a link that is the whole input: discogs.com/release/<id>
 * or /master/<id> (optional http(s)://, www. or m.; any path before it such as /pt_BR/,
 * /sell/ or the old artist-slug form; a "-slug", "/", "?" or "#" may follow the id), or
 * api.discogs.com/releases/<id> / masters/<id>. Ids have at most 10 digits and are never 0;
 * anything else (other hosts, text around the link, longer ids) is null. One wrapping layer
 * (<>, quotes, parentheses) and trailing . , ; : ! ? ) are stripped first, for links pasted from prose.
 * Returns {id}, {master: id} for a master release (not one exact version), or null.
 */
function parseDiscogsRelease(text) {
    let s = String(text || '').trim();
    // Pasted from prose: drop one wrapping layer (<>, quotes, parentheses) and trailing punctuation.
    // Punctuation after the wrapper goes first ("…/release/5",), but not ")", which may close it.
    s = s.replace(/[.,;:!?]+$/, '');
    const wrapped = s.match(/^(?:<(.*)>|"(.*)"|'(.*)'|\((.*)\))$/s);
    if (wrapped) s = (wrapped[1] ?? wrapped[2] ?? wrapped[3] ?? wrapped[4]).trim();
    s = s.replace(/[.,;:!?)]+$/, '');
    let m = s.match(DISCOGS_BARE_RELEASE);
    if (m) return { id: Number(m[1]) };
    m = s.match(DISCOGS_BARE_MASTER);
    if (m) return { master: Number(m[1]) };
    m = s.match(DISCOGS_WEB_LINK) || s.match(DISCOGS_API_LINK);
    if (m) return m[1].toLowerCase() === 'release' ? { id: Number(m[2]) } : { master: Number(m[2]) };
    return null;
}

function vinylApp() {
    return {
        // WebSocket connection
        ws: null,
        wsPingTimer: null,

        // UI State
        dragging: false,
        showSettings: false,
        uploadProgress: 0,
        uploadFinalizing: false,
        uploadNotice: '',
        analyzing: false,
        searchLoading: false,
        searchNotice: '',
        releaseIdInput: '',      // exact Discogs release: ID or pasted link
        releaseIdNotice: '',
        releaseIdLoading: false,
        trackCountMismatch: false,
        autoRetryAttempts: 0,
        maxAutoRetries: 3,

        // Files and Queue
        uploadedFiles: [],
        currentFileId: null,
        currentFile: null,

        // Import from folder (files read in place, no browser upload)
        importTab: 'upload',            // 'folder' | 'upload'
        importInfo: { enabled: false, dir: '', error: null, files: [] },
        importSelected: [],             // names ticked in the folder list
        importBusy: false,              // POST /api/import in flight
        importLoading: false,           // GET /api/import in flight
        desktopPicker: false,           // the pywebview bridge offers a native file dialog

        // Analysis
        detectedTracks: [],

        // Waveform
        isPlaying: false,       // follows the player's own play/pause events
        playTime: 0,
        playDuration: 0,
        playheadIdx: -1,        // index of the track under the playhead (-1 = gap / none)
        playerNotice: '',       // inline error when playback cannot start
        playerBuffering: false,
        _pendingSeek: null,     // a seek made before the media element knew its length
        _auditionStop: null,    // one-shot stop time (s) for cut-point auditions
        _regionDownX: null,     // pointer x at region mousedown, to tell a click from a drag end
        waveform: null,
        waveformLoading: false,
        waveformRegions: null,
        currentZoom: 50,

        // Context Menu for waveform splits
        contextMenu: {
            show: false,
            x: 0,
            y: 0,
            time: 0
        },

        // Context Menu for region actions
        regionContextMenu: {
            show: false,
            x: 0,
            y: 0,
            trackNumber: null,
            isIgnored: false,
            time: 0  // Position for split functionality
        },

        // Discogs Search
        searchQuery: '',
        searchResults: [],
        searchedQuery: '',        // Query the shown results came from ("Load more" continues it, not the edited box)
        searchPage: 0,            // Discogs result pages fetched for it
        searchHasMore: false,     // Discogs has another page and the fetch limit allows it
        searchMoreLoading: false,
        searchMoreError: '',
        searchMoreNotice: '',     // After a Load more that added nothing: "No more results." (Load more gone) or "No new results on this page."
        onlyWithLengths: false,   // Result filter: hide releases without track lengths
        detailsSettled: false,    // True once every result's tracklist load finished or failed
        searchSeq: 0,             // Bumped by every search and file switch: stale replies are dropped
        _detailInflight: {},      // `${searchSeq}:${releaseId}` -> promise of a tracklist load in progress
        _pendingPickId: null,     // Card clicked while its tracklist loads; a later pick clears it
        _releaseIdCall: 0,        // Bumped per "exact pressing" lookup; only the latest resets releaseIdLoading
        selectedRelease: null,
        trackMappingReversed: false,
        customMapping: [],   // detected track index -> index into selectedRelease.tracks (a side can be a subset)

        // Which side(s) of the selected release the recording covers (guided detection places that many tracks)
        selectedSides: [],        // Side keys ('A', 'B', '' = unlettered)
        suggestedSides: [],       // Sides auto-picked when the release was selected
        sidesSuggestionReason: '',   // 'length' | 'default' | 'single' | '' (how suggestedSides was chosen)

        // Guided detection and hand edits
        expectedBoundaries: [],   // Seconds where Discogs lengths put each break (ghost markers)
        maxGuidedTracks: MAX_GUIDED_TRACKS,   // for templates
        boundaryEditCount: 0,     // Bumped by every hand edit and Undo restore, so a guided reply started before one is discarded
        boundariesEdited: false,  // True once the user hand-edits boundaries (until detection replaces them)
        editBanner: false,        // Show "You've adjusted boundaries by hand" choice

        // Smart Rescan
        expectedTracks: null,
        rescanNotice: '',

        // Processing
        isProcessing: false,
        processingProgress: 0,
        processingMessage: '',
        successMessage: '',
        currentJobId: null,
        processPollTimer: null,
        lastProgressAt: null,

        // Output format
        outputFormat: 'flac',
        availableFormats: [],

        // Audio restoration
        restorationLevel: 0,   // 0=disabled, 1=enabled

        // Toasts (see notify) and queue files removed but not yet deleted server-side
        toasts: [],
        _toastSeq: 0,
        _toastTimers: {},
        pendingRemovals: {},   // fileId -> { file, order, wasCurrent, snapshot, search, committed, toastId }

        // Config
        config: {
            silence_threshold: -40,
            min_silence_duration: 1.5,
            track_numbering: 'sequential_disc_per_lp',
            min_track_length: 30,
            flac_compression: 8,
            output_dir: '',
            hide_support_ask: false,
            album_description: false   // Wikipedia lead paragraph in the Comment tag (off by default)
        },

        // Wikipedia description preview on the Release card (see loadAlbumDescription)
        albumDescription: { releaseId: null, status: 'idle', text: '', title: '', url: '' },
        albumDescriptionExpanded: false,   // "Show more" in the preview

        // Supported input file extensions
        supportedExtensions: ['.wav', '.aiff', '.aif'],

        // Status
        discogsConfigured: true,

        // Setup (first-run)
        setupRequired: false,
        setupLoading: false,
        setupError: null,
        setupToken: '',
        setupUserAgent: 'VinylFlow/1.0',

        /**
         * Initialize the application
         */
        async init() {
            // Suppress the native WebKit/WKWebView context menu globally.
            // WaveSurfer v7 renders inside a shadow DOM; by the time the
            // 'contextmenu' event bubbles to our container listener WKWebView
            // has already decided to show its native menu.  Calling
            // preventDefault() in the capture phase (before any shadow-DOM
            // handler fires) is the only reliable way to stop it.
            document.addEventListener('contextmenu', e => e.preventDefault(), true);

            // Space toggles waveform play/pause
            document.addEventListener('keydown', (e) => this.onGlobalKeydown(e));

            // Files removed from the queue are deleted server-side only after
            // an undo window; flush them if the page goes away first.
            window.addEventListener('pagehide', () => {
                this.commitAllRemovals({ keepalive: true });
            });

            await this.loadConfig();
            await this.loadFormats();
            await this.checkStatus();
            this.connectWebSocket();

            // Import from folder: pywebview injects its bridge asynchronously
            this.detectDesktopPicker();
            window.addEventListener('pywebviewready', () => this.detectDesktopPicker());
            await this.loadImportList();
            this.chooseDefaultImportTab();

            this.$watch('currentFile', (file) => {
                if (file && !this.searchQuery) {
                    this.searchQuery = this.cleanFilename(file.filename);
                }
            });
        },

        /**
         * Load available output formats from API
         */
        async loadFormats() {
            try {
                const response = await fetch('/api/formats');
                const data = await response.json();
                this.availableFormats = data.formats;
            } catch (error) {
                console.error('Failed to load formats:', error);
                this.availableFormats = [
                    { id: 'flac', label: 'FLAC (Lossless)', extension: '.flac' },
                    { id: 'mp3', label: 'MP3 (320kbps)', extension: '.mp3' },
                    { id: 'aiff', label: 'AIFF (Lossless)', extension: '.aiff' },
                ];
            }
        },

        /**
         * Connect to WebSocket for real-time updates
         */
        connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;

            // Clear any previous ping timer — reconnects must not stack
            // one leaked interval per attempt.
            if (this.wsPingTimer) {
                clearInterval(this.wsPingTimer);
                this.wsPingTimer = null;
            }

            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                console.log('WebSocket connected');
            };

            this.ws.onmessage = (event) => {
                if (event.data === 'pong') return;

                try {
                    const data = JSON.parse(event.data);
                    this.handleWebSocketMessage(data);
                } catch (error) {
                    console.warn('Non-JSON WebSocket message:', event.data);
                }
            };

            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };

            this.ws.onclose = () => {
                console.log('WebSocket disconnected, reconnecting...');
                setTimeout(() => this.connectWebSocket(), 3000);
            };

            this.wsPingTimer = setInterval(() => {
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    this.ws.send('ping');
                }
            }, 30000);
        },

        /**
         * Handle WebSocket messages
         */
        handleWebSocketMessage(data) {
            console.log('WebSocket message:', data);

            switch (data.type) {
                case 'scan_complete':
                    this.applyScan(data.file_id, data);
                    break;

                case 'progress':
                    if (data.file_id === this.currentFileId) {
                        this.processingProgress = data.progress;
                        this.processingMessage = data.message;
                        this.lastProgressAt = Date.now();
                    }
                    break;

                case 'step_complete':
                    if (data.file_id === this.currentFileId) {
                        console.log(data.message);
                    }
                    break;

                case 'complete':
                    if (data.file_id === this.currentFileId) {
                        this.stopProcessPolling();
                        this.processingProgress = 1.0;
                        this.processingMessage = 'Complete!';
                        this.isProcessing = false;
                        this.successMessage = `✅ ${data.tracks.length} tracks saved to your VinylFlow/output folder\n\nTracks: ${data.tracks.join(', ')}`;

                        const file = this.uploadedFiles.find(f => f.id === data.file_id);
                        if (file) {
                            file.status = 'completed';
                            if (file.in_place) this.loadImportList();
                        }
                    }
                    break;

                case 'error':
                    // Detection broadcasts (analyze / rescan / guided) are `recoverable: true`, export
                    // failures `recoverable: false`.  Only an export failure may end the export state:
                    // another recording can be exporting while this one is being detected, and the
                    // detection code shows its own failure.
                    if (data.recoverable === true) break;
                    if (data.file_id === this.currentFileId) {
                        this.stopProcessPolling();
                        this.isProcessing = false;
                        this.processingMessage = `Error: ${data.message}`;
                        this.notify(`Processing error: ${data.message}`, { type: 'error' });

                        const file = this.uploadedFiles.find(f => f.id === data.file_id);
                        if (file) {
                            file.status = 'error';
                        }
                    }
                    break;
            }
        },

        /**
         * Load configuration from API
         */
        async loadConfig() {
            try {
                const response = await fetch('/api/config');
                const data = await response.json();
                this.config = data;
            } catch (error) {
                console.error('Failed to load config:', error);
            }
        },

        /**
         * Check app configuration status
         */
        async checkStatus() {
            try {
                const response = await fetch('/api/status');
                const data = await response.json();
                this.discogsConfigured = data.discogs_configured;

                // Show setup modal if not configured
                if (!this.discogsConfigured) {
                    this.setupRequired = true;
                }
            } catch (error) {
                console.error('Failed to check status:', error);
            }
        },

        /**
         * Submit setup with Discogs token
         */
        async submitSetup() {
            this.setupLoading = true;
            this.setupError = null;

            try {
                const response = await fetch('/api/setup/discogs-token', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        token: this.setupToken,
                        user_agent: this.setupUserAgent
                    })
                });

                const data = await response.json();

                if (!response.ok) {
                    throw new Error(data.detail || 'Setup failed');
                }

                // Success!
                this.discogsConfigured = true;
                this.setupRequired = false;
                this.setupToken = '';

                this.notify(`Connected to Discogs as ${data.username}.`, { type: 'success' });

            } catch (error) {
                this.setupError = error.message;
            } finally {
                this.setupLoading = false;
            }
        },

        /**
         * Save configuration to API
         */
        async saveConfig() {
            try {
                const response = await fetch('/api/config', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(this.config)
                });
                const data = await response.json();
                this.config = data;
                this.showSettings = false;
                this.loadAlbumDescription();   // the setting may have just been switched on or off
                this.notify('Settings saved.', { type: 'success' });
            } catch (error) {
                console.error('Failed to save config:', error);
                this.notify('Failed to save settings.', { type: 'error' });
            }
        },

        /** The support note in the "Processing Complete" card: after every export until turned off */
        get showSupportAsk() {
            return !!this.successMessage && !this.config.hide_support_ask;
        },

        /**
         * "Don't show this again" on the support note.  Saved in settings.json, not
         * localStorage: the desktop window starts with empty web storage every launch.
         * Sends only this key, so unsaved edits in the Settings dialog are not saved with it.
         */
        async hideSupportAsk() {
            this.config.hide_support_ask = true;
            try {
                await fetch('/api/config', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ hide_support_ask: true })
                });
            } catch (error) {
                console.error('Failed to save support note choice:', error);
            }
        },

        /**
         * Open desktop folder picker for output directory
         */
        async chooseOutputFolder() {
            try {
                if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.select_output_folder) {
                    this.notify('The folder picker is available in the desktop app only.', { type: 'info' });
                    return;
                }

                const selected = await window.pywebview.api.select_output_folder(this.config.output_dir || '');
                if (selected) {
                    this.config.output_dir = selected;
                }
            } catch (error) {
                console.error('Failed to choose output folder:', error);
                this.notify('Failed to open the folder picker.', { type: 'error' });
            }
        },

        /**
         * Check if a file has a supported extension
         */
        isSupportedFile(filename) {
            const ext = filename.toLowerCase().substring(filename.lastIndexOf('.'));
            return this.supportedExtensions.includes(ext);
        },

        // ---------------------------------------------------------------
        // Import from folder
        // ---------------------------------------------------------------

        /** True when the desktop shell offers a native file dialog */
        detectDesktopPicker() {
            this.desktopPicker = typeof window !== 'undefined'
                && typeof window.pywebview?.api?.choose_import_files === 'function';
            return this.desktopPicker;
        },

        /** The folder tab shows when an import dir is configured or the desktop dialog exists */
        get showFolderTab() {
            return this.importInfo.enabled || this.desktopPicker;
        },

        /** Every stable source can be selected again, even after a previous import or export. */
        get importableFiles() {
            return (this.importInfo.files || []).filter(f => f.stable !== false);
        },

        /** Reload the folder listing; failures leave the previous list in place */
        async loadImportList() {
            this.importLoading = true;
            try {
                const response = await fetch('/api/import');
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();
                this.importInfo = {
                    enabled: !!data.enabled,
                    dir: data.dir || '',
                    error: data.error || null,
                    files: data.files || []
                };
                // Drop ticks for rows that vanished or became unselectable
                const ok = new Set(this.importableFiles.map(f => f.name));
                this.importSelected = this.importSelected.filter(n => ok.has(n));
            } catch (error) {
                console.error('Failed to list import folder:', error);
            } finally {
                this.importLoading = false;
            }
        },

        /** Open on the folder tab only when it has recordings: an empty one would hide the drop zone */
        chooseDefaultImportTab() {
            this.importTab = this.importInfo.files.length > 0 ? 'folder' : 'upload';
        },

        isImportSelected(name) {
            return this.importSelected.includes(name);
        },

        toggleImportSelected(name) {
            this.importSelected = this.isImportSelected(name)
                ? this.importSelected.filter(n => n !== name)
                : [...this.importSelected, name];
        },

        get allImportSelected() {
            const n = this.importableFiles.length;
            return n > 0 && this.importableFiles.every(f => this.importSelected.includes(f.name));
        },

        toggleImportAll() {
            this.importSelected = this.allImportSelected ? [] : this.importableFiles.map(f => f.name);
        },

        /** A queue entry is not proof of a completed export. */
        importFileLabel(file) {
            if (file.stable === false) return 'Still copying…';
            const entry = this.uploadedFiles.find(item => item.id === file.imported_id);
            const status = entry?.status || file.imported_status;
            if (status === 'processing') return 'Processing…';
            if (status === 'completed') return 'Complete';
            return file.imported_id ? 'In queue' : '';
        },

        /** Row and checkbox use the same selection behavior; existing sessions are reused. */
        onImportRowClick(file) {
            if (file.stable !== false) this.toggleImportSelected(file.name);
        },

        /** Import the ticked folder files */
        async importSelectedFiles() {
            if (!this.importSelected.length) return;
            await this.postImport({ names: [...this.importSelected] });
            this.importSelected = [];
        },

        /** Desktop: native multi-select dialog, then import the chosen paths */
        async chooseImportFiles() {
            if (this.importBusy || !this.detectDesktopPicker()) return;
            let paths;
            try {
                paths = await window.pywebview.api.choose_import_files();
            } catch (error) {
                this.notifyError('Could not open the file dialog', error);
                return;
            }
            if (!paths || !paths.length) return;
            await this.postImport({ paths });
        },

        /** POST /api/import and queue the returned files like an upload */
        async postImport(body) {
            this.importBusy = true;
            try {
                await this.commitAllRemovals();
                const response = await fetch('/api/import', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();

                const known = new Set(this.uploadedFiles.map(f => f.id));
                const added = [];
                let replacedCurrentBy = null;
                (data.files || []).forEach(file => {
                    // The file changed on disk, so the server started over: the old entry is gone
                    if (file.replaces) {
                        if (file.replaces === this.currentFileId) replacedCurrentBy = file.id;
                        this.uploadedFiles = this.uploadedFiles.filter(entry => entry.id !== file.replaces);
                        this.notify(`"${file.filename}" changed on disk, so it was imported again. Earlier splits for it are gone.`, { type: 'warning' });
                    }
                    if (known.has(file.id)) {
                        const existing = this.uploadedFiles.find(entry => entry.id === file.id);
                        if (file.status) existing.status = file.status;
                        return;
                    }
                    known.add(file.id);
                    const entry = {
                        ...file,
                        status: file.status || 'uploaded'
                    };
                    this.uploadedFiles.push(entry);
                    added.push(entry);
                });
                if (replacedCurrentBy) {
                    this.selectFile(replacedCurrentBy);
                } else if (!this.currentFile && data.files?.length > 0) {
                    this.selectFile((added[0] || data.files[0]).id);
                } else if (data.files?.length === 1 && !added.length && data.files[0].id !== this.currentFileId) {
                    // Not the recording on screen: selectFile would clear its splits and release
                    this.selectFile(data.files[0].id);
                }

                const skipped = data.skipped || [];
                if (skipped.length) {
                    const shown = skipped.slice(0, 3).map(s => `${s.name} — ${s.reason}`);
                    if (skipped.length > 3) shown.push(`and ${skipped.length - 3} more`);
                    this.notify(`${skipped.length} file(s) not imported: ${shown.join('; ')}`, { type: 'warning' });
                }
            } catch (error) {
                this.notifyError('Import failed', error);
            } finally {
                this.importBusy = false;
            }
            await this.loadImportList();
        },

        /**
         * Handle file drop
         */
        async handleDrop(event) {
            this.dragging = false;
            const dropped = Array.from(event.dataTransfer.files);
            const files = dropped.filter(f => this.isSupportedFile(f.name));

            const skipped = dropped.length - files.length;
            if (skipped > 0) {
                this.showUploadNotice(`${skipped} file(s) skipped — only WAV and AIFF are supported`);
            }

            await this.uploadFiles(files);
        },

        /**
         * Show a transient notice under the upload zone (replaces any
         * previous one so an old timer can't clear a newer message early)
         */
        showUploadNotice(message) {
            this.uploadNotice = message;
            if (this._uploadNoticeTimer) clearTimeout(this._uploadNoticeTimer);
            this._uploadNoticeTimer = setTimeout(() => { this.uploadNotice = ''; }, 6000);
        },

        /**
         * Handle file selection from input
         */
        async handleFileSelect(event) {
            const files = Array.from(event.target.files);
            await this.uploadFiles(files);
            event.target.value = '';
        },

        /**
         * Upload files to server with progress tracking
         */
        async uploadFiles(files) {
            if (files.length === 0) return;

            const formData = new FormData();
            files.forEach(file => formData.append('files', file));

            return new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();

                // Track upload progress. When the last byte is sent the
                // server still has to write the file and probe its duration,
                // so switch to a "finalizing" state instead of hiding the bar.
                xhr.upload.addEventListener('progress', (e) => {
                    if (e.lengthComputable) {
                        this.uploadProgress = (e.loaded / e.total) * 100;
                        if (e.loaded >= e.total) {
                            this.uploadFinalizing = true;
                        }
                    }
                });

                // Handle completion
                xhr.addEventListener('load', () => {
                    this.uploadProgress = 100;
                    this.uploadFinalizing = false;

                    if (xhr.status >= 200 && xhr.status < 300) {
                        try {
                            const data = JSON.parse(xhr.responseText);

                            data.files.forEach(file => {
                                this.uploadedFiles.push({
                                    ...file,
                                    status: 'uploaded',
                                    musicDuration: null   // Filled by the background scan (scan_complete / /api/scan)
                                });
                            });

                            if (data.files.length === 0) {
                                this.showUploadNotice('No files were uploaded — only WAV and AIFF are supported');
                            }

                            if (!this.currentFile && data.files.length > 0) {
                                this.selectFile(data.files[0].id);
                            }

                            // Reset progress after a short delay — the queue
                            // panel is the success feedback, no alert needed.
                            setTimeout(() => {
                                this.uploadProgress = 0;
                            }, 1000);

                            resolve(data);
                        } catch (error) {
                            console.error('Upload failed:', error);
                            this.notify('Upload failed: invalid response from the server.', { type: 'error' });
                            this.uploadProgress = 0;
                            reject(error);
                        }
                    } else {
                        console.error('Upload failed:', xhr.statusText);
                        this.notify('Upload failed' + (xhr.statusText ? ': ' + xhr.statusText : '.'), { type: 'error' });
                        this.uploadProgress = 0;
                        reject(new Error(xhr.statusText));
                    }
                });

                // Handle errors
                xhr.addEventListener('error', () => {
                    console.error('Upload failed');
                    this.notify('Upload failed: could not reach the server.', { type: 'error' });
                    this.uploadProgress = 0;
                    this.uploadFinalizing = false;
                    reject(new Error('Upload failed'));
                });

                // Send the request
                xhr.open('POST', '/api/upload');
                xhr.send(formData);
            });
        },

        // ---------------------------------------------------------------
        // Toasts (replace alert()/confirm(); destructive actions use Undo)
        // ---------------------------------------------------------------

        /**
         * Show a toast.  notify(message, { type, action: {label, fn}, timeout, onClose })
         *  - type: 'info' | 'success' (auto-dismiss after 5 s) | 'warning' | 'error' (sticky)
         *  - timeout: ms override (0 = sticky)
         *  - action: button on the toast; clicking runs fn and closes the toast
         *  - onClose(reason): reason is 'timeout' | 'dismiss' | 'action' | 'commit' | 'evict'
         *    (used by undo flows; 'commit' = a pending removal was committed early,
         *    'evict' = pushed out by the stack cap, which never happens to a toast with an action)
         * Hovering or focusing a toast pauses its timer.  Returns the toast id.
         */
        notify(message, opts = {}) {
            const type = ['info', 'success', 'warning', 'error'].includes(opts.type) ? opts.type : 'info';
            const sticky = type === 'warning' || type === 'error';
            const timeout = opts.timeout !== undefined ? opts.timeout : (sticky ? 0 : 5000);
            message = String(message);

            // An identical toast already on screen is refreshed, not stacked
            const duplicate = this.toasts.find(t => t.message === message && t.type === type && !t.action && !opts.action);
            if (duplicate) {
                this._armToast(duplicate.id, timeout);
                return duplicate.id;
            }

            const id = ++this._toastSeq;
            this.toasts.push({
                id, message, type,
                action: opts.action || null,
                onClose: opts.onClose || null
            });
            // Keep the stack short: the oldest other toast without an action makes room.  Toasts
            // with an action (Undo) are never evicted, so the stack may grow past the cap.
            while (this.toasts.length > MAX_TOASTS) {
                const oldest = this.toasts.find(t => !t.action && t.id !== id);
                if (!oldest) break;
                this.dismissToast(oldest.id, 'evict');
            }
            this._armToast(id, timeout);
            return id;
        },

        /** Icon shown beside a toast so its type is not conveyed by colour alone */
        toastIcon(type) {
            return { success: '✓', warning: '⚠', error: '✕' }[type] || 'ℹ';
        },

        _armToast(id, ms) {
            this._clearToastTimer(id);
            if (!(ms > 0)) return;
            const handle = setTimeout(() => this.dismissToast(id, 'timeout'), ms);
            if (handle && handle.unref) handle.unref();   // Node (frontend tests) only
            this._toastTimers[id] = { handle, remaining: ms, startedAt: Date.now() };
        },

        _clearToastTimer(id) {
            const timer = this._toastTimers[id];
            if (timer) clearTimeout(timer.handle);
            delete this._toastTimers[id];
        },

        /** Hover/focus: stop the countdown, remembering how much was left */
        pauseToast(id) {
            const timer = this._toastTimers[id];
            if (!timer || timer.paused) return;
            clearTimeout(timer.handle);
            timer.remaining = Math.max(timer.remaining - (Date.now() - timer.startedAt), 500);
            timer.paused = true;
        },

        /** Mouse leave / focus out: continue with the remaining time */
        resumeToast(id) {
            const timer = this._toastTimers[id];
            if (!timer || !timer.paused) return;
            this._armToast(id, timer.remaining);
        },

        /** Close a toast; reason is passed to its onClose callback */
        dismissToast(id, reason = 'dismiss') {
            const index = this.toasts.findIndex(t => t.id === id);
            if (index === -1) return;
            const [toast] = this.toasts.splice(index, 1);
            this._clearToastTimer(id);
            if (toast.onClose) {
                try { toast.onClose(reason); } catch (e) { console.error('Toast onClose failed:', e); }
            }
        },

        /** Run a toast's action button */
        runToastAction(id) {
            const toast = this.toasts.find(t => t.id === id);
            if (!toast || !toast.action) return;
            const actionFn = toast.action.fn;
            this.dismissToast(id, 'action');
            if (actionFn) actionFn();
        },

        /** Convenience for error toasts from catch blocks */
        notifyError(prefix, error) {
            const detail = error && error.message ? error.message : String(error || '');
            return this.notify(detail ? `${prefix}: ${detail}` : prefix, { type: 'error' });
        },

        /** Why an edit Undo for `fileId` can't run now, or '' when it can. */
        undoBlockedReason(fileId) {
            if (this.currentFileId !== fileId) return 'Switch back to that file to undo.';
            if (this.analyzing) return 'Wait for track detection to finish, then undo.';
            if (this.isProcessing) return "Can't undo while an export is running.";
            return '';
        },

        /**
         * Undo toast.  Its button first checks undoBlockedReason(fileId); a refusal is a
         * warning toast and leaves the state alone.  guard: false skips that check (file
         * removal must stay undoable while another file is open).
         */
        notifyUndo(message, undo, { fileId = null, guard = true, type = 'info', timeout = UNDO_TOAST_MS, onClose } = {}) {
            return this.notify(message, {
                type, timeout, onClose,
                action: {
                    label: 'Undo',
                    fn: () => {
                        const blocked = guard ? this.undoBlockedReason(fileId) : '';
                        if (blocked) {
                            this.notify(blocked, { type: 'warning', timeout: 6000 });
                            return;
                        }
                        undo();
                    }
                }
            });
        },

        /**
         * Copy of the track-editing state that Undo flows restore: tracks,
         * release and mapping.  Tracks are cloned (they are edited in place);
         * the release is replaced, never mutated, so a reference is enough.
         * Search results are not part of it (see snapshotSearch): a search made
         * after an edit must survive that edit's Undo.  snapshotEdits,
         * restoreEdits and clearEditState cover the same fields.
         */
        snapshotEdits() {
            return {
                detectedTracks: this.detectedTracks.map(track => ({ ...track })),
                selectedRelease: this.selectedRelease,
                trackCountMismatch: this.trackCountMismatch,
                trackMappingReversed: this.trackMappingReversed,
                customMapping: [...this.customMapping],
                selectedSides: [...this.selectedSides],
                suggestedSides: [...this.suggestedSides],
                sidesSuggestionReason: this.sidesSuggestionReason,
                expectedBoundaries: [...this.expectedBoundaries],
                boundariesEdited: this.boundariesEdited
            };
        },

        /** Put a snapshotEdits() copy back and redraw the waveform regions */
        restoreEdits(snapshot) {
            this.detectedTracks = snapshot.detectedTracks.map(track => ({ ...track }));
            this.selectedRelease = snapshot.selectedRelease;
            if (snapshot.selectedRelease) this._pendingPickId = null;   // a card still loading must not replace it
            // The preview is keyed by release id: load it for a different release the Undo put back
            if (snapshot.selectedRelease && snapshot.selectedRelease.id !== this.albumDescription.releaseId) {
                this.loadAlbumDescription();
            }
            this.trackCountMismatch = snapshot.trackCountMismatch;
            this.trackMappingReversed = snapshot.trackMappingReversed;
            this.customMapping = [...snapshot.customMapping];
            this.selectedSides = [...snapshot.selectedSides];
            this.suggestedSides = [...snapshot.suggestedSides];
            this.sidesSuggestionReason = snapshot.sidesSuggestionReason;
            this.expectedBoundaries = [...snapshot.expectedBoundaries];
            this.boundariesEdited = snapshot.boundariesEdited;
            this.boundaryEditCount++;   // an Undo replaces the boundaries outside detection
            this.editBanner = false;
            this.addTrackRegions();   // draws the ghost markers too
        },

        /** Reset the fields snapshotEdits() covers (no tracks, no release, no mapping, no sides) */
        clearEditState() {
            this.detectedTracks = [];
            this.selectedRelease = null;
            this.trackCountMismatch = false;
            this.trackMappingReversed = false;
            this.customMapping = [];
            this.selectedSides = [];
            this.suggestedSides = [];
            this.sidesSuggestionReason = '';
            this.resetEditState();
        },

        /** Search results and query, for the flows that clear them (re-analyze, removing the open file) */
        snapshotSearch() {
            return {
                results: this.searchResults, query: this.searchQuery,
                searchedQuery: this.searchedQuery, page: this.searchPage, hasMore: this.searchHasMore
            };
        },

        restoreSearch(saved) {
            this.searchResults = saved.results;
            this.searchQuery = saved.query;
            this.searchedQuery = saved.searchedQuery;
            this.searchPage = saved.page;
            this.searchHasMore = saved.hasMore;
            // The loaders of the restored cards stopped when their search was superseded
            // (removing the file bumped searchSeq): fill in the tracklists still missing.
            const seq = ++this.searchSeq;
            const missing = this.searchResults.filter(r => !r.tracks).map(r => r.id);
            if (missing.length) {
                this.detailsSettled = false;
                this.searchResults.forEach(r => { if (!r.tracks) r.detailStatus = 'pending'; });
                this.loadAllReleaseDetails(seq, missing);
            } else {
                this.detailsSettled = true;
            }
        },

        /**
         * Select a file from the queue
         */
        selectFile(fileId) {
            // Already open: everything below would throw away its splits and release, then search again.
            if (fileId === this.currentFileId) return;
            // Detection writes into the open recording: switching mid-way would hand it
            // the new file's tracks (a release pick starts detection by itself).
            if (this.analyzing) {
                this.notify('Wait for track detection to finish before switching recordings.', { type: 'warning', timeout: 5000 });
                return;
            }
            this.releaseIdLoading = false;   // a lookup for the previous recording is dropped on arrival
            this.currentFileId = fileId;
            this.currentFile = this.uploadedFiles.find(f => f.id === fileId);
            if (this.currentFile && !(this.currentFile.musicDuration > 0)) {
                this.fetchScan(fileId);
            }
            this.clearEditState();
            this.resetSearchResults();
            this._pendingPickId = null;
            this.releaseIdNotice = '';
            this.releaseIdInput = '';
            this.searchNotice = '';
            // A search of the previous file is superseded: its reply (and its tracklist
            // loads) are dropped by the sequence check, and its loading flag must not
            // block this file's search.
            this.searchLoading = false;
            this.searchSeq++;
            this.successMessage = '';
            this.processingProgress = 0;
            this.processingMessage = '';

            this.destroyWaveform();

            if (this.currentFile) {
                this.searchQuery = this.cleanFilename(this.currentFile.filename);
                // Discogs first: start looking right away (not awaited, the UI stays usable).
                // Without a token there is nothing to search; "Skip Discogs" still works.
                if (this.discogsConfigured && this.searchQuery.trim()) this.searchDiscogs();
            }
        },

        /**
         * Remove a file from the queue.  The entry disappears at once and an
         * Undo toast is shown; the server-side DELETE only happens once the
         * undo window has passed (or the toast is closed / the page unloads).
         */
        removeFile(fileId, undoMs = 8000) {
            const index = this.uploadedFiles.findIndex(f => f.id === fileId);
            if (index === -1 || this.pendingRemovals[fileId]) return;

            const file = this.uploadedFiles[index];
            const isCurrent = this.currentFileId === fileId;
            // The file's own status covers an export left running while another file is open;
            // the open file is also refused while its export request is still being sent.
            if (file.status === 'processing' || (isCurrent && (this.analyzing || this.isProcessing))) {
                this.notify('Wait for the current analysis or export to finish before removing this file.', { type: 'warning', timeout: 5000 });
                return;
            }

            const entry = {
                file,
                order: this.uploadedFiles.map(f => f.id),   // queue order before this removal, for Undo
                wasCurrent: isCurrent,
                snapshot: isCurrent ? this.snapshotEdits() : null,
                search: isCurrent ? this.snapshotSearch() : null,
                committed: false,
                toastId: null
            };
            this.pendingRemovals = { ...this.pendingRemovals, [fileId]: entry };

            this.uploadedFiles = this.uploadedFiles.filter(f => f.id !== fileId);
            if (isCurrent) {
                this.destroyWaveform();
                this.currentFileId = null;
                this.currentFile = null;
                this.clearEditState();
                this.resetSearchResults();
                this.searchLoading = false;
                this.searchSeq++;   // drop the replies of a search still running for this file
                this._pendingPickId = null;   // ... and of a release pick (card or pasted link) still loading
                this.releaseIdLoading = false;
            }

            const removedMessage = file.in_place
                ? `Removed "${file.filename}" from the queue. The original file stays in the import folder.`
                : `Removed "${file.filename}" from the queue.`;
            entry.toastId = this.notifyUndo(removedMessage, () => this.undoRemoveFile(fileId), {
                guard: false,
                timeout: undoMs,
                // Any close other than Undo (timeout, X button, page unload) makes it final
                onClose: (reason) => { if (reason !== 'action') this.commitRemoval(fileId); }
            });
        },

        /** Put a soft-removed file back in its old queue position */
        async undoRemoveFile(fileId) {
            const entry = this.pendingRemovals[fileId];
            if (!entry || entry.committed) return;
            const remaining = { ...this.pendingRemovals };
            delete remaining[fileId];
            this.pendingRemovals = remaining;

            this.reinsertFile(entry.file, entry.order);

            // Re-select it only if nothing else was opened in the meantime
            if (entry.wasCurrent && !this.currentFileId) {
                // Nothing analyzed yet: select it like a fresh click (search query from the filename)
                if (entry.snapshot.detectedTracks.length === 0) {
                    this.selectFile(fileId);
                    return;
                }
                this.currentFileId = fileId;
                this.currentFile = entry.file;
                this.restoreEdits(entry.snapshot);
                this.restoreSearch(entry.search);
                await this.initWaveform();
            }
        },

        /**
         * Put a removed file back where it was in `order` (the queue's ids before its
         * removal): before the first later file still queued, else after the nearest
         * earlier one, else at the end.  Removals and Undos may interleave in any order.
         */
        reinsertFile(file, order) {
            const files = this.uploadedFiles.filter(f => f.id !== file.id);
            const at = order.indexOf(file.id);
            const position = id => files.findIndex(f => f.id === id);
            let index = -1;
            if (at !== -1) {
                for (const id of order.slice(at + 1)) {
                    index = position(id);
                    if (index !== -1) break;
                }
                for (const id of index === -1 ? order.slice(0, at).reverse() : []) {
                    const before = position(id);
                    if (before !== -1) { index = before + 1; break; }
                }
            }
            files.splice(index === -1 ? files.length : index, 0, file);
            this.uploadedFiles = files;
        },

        /** Really delete a soft-removed file on the server */
        async commitRemoval(fileId, { keepalive = false } = {}) {
            const entry = this.pendingRemovals[fileId];
            if (!entry || entry.committed) return;
            entry.committed = true;
            if (entry.toastId) this.dismissToast(entry.toastId, 'commit');
            try {
                const response = await fetch(`/api/queue/${fileId}`, { method: 'DELETE', keepalive });
                if (!response.ok && response.status !== 404) {
                    throw new Error(`HTTP ${response.status}`);
                }
                // The "In queue" marker in the folder list clears now
                if (entry.file.in_place) this.loadImportList();
            } catch (error) {
                // The server still has it: bring the file back so the queue
                // reflects reality, and say so.
                console.error('Failed to remove file:', error);
                if (!this.uploadedFiles.some(f => f.id === fileId)) {
                    this.reinsertFile(entry.file, entry.order);
                }
                this.notifyError(`Could not remove "${entry.file.filename}"`, error);
            } finally {
                const remaining = { ...this.pendingRemovals };
                delete remaining[fileId];
                this.pendingRemovals = remaining;
            }
        },

        /** Commit every pending removal now (page unload) */
        commitAllRemovals(opts = {}) {
            return Promise.all(Object.keys(this.pendingRemovals).map(id => this.commitRemoval(id, opts)));
        },

        /**
         * Analyze file for silence detection
         */
        async analyzeFile() {
            if (!this.currentFileId || this.analyzing || this.currentFileExporting) return;

            this.analyzing = true;
            this.rescanNotice = '';
            this.processingMessage = 'Analyzing...';

            try {
                const response = await fetch('/api/analyze', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_id: this.currentFileId })
                });
                const data = await response.json();

                if (!response.ok || !data.tracks) {
                    throw new Error(data.detail || 'Analysis failed');
                }

                this.setDetectedTracks(data.tracks);
                this.processingMessage = '';

                const file = this.uploadedFiles.find(f => f.id === this.currentFileId);
                if (file) {
                    file.status = 'analyzed';
                }

                // A release may be picked already (Discogs first): its mapping and track-count
                // check need the tracks that just arrived. The release, its sides and the
                // search results are left alone.
                if (this.selectedRelease) {
                    this.resetMappingToTargets();
                }

                if (this.detectedTracks.length === 0) {
                    // No waveform either: initWaveform keeps an existing one, so the retry's
                    // tracks would get no regions.
                    this.notify('No tracks were found in this recording. Try another release or adjust the silence settings.', { type: 'warning' });
                } else {
                    await this.initWaveform();
                }
            } catch (error) {
                console.error('Analysis failed:', error);
                this.notifyError('Analysis failed', error);
                this.processingMessage = '';
                // With a release picked, show the count mismatch (and its Smart Rescan /
                // Snap to Discogs options) instead of an empty mapping.
                if (this.selectedRelease) {
                    this.resetMappingToTargets();
                }
            } finally {
                this.analyzing = false;
            }
        },

        /**
         * Re-analyze file with current settings.  Runs immediately; an Undo
         * toast restores the previous boundaries and release.
         */
        async reanalyzeFile() {
            if (!this.currentFileId || this.analyzing || this.currentFileExporting) return;

            const fileId = this.currentFileId;
            const snapshot = this.snapshotEdits();

            // The Discogs search is independent of detection: its results stay.
            this.clearEditState();
            this.destroyWaveform();
            await this.analyzeFile();

            if (this.currentFileId !== fileId) return;
            if (this.detectedTracks.length === 0 && snapshot.detectedTracks.length > 0) {
                // Analysis failed (analyzeFile already showed the error). Nobody
                // confirmed the reset, so put the previous boundaries back.
                await this.restoreSnapshotWithWaveform(snapshot);
                return;
            }
            if (this.detectedTracks.length > 0) {
                this.notifyUndo('Re-analyzed. Boundaries and release were reset.',
                    () => this.restoreSnapshotWithWaveform(snapshot), { fileId });
            }
        },

        /**
         * Undo of a flow that replaced the whole analysis (re-analyze); its Undo toast
         * checks undoBlockedReason first.
         */
        async restoreSnapshotWithWaveform(snapshot) {
            this.destroyWaveform();
            this.restoreEdits(snapshot);   // regions are redrawn by initWaveform below
            await this.$nextTick();
            await this.initWaveform();
        },

        /**
         * Smart Rescan: ask the backend to sweep detection settings until it
         * finds the expected number of tracks.  Unlike reanalyzeFile(), this
         * keeps the Discogs search/selection intact so it works as a quick
         * retry from the track-count-mismatch warning too.
         */
        async smartRescan(target) {
            target = parseInt(target, 10);
            if (!this.currentFileId || this.analyzing || this.currentFileExporting) return;
            if (!target || target < 1 || target > MAX_GUIDED_TRACKS) {
                this.rescanNotice = `Enter how many songs the recording contains (1–${MAX_GUIDED_TRACKS}) first.`;
                return;
            }

            this.analyzing = true;
            this.rescanNotice = '';
            this.processingMessage = `Rescanning for ${target} tracks...`;
            this.pauseWaveform();

            try {
                const response = await fetch('/api/rescan', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        file_id: this.currentFileId,
                        expected_tracks: target
                    })
                });
                const data = await response.json();

                if (!response.ok || !data.tracks) {
                    throw new Error(data.detail || 'Smart rescan failed');
                }

                // A new detection: hand edits and ghost markers of the old one no longer apply.
                this.setDetectedTracks(data.tracks);
                this.processingMessage = '';

                const file = this.uploadedFiles.find(f => f.id === this.currentFileId);
                if (file) {
                    file.status = 'analyzed';
                }

                if (this.selectedRelease) {
                    this.resetMappingToTargets();
                }

                this.destroyWaveform();
                await this.initWaveform();

                this.rescanNotice = data.matched
                    ? `✅ Found exactly ${data.tracks.length} tracks.`
                    : `⚠️ Couldn't find exactly ${target} tracks — closest result is ${data.tracks.length}. Right-click the waveform to add or remove splits manually.`;
            } catch (error) {
                console.error('Smart rescan failed:', error);
                this.rescanNotice = '❌ Smart rescan failed: ' + error.message;
                this.processingMessage = '';
            } finally {
                this.analyzing = false;
            }
        },

        /**
         * Search Discogs for releases from the query box.  Safe to call twice: while a
         * search is running the second call is ignored.  Every search takes a new
         * sequence number, so replies (and tracklist loads) of a superseded search --
         * another file was selected meanwhile -- are dropped instead of shown.
         */
        async searchDiscogs() {
            if (this.searchLoading) return;
            if (!this.searchQuery.trim()) {
                this.searchNotice = 'Please enter a search query';
                return;
            }

            this.searchLoading = true;
            this.resetSearchResults();
            this.searchNotice = '';
            const seq = ++this.searchSeq;
            const query = this.searchQuery;

            try {
                const response = await fetch('/api/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query, max_results: SEARCH_PAGE_SIZE, page: 1 })
                });
                const data = await response.json();
                if (seq !== this.searchSeq) return;   // a file switch superseded this search

                // Surface the server's error (missing token, network down)
                // instead of corrupting searchResults with undefined.
                if (!response.ok || !Array.isArray(data.results)) {
                    throw new Error(data.detail || 'Search failed');
                }

                this.searchResults = data.results.map(newSearchCard);
                this.searchedQuery = query;
                this.searchPage = 1;
                this.searchHasMore = !!data.has_more;

                if (this.searchResults.length === 0) {
                    this.searchNotice = 'No results found. Try a different search query.';
                } else {
                    // Not awaited: the cards are already visible.
                    this.loadAllReleaseDetails(seq);
                }
            } catch (error) {
                if (seq !== this.searchSeq) return;
                console.error('Search failed:', error);
                this.resetSearchResults();
                this.searchNotice = 'Search failed: ' + error.message;
            } finally {
                if (seq === this.searchSeq) {
                    this.searchLoading = false;
                }
            }
        },

        /** Forget the shown results and their "Load more" state (not the query box) */
        resetSearchResults() {
            this.searchResults = [];
            this.detailsSettled = false;
            this.searchedQuery = '';
            this.searchPage = 0;
            this.searchHasMore = false;
            this.searchMoreLoading = false;
            this.searchMoreError = '';
            this.searchMoreNotice = '';
        },

        /**
         * "Load more": fetch the next page of the same query and append it, with its
         * tracklists loading like the first page's. At most SEARCH_MAX_PAGES fetches.
         */
        async loadMoreResults() {
            if (!this.searchHasMore || this.searchLoading || this.searchMoreLoading || this.searchPage >= SEARCH_MAX_PAGES) return;
            const seq = this.searchSeq;
            const page = this.searchPage + 1;
            this.searchMoreLoading = true;
            this.searchMoreError = '';
            this.searchMoreNotice = '';
            try {
                const response = await fetch('/api/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query: this.searchedQuery, max_results: SEARCH_PAGE_SIZE, page })
                });
                const data = await response.json();
                if (seq !== this.searchSeq) return;   // a new search/file replaced these results
                if (!response.ok || !Array.isArray(data.results)) {
                    throw new Error(data.detail || 'Search failed');
                }
                // Discogs' order can shift between requests: never show a release twice
                // (cards are keyed by id, and a duplicate key breaks the grid).
                const shown = new Set(this.searchResults.map(r => r.id));
                const added = data.results.filter(r => !shown.has(r.id)).map(newSearchCard);
                this.searchPage = page;
                this.searchHasMore = !!data.has_more && page < SEARCH_MAX_PAGES;
                if (added.length) {
                    this.detailsSettled = false;
                    this.searchResults = [...this.searchResults, ...added];
                    this.loadAllReleaseDetails(seq, added.map(r => r.id));
                } else {
                    // A full page of already shown ids leaves has_more true: Load more stays, so don't say "no more"
                    this.searchMoreNotice = this.searchHasMore ? 'No new results on this page.' : 'No more results.';
                }
            } catch (error) {
                if (seq !== this.searchSeq) return;
                console.error('Load more failed:', error);
                this.searchMoreError = "Couldn't load more results: " + error.message;
            } finally {
                if (seq === this.searchSeq) this.searchMoreLoading = false;
            }
        },

        /**
         * Fetch full tracklists for the given results (default: all shown) in display
         * order, RELEASE_DETAIL_CONCURRENCY at a time.  Stops as soon as searchSeq moves
         * on (new search or file switch).  Settled only when no result is still pending:
         * "Load more" can run beside the first page's loader.
         */
        async loadAllReleaseDetails(seq, ids = this.searchResults.map(r => r.id)) {
            let next = 0;
            const worker = async () => {
                while (seq === this.searchSeq && next < ids.length) {
                    await this.loadReleaseDetails(ids[next++], seq);
                }
            };
            await Promise.all(Array.from({ length: Math.min(RELEASE_DETAIL_CONCURRENCY, ids.length) }, worker));
            if (seq === this.searchSeq && !this.searchResults.some(r => r.detailStatus === 'pending')) {
                this.detailsSettled = true;
            }
        },

        /**
         * Fetch one release's full details and merge them into its result card.
         * Shares an in-flight request (card click vs. background loader) of the same
         * search; resolves true when the tracklist is loaded.
         */
        loadReleaseDetails(id, seq = this.searchSeq) {
            const existing = this.searchResults.find(r => r.id === id);
            if (existing && existing.tracks) return Promise.resolve(true);
            // Keyed by search too: a load left over from the previous file's search must
            // not be handed to this one (its reply is dropped, the card would stay pending).
            const key = `${seq}:${id}`;
            if (this._detailInflight[key]) return this._detailInflight[key];

            const promise = (async () => {
                try {
                    const response = await fetch(`/api/release/${id}`);
                    const data = await response.json();
                    if (!response.ok || !Array.isArray(data.tracks)) {
                        throw new Error(data.detail || 'Release fetch failed');
                    }
                    if (seq !== this.searchSeq) return false;
                    const result = this.searchResults.find(r => r.id === id);
                    if (!result) return false;
                    Object.assign(result, { ...data, cover_url: data.cover_url || result.cover_url, detailStatus: 'loaded' });
                    return true;
                } catch (error) {
                    if (seq === this.searchSeq) {
                        const result = this.searchResults.find(r => r.id === id);
                        if (result) result.detailStatus = 'error';
                    }
                    console.error('Release details failed:', error);
                    return false;
                } finally {
                    delete this._detailInflight[key];
                }
            })();
            this._detailInflight[key] = promise;
            return promise;
        },

        /**
         * Card click: make sure the tracklist is loaded, then select.  A card whose
         * load failed is retried by the click; the release is only ever selected
         * with its tracks.  The latest click wins: a later pick, Change release or a
         * file switch while this card loads clears _pendingPickId, and its reply then
         * only fills the card.
         */
        async chooseResult(result) {
            if (result.opening) return;
            if (result.tracks) {
                this.selectRelease(result);
                return;
            }
            const seq = this.searchSeq;
            this._pendingPickId = result.id;
            result.opening = true;
            try {
                const ok = await this.loadReleaseDetails(result.id, seq);
                if (ok && seq === this.searchSeq && this._pendingPickId === result.id) {
                    const fresh = this.searchResults.find(r => r.id === result.id);
                    if (fresh) this.selectRelease(fresh);
                }
            } finally {
                if (this._pendingPickId === result.id) this._pendingPickId = null;
                result.opening = false;
            }
        },

        /**
         * Select a release from search results.  Only a release whose tracklist has
         * loaded can be selected (everything downstream reads release.tracks).
         * Picks the side(s) the recording most likely covers, then places exactly that
         * many tracks with guided detection (or asks first when boundaries were edited
         * by hand, see afterTargetsChanged).
         */
        selectRelease(release) {
            if (!Array.isArray(release?.tracks)) return;
            this._pendingPickId = null;   // this pick replaces any card still loading, even when refused below
            if (this.currentFileExporting) {
                this.notify('This recording is exporting. Pick a release when the export has finished.', { type: 'warning', timeout: 5000 });
                return;
            }
            if (this.analyzing) {
                this.notify('Wait for track detection to finish before picking a release.', { type: 'warning', timeout: 5000 });
                return;
            }
            this.selectedRelease = release;
            this.loadAlbumDescription();
            const suggestion = this.suggestSides(
                release,
                this.currentFile?.musicDuration || this.currentFile?.duration
            );
            this.selectedSides = suggestion.sides;
            this.suggestedSides = suggestion.sides;
            this.sidesSuggestionReason = suggestion.reason;

            this.resetMappingToTargets();
            this.afterTargetsChanged();
        },

        /**
         * True while the open recording itself is being exported.  Not `isProcessing` alone: that
         * flag is global, and another recording may be opened and detected during an export.
         * Re-detecting the exporting recording would make the server reject the request with a
         * WebSocket `error` for that file, which the export handler shows as a failed export.
         */
        get currentFileExporting() {
            return this.isProcessing
                && this.uploadedFiles.find(f => f.id === this.currentFileId)?.status === 'processing';
        },

        /**
         * Release sides as chips (with counts/lengths) for the selected release
         */
        get sideChips() {
            return this.releaseSides(this.selectedRelease);
        },

        /**
         * Scope presets for the selected release: "Full album" plus one per
         * disc (sides pair up in order: A/B = disc 1, C/D = disc 2 ...).
         * Disc presets only exist when the release has more than 2 sides.
         */
        get sidePresets() {
            return this.presetsFor(this.sideChips);
        },

        /** Does the current selection exactly equal this side set? */
        sidesEqualSelection(sides) {
            return sides.length === this.selectedSides.length &&
                sides.every(key => this.selectedSides.includes(key));
        },

        /** Is this exact side set what the auto-suggestion picked by length? */
        isMatchedScope(sides) {
            return this.sidesSuggestionReason === 'length' &&
                sides.length === this.suggestedSides.length &&
                sides.every(key => this.suggestedSides.includes(key));
        },

        /** Muted note shown when the scope was defaulted rather than matched by length */
        get defaultScopeNote() {
            if (this.sidesSuggestionReason !== 'default' || this.sideChips.length <= 1) return '';
            if (!this.sidesEqualSelection(this.suggestedSides)) return '';
            return 'Defaulted to the full album. Pick a disc or side if this recording holds less.';
        },

        /** Apply a scope preset and re-run detection */
        applyPreset(sides) {
            // An export keeps the session busy: the backend would reject it and report the export as failed
            if (this.analyzing || this.currentFileExporting) return;
            this.selectedSides = this.sideChips.map(g => g.side).filter(key => sides.includes(key));
            this.resetMappingToTargets();
            this.afterTargetsChanged();
        },

        /**
         * Toggle a side chip (multi-select, at least one side stays selected)
         * and re-run detection for the new track count.
         */
        toggleSide(side) {
            if (this.analyzing || this.currentFileExporting) return;

            const isSelected = this.selectedSides.includes(side);
            if (isSelected && this.selectedSides.length === 1) return;

            const wanted = isSelected
                ? this.selectedSides.filter(key => key !== side)
                : [...this.selectedSides, side];
            // Keep release order regardless of click order
            this.selectedSides = this.sideChips.map(g => g.side).filter(key => wanted.includes(key));

            this.resetMappingToTargets();
            this.afterTargetsChanged();
        },

        /**
         * Release tracks on the selected sides, in release order, each carrying its
         * releaseIdx into selectedRelease.tracks.
         */
        get targetTracks() {
            if (!this.selectedRelease) return [];
            return this.releaseSides(this.selectedRelease)
                .filter(g => this.selectedSides.includes(g.side))
                .flatMap(g => g.tracks)
                .sort((a, b) => a.releaseIdx - b.releaseIdx);
        },

        /**
         * Point customMapping at targetTracks (detected track i -> i-th target; clamped to
         * the last target when there are more detected tracks than targets), un-reverse,
         * and recompute the track-count warning.  customMapping holds indices into
         * selectedRelease.tracks, so a side that is a subset maps correctly.  Run whenever
         * the release, the sides or the detected tracks change.
         */
        resetMappingToTargets() {
            const targets = this.targetTracks;
            const lastIdx = targets.length ? targets[targets.length - 1].releaseIdx : null;
            this.trackMappingReversed = false;
            this.customMapping = Array.from({ length: this.detectedTracks.length }, (_, i) =>
                targets[i]?.releaseIdx ?? lastIdx ?? i
            );
            this.trackCountMismatch = !!this.selectedRelease &&
                this.activeTrackCount !== targets.length;
        },

        /**
         * Install a detection result.  Detection replaces any hand edits, so the edit
         * state starts over and the ghost markers (Discogs-expected breaks, if any) are new.
         */
        setDetectedTracks(tracks, { expected = [] } = {}) {
            this.detectedTracks = tracks.map(track => ({
                ...track,
                editing: false,
                ignored: false
            }));
            this.boundariesEdited = false;
            this.editBanner = false;
            this.expectedBoundaries = [...expected];
        },

        /** Forget the edit flag, the banner and the ghost markers */
        resetEditState() {
            this.boundariesEdited = false;
            this.editBanner = false;
            this.expectedBoundaries = [];
        },

        /**
         * Record that the user changed boundaries by hand.  Anything that installs tracks
         * without detection (e.g. imported labels) must call this too, or picking a release
         * re-detects over them.
         */
        markEdited() {
            this.boundariesEdited = true;
            this.boundaryEditCount++;
        },

        /**
         * Re-run guided detection after the release or the sides changed, unless the user
         * hand-edited the boundaries: then keep them and ask (editBanner).
         */
        afterTargetsChanged() {
            if (this.boundariesEdited && this.detectedTracks.length > 0) {
                this.editBanner = true;
                this.clearExpectedMarkers();
                return Promise.resolve(false);
            }
            return this.redetectForTargets();
        },

        /**
         * Snap the boundaries to the Discogs lengths of the chosen sides: guided detection
         * again, replacing hand edits.  An Undo toast puts the previous boundaries back.
         */
        async snapToDiscogs() {
            if (this.analyzing || this.currentFileExporting || !this.selectedRelease || this.targetTracks.length < 1) return;

            const fileId = this.currentFileId;
            const snapshot = this.snapshotEdits();
            const editCount = this.boundaryEditCount;
            this.editBanner = false;

            const ok = await this.redetectForTargets();
            if (this.currentFileId !== fileId) return;
            // The user edited during detection (the reply was dropped): their state stands, even an emptied one
            if (ok) {
                this.notifyUndo('Snapped the boundaries to the Discogs lengths.',
                    () => this.undoSnap(snapshot), { fileId, type: 'success' });
            } else if (this.boundaryEditCount === editCount && this.detectedTracks.length === 0 && snapshot.detectedTracks.length > 0) {
                // Found nothing (redetectForTargets already warned): keep the hand edits rather
                // than leave an empty editor with nothing to undo.
                this.rescanNotice = '';
                await this.restoreSnapshotWithWaveform(snapshot);
            }
        },

        /** Undo of Snap to Discogs: the boundaries, mapping, sides and markers from before */
        async undoSnap(snapshot) {
            this.rescanNotice = '';   // "Placed N tracks" described the snap, not the restored edits
            if (this.waveform || snapshot.detectedTracks.length === 0) {
                this.restoreEdits(snapshot);
            } else {
                // A snap can leave no waveform (it found nothing): build one for the restored tracks
                await this.restoreSnapshotWithWaveform(snapshot);
            }
        },

        /** True when the loaded description belongs to the release now selected and is worth showing. */
        get albumDescriptionVisible() {
            const state = this.albumDescription;
            return !!this.selectedRelease && state.releaseId === this.selectedRelease.id
                && ['loading', 'found', 'none', 'error'].includes(state.status);
        },

        /**
         * Fetch the Wikipedia description of the selected release for the preview. Skipped (no
         * request) when the setting is off. A reply for a release that is no longer selected is
         * ignored. Never touches detection or tracks. Called explicitly, not from a watcher.
         */
        async loadAlbumDescription() {
            const releaseId = this.selectedRelease?.id;
            this.albumDescriptionExpanded = false;
            if (!releaseId || this.config.album_description !== true) {
                this.albumDescription = { releaseId: null, status: 'idle', text: '', title: '', url: '' };
                return;
            }
            this.albumDescription = { releaseId, status: 'loading', text: '', title: '', url: '' };
            let result;
            try {
                const response = await fetch(`/api/release/${releaseId}/description`);
                // A 404 means the release itself can't be loaded; the preview treats it as an error.
                result = response.ok ? await response.json() : { status: 'error' };
            } catch (error) {
                result = { status: 'error' };
            }
            // Stale reply: another release was selected, or the setting was switched off meanwhile.
            if (this.selectedRelease?.id !== releaseId || this.config.album_description !== true) {
                // Dropping the reply of the lookup that still owns the state (release cleared, e.g.
                // by delete track, and about to be restored by Undo) would leave "loading" forever,
                // and restoreEdits skips the reload for the same release id: go back to idle.
                if (this.albumDescription.releaseId === releaseId && this.albumDescription.status === 'loading') {
                    this.albumDescription = { releaseId: null, status: 'idle', text: '', title: '', url: '' };
                }
                return;
            }
            this.albumDescription = {
                releaseId, status: result.status || 'error',
                text: result.text || '', title: result.title || '', url: result.url || ''
            };
        },

        /**
         * "Keep my edits" on the hand-edit banner: the boundaries stay, the mapping
         * follows the new release / sides.
         */
        keepMyEdits() {
            this.editBanner = false;
            this.resetMappingToTargets();
        },

        /**
         * Place exactly as many tracks as the chosen sides have (POST /api/analyze-guided):
         * Discogs lengths where known, side changes as hints.  Resolves true when tracks
         * were placed.
         */
        async redetectForTargets() {
            const targets = this.targetTracks;
            if (targets.length < 1 || !this.currentFileId || this.analyzing || this.currentFileExporting) return false;
            if (targets.length > MAX_GUIDED_TRACKS) {
                this.rescanNotice = `⚠️ The chosen sides hold ${targets.length} tracks; detection places at most ${MAX_GUIDED_TRACKS} at a time.${this.sideChips.length > 1 ? ' Pick a disc or side.' : ''}`;
                this.clearExpectedMarkers();   // they belonged to the previous sides
                return false;
            }

            const fileId = this.currentFileId;
            // A reply is stale if the file, the release or the boundaries changed while it was computed
            const releaseId = this.selectedRelease?.id ?? null;
            const editCount = this.boundaryEditCount;
            const isSuperseded = () => this.currentFileId !== fileId
                || (this.selectedRelease?.id ?? null) !== releaseId
                || this.boundaryEditCount !== editCount;
            let succeeded = false;
            this.analyzing = true;
            this.rescanNotice = '';
            this.processingMessage = 'Placing track boundaries...';
            this.pauseWaveform();

            try {
                const response = await fetch('/api/analyze-guided', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        file_id: fileId,
                        expected_tracks: targets.length,
                        durations: targets.map(track => this.parseTrackDuration(track.duration)),
                        side_breaks: this.sideBreaks(targets)
                    })
                });
                const data = await response.json();

                if (!response.ok || !data.tracks) {
                    throw new Error(data.detail || 'Boundary detection failed');
                }

                if (isSuperseded()) {
                    this.processingMessage = '';
                    this.discardSupersededDetection(fileId, releaseId);
                    return false;
                }

                this.setDetectedTracks(data.tracks, {
                    // Only a length-driven placement has expected breaks to show
                    expected: data.method === 'durations'
                        ? (data.boundaries || []).map(b => b.expected).filter(time => Number.isFinite(time))
                        : []
                });
                this.processingMessage = '';

                const file = this.uploadedFiles.find(f => f.id === fileId);
                if (file) {
                    file.status = 'analyzed';
                }
                this.applyScan(fileId, data);

                if (this.selectedRelease) {
                    this.resetMappingToTargets();
                }

                // initWaveform keeps an existing waveform, and with it the old regions
                this.destroyWaveform();
                if (data.tracks.length === 0) {
                    // No waveform either, or the retry's tracks would get no regions
                    this.notify('No tracks were found in this recording. Try another release or adjust the silence settings.', { type: 'warning' });
                } else {
                    await this.$nextTick();
                    await this.initWaveform();
                }

                if (data.method === 'durations') {
                    const snapped = (data.boundaries || []).filter(b => b.snapped).length;
                    this.rescanNotice = `✅ Placed ${data.tracks.length} tracks from Discogs lengths (${snapped} of ${(data.boundaries || []).length} boundaries snapped to silence).`;
                } else {
                    this.rescanNotice = data.matched
                        ? `✅ Placed ${data.tracks.length} tracks by track count (Discogs lengths unavailable).`
                        : `⚠️ Couldn't find exactly ${targets.length} tracks — closest result is ${data.tracks.length}. Right-click the waveform to add or remove splits manually.`;
                }
                // Nothing found is not a success: the caller must not offer an Undo of it
                succeeded = data.tracks.length > 0;
            } catch (error) {
                console.error('Guided detection failed:', error);
                this.processingMessage = '';
                if (isSuperseded()) {   // the failed run belonged to state the user has since changed
                    this.discardSupersededDetection(fileId, releaseId);
                    return false;
                }
                this.notifyError('Boundary detection failed', error);
                this.clearExpectedMarkers();   // they belonged to the previous sides
                // With no tracks, show the count mismatch and its options instead of an empty mapping
                if (this.selectedRelease) {
                    this.resetMappingToTargets();
                }
            } finally {
                this.analyzing = false;
            }
            return succeeded;
        },

        /**
         * A guided reply arrived after the user changed things.  Same file and release means a
         * hand edit during detection: the edits stay, the ghost markers of the previous sides go
         * and the "adjusted by hand" banner asks what to do (as afterTargetsChanged would).  A
         * different file or release made the reply irrelevant: drop it silently.
         */
        discardSupersededDetection(fileId, releaseId) {
            if (this.currentFileId !== fileId || (this.selectedRelease?.id ?? null) !== releaseId) return;
            this.clearExpectedMarkers();
            this.editBanner = this.detectedTracks.length > 0;
        },

        /**
         * Indices (into `targets`) where a lettered side changes (A2 -> B1 is [2]): the
         * backend anchors each side on a long silence near that point.  Numeric-only
         * positions ("1", "2") never imply a flip.
         */
        sideBreaks(targets) {
            const sideOf = track => /^([A-Za-z]+)/.exec(String(track.position || '').trim())?.[1]?.toUpperCase();
            return targets.flatMap((track, index) => {
                if (!index) return [];
                const before = sideOf(targets[index - 1]);
                const after = sideOf(track);
                return before && after && before !== after ? [index] : [];
            });
        },

        /**
         * The count warning, except while the first detection runs: a pick starts it, and
         * "0 detected" is no finding yet (its Snap to Discogs button would race it too).
         * With tracks present it stays up during a Smart Rescan, whose spinner lives in it.
         */
        get showTrackCountMismatch() {
            return this.trackCountMismatch && (this.detectedTracks.length > 0 || !this.analyzing);
        },

        /**
         * Drop the selected release but keep the search results and the current
         * detection/waveform so another release can be picked.
         */
        changeRelease() {
            // A running detection would install its tracks and markers on a release that is gone;
            // an export of this recording needs its release, and selectRelease is refused meanwhile
            if (this.analyzing || this.currentFileExporting) return;
            this.selectedRelease = null;
            this._pendingPickId = null;   // a card still loading must not bring a release back
            this.selectedSides = [];
            this.suggestedSides = [];
            this.sidesSuggestionReason = '';
            this.releaseIdLoading = false;   // a pasted lookup in flight is dropped (its call token guards the reset)
            this.trackMappingReversed = false;
            this.trackCountMismatch = false;
            this.customMapping = [];
            this.editBanner = false;
            this.clearExpectedMarkers();   // they belong to the release's lengths
        },

        /**
         * Back to the search card (tracks exist, no release chosen); searches only if it has no results.
         * Without a token the search card is hidden and a search would only fail: open the setup instead.
         */
        tryDiscogsAgain() {
            if (this.analyzing) return;
            if (!this.discogsConfigured) {
                this.setupRequired = true;
                return;
            }
            if (!this.searchQuery.trim()) this.searchQuery = this.cleanFilename(this.currentFile?.filename || '');
            if (this.searchResults.length === 0) this.searchDiscogs();
            this.$nextTick(() => {
                if (typeof document === 'undefined') return;
                document.getElementById('discogs-search')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                document.getElementById('discogs-query')?.focus({ preventScroll: true });
            });
        },

        // ---- Music span (for the result cards' fit indicator) ------------

        /** Record the music span (recording minus lead-in/run-out) of a file */
        applyScan(fileId, scan) {
            const file = this.uploadedFiles.find(f => f.id === fileId);
            if (!file || !(scan.music_end > scan.music_start)) return;
            file.musicDuration = scan.music_end - scan.music_start;
        },

        /**
         * Ask the server whether the background scan finished (it may have completed
         * before this file was selected / the WebSocket connected).
         */
        async fetchScan(fileId) {
            try {
                const response = await fetch(`/api/scan/${fileId}`);
                if (!response.ok) return;
                const data = await response.json();
                if (data.ready) this.applyScan(fileId, data);
            } catch (error) {
                console.warn('Scan status unavailable:', error);
            }
        },

        // ---- Release cards: lengths, sides and fit -----------------------

        parseDiscogsRelease,

        /** Select the exact release the user named by ID or link, skipping search. */
        async openReleaseById() {
            if (this.analyzing || this.releaseIdLoading) return;
            const ref = parseDiscogsRelease(this.releaseIdInput);
            if (!ref) {
                this.releaseIdNotice = 'Paste a Discogs release link (discogs.com/release/…) or type its number, e.g. 2874426.';
                return;
            }
            if (ref.master) {
                this.releaseIdNotice = `That is a master release (all versions of the album). Open it on Discogs, pick the exact pressing you own, and paste that release's link.`;
                return;
            }
            this.releaseIdNotice = '';
            this.releaseIdLoading = true;
            // A pick like a card click: a later pick, Change release or a file switch while
            // this loads clears _pendingPickId, and the reply is then dropped.
            const call = ++this._releaseIdCall;
            const pick = `id:${call}`;
            const fileId = this.currentFileId;
            const superseded = () => this._pendingPickId !== pick || this.currentFileId !== fileId;
            this._pendingPickId = pick;
            try {
                const response = await fetch(`/api/release/${ref.id}`);
                const data = await response.json().catch(() => ({}));
                if (superseded()) return;
                if (!response.ok || !Array.isArray(data.tracks)) {
                    this.releaseIdNotice = response.status === 404
                        ? `Discogs release ${ref.id} was not found (or Discogs is unavailable right now).`
                        : `Couldn't load release ${ref.id}: ${data.detail || 'unknown error'}`;
                    return;
                }
                if (!data.tracks.length) {
                    this.releaseIdNotice = `Release ${ref.id} has no tracklist on Discogs, so it can't guide the splits.`;
                    return;
                }
                this.releaseIdInput = '';
                this.selectRelease({ ...data, detailStatus: 'loaded' });
            } catch (error) {
                if (!superseded()) this.releaseIdNotice = `Couldn't reach the server to load release ${ref.id}.`;
            } finally {
                if (this._pendingPickId === pick) this._pendingPickId = null;
                if (this._releaseIdCall === call) this.releaseIdLoading = false;
            }
        },

        /**
         * Parse a Discogs duration string ("5:24", "1:02:03") into seconds.
         * Returns null when missing/unparseable/zero.
         */
        parseTrackDuration(str) {
            if (!str || typeof str !== 'string') return null;
            const parts = str.trim().split(':');
            if (parts.length < 2 || parts.length > 3) return null;
            if (!parts.every(p => /^\d+$/.test(p))) return null;
            const nums = parts.map(p => parseInt(p, 10));
            const secs = nums.length === 3
                ? nums[0] * 3600 + nums[1] * 60 + nums[2]
                : nums[0] * 60 + nums[1];
            return secs > 0 ? secs : null;
        },

        /**
         * How many of a release's tracks carry a usable length.
         * status: 'full' | 'partial' | 'none'
         */
        releaseDurationInfo(release) {
            const tracks = release?.tracks || [];
            const total = tracks.length;
            const withDuration = tracks.filter(t => this.parseTrackDuration(t.duration) !== null).length;
            let status = 'none';
            if (total > 0 && withDuration === total) status = 'full';
            else if (withDuration > 0) status = 'partial';
            return { total, withDuration, status };
        },

        /**
         * Group a release's tracks by vinyl side (leading letters of the
         * position, e.g. "A1" -> "A").  Tracks without a letter prefix
         * ("1", "2") share a single group with side ''.  Groups keep the
         * order in which they first appear; every track carries its
         * original index in release.tracks as releaseIdx.
         * totalSeconds is null unless every track in the group has a length.
         */
        releaseSides(release) {
            const groups = [];
            const bySide = {};
            (release?.tracks || []).forEach((t, releaseIdx) => {
                const m = /^([A-Za-z]+)/.exec(String(t.position || '').trim());
                const side = m ? m[1].toUpperCase() : '';
                if (!bySide[side]) {
                    bySide[side] = { side, tracks: [], totalSeconds: 0 };
                    groups.push(bySide[side]);
                }
                const g = bySide[side];
                g.tracks.push({ ...t, releaseIdx });
                const secs = this.parseTrackDuration(t.duration);
                g.totalSeconds = (g.totalSeconds !== null && secs !== null) ? g.totalSeconds + secs : null;
            });
            return groups;
        },

        /** Short "A·4  B·5" summary of a release's sides */
        sidesSummary(release) {
            return this.releaseSides(release)
                .map(g => `${this.sideLabel(g.side)}·${g.tracks.length}`)
                .join('  ');
        },

        /** Display label for a side key */
        sideLabel(side) {
            return side === '' ? 'All' : side;
        },

        /**
         * Search results after applying the "only with track lengths" filter.
         * Discogs relevance order is preserved until every tracklist is in; then
         * releases that fit the recording come first (stable within each group).
         */
        get filteredResults() {
            // Unloaded (tracks null) and failed results stay visible: the
            // filter only hides releases KNOWN to lack full lengths.
            const shown = this.onlyWithLengths
                ? this.searchResults.filter(r => !r.tracks || this.releaseDurationInfo(r).status === 'full')
                : this.searchResults;
            if (!this.detailsSettled) return shown;
            return [
                ...shown.filter(r => this.cardFit(r)?.state === 'fit'),
                ...shown.filter(r => this.cardFit(r)?.state !== 'fit')
            ];
        },

        /**
         * Scope presets for a release's sides: "Full album" plus one per disc
         * (sides pair up in order: A/B = disc 1, C/D = disc 2 ...).
         * Disc presets only exist when the release has more than 2 sides.
         */
        presetsFor(groups) {
            if (groups.length <= 1) return [];
            const sum = gs => gs.every(g => g.totalSeconds !== null)
                ? gs.reduce((t, g) => t + g.totalSeconds, 0) : null;
            const make = (label, gs) => ({
                label,
                sides: gs.map(g => g.side),
                trackCount: gs.reduce((t, g) => t + g.tracks.length, 0),
                totalSeconds: sum(gs)
            });
            const presets = [make('Full album', groups)];
            if (groups.length > 2) {
                for (let i = 0; i < groups.length; i += 2) {
                    const gs = groups.slice(i, i + 2);
                    presets.push(make(`Disc ${i / 2 + 1} (${gs.map(g => g.side).join('/')})`, gs));
                }
            }
            return presets;
        },

        /**
         * Human label for a set of sides, matching the scope presets
         * ("Full album", "Disc 2 (C/D)"); falls back to "Side A" / "Sides A+B".
         */
        scopeLabel(release, sides) {
            const groups = this.releaseSides(release);
            if (groups.length <= 1 || sides.length === groups.length) return 'Full album';
            const preset = this.presetsFor(groups).find(p =>
                p.sides.length === sides.length && p.sides.every(k => sides.includes(k)));
            if (preset) return preset.label;
            const names = sides.map(k => this.sideLabel(k));
            return names.length === 1 ? `Side ${names[0]}` : `Sides ${names.join('+')}`;
        },

        /**
         * How well a loaded release fits the current file's music length.
         * Returns null when unknown (tracks not loaded, no lengths, no file
         * length yet), else
         *   { state: 'fit', label, scopeSeconds, fileSeconds, usesMusic, diff }
         *   { state: 'nofit', fullSeconds, fileSeconds, usesMusic }
         */
        cardFit(release) {
            if (!release?.tracks) return null;
            const music = this.currentFile?.musicDuration;
            const usesMusic = music > 0;
            const fileSeconds = usesMusic ? music : this.currentFile?.duration;
            if (!(fileSeconds > 0)) return null;

            const groups = this.releaseSides(release);
            if (!groups.length) return null;
            const sumOf = sides => {
                const gs = groups.filter(g => sides.includes(g.side));
                return gs.every(g => g.totalSeconds !== null)
                    ? gs.reduce((t, g) => t + g.totalSeconds, 0) : null;
            };

            const fullSeconds = sumOf(groups.map(g => g.side));
            // suggestSides() has no candidates for single-sided releases.
            const suggestion = groups.length <= 1
                ? { sides: groups.map(g => g.side), matched: fullSeconds !== null && Math.abs(fullSeconds - fileSeconds) / fileSeconds <= SIDE_FIT_TOLERANCE }
                : this.suggestSides(release, fileSeconds);

            if (suggestion.matched) {
                const scopeSeconds = sumOf(suggestion.sides);
                if (scopeSeconds !== null) {
                    return {
                        state: 'fit',
                        label: this.scopeLabel(release, suggestion.sides),
                        scopeSeconds,
                        fileSeconds,
                        usesMusic,
                        diff: Math.abs(scopeSeconds - fileSeconds) / fileSeconds
                    };
                }
            }
            if (fullSeconds === null) return null;
            return { state: 'nofit', fullSeconds, fileSeconds, usesMusic };
        },

        /** Card text for a fit result ('' when unknown) */
        fitText(fit) {
            if (!fit) return '';
            const what = fit.usesMusic ? 'music' : 'file';
            if (fit.state === 'fit') {
                return `${fit.label} ${this.formatDuration(fit.scopeSeconds)} · your ${what} ${this.formatDuration(fit.fileSeconds)} ✓`;
            }
            return `Lengths don't fit this file (full album ${this.formatDuration(fit.fullSeconds)} vs ${what} ${this.formatDuration(fit.fileSeconds)})`;
        },

        /** Id of the single closest fit, once all details are in (else null) */
        get bestFitId() {
            if (!this.detailsSettled) return null;
            let best = null;
            for (const r of this.searchResults) {
                const fit = this.cardFit(r);
                if (fit?.state === 'fit' && (best === null || fit.diff < best.diff)) {
                    best = { id: r.id, diff: fit.diff };
                }
            }
            return best ? best.id : null;
        },

        /**
         * Decide which side(s) the recording most likely contains.
         *
         * Candidates are every contiguous run of sides in release order
         * (A, B, A-B, B-C, A-D ...) whose total length is fully known.  The
         * run closest to the recording length wins if within SIDE_FIT_TOLERANCE.
         * Near-ties (within SIDE_FIT_TIE) prefer the full album, then a
         * disc-aligned run (even start index, even length), then anything else.
         *
         * usedSides lists sides already claimed for the same release by other
         * recordings; overlapping runs are skipped unless nothing else is
         * left.  With no length match, the default is the first disc-aligned
         * run of unused sides when some are used, otherwise the full album.
         *
         * Returns { sides: [keys], matched: bool, reason: 'length'|'default'|'single' }.
         */
        suggestSides(release, recordingSeconds, usedSides = []) {
            const groups = this.releaseSides(release);
            const keys = groups.map(g => g.side);
            const n = groups.length;
            if (n <= 1) {
                return { sides: keys, matched: false, reason: 'single' };
            }

            const runs = [];
            for (let i = 0; i < n; i++) {
                let total = 0;
                for (let j = i; j < n; j++) {
                    total = (total !== null && groups[j].totalSeconds !== null)
                        ? total + groups[j].totalSeconds : null;
                    const len = j - i + 1;
                    runs.push({
                        sides: keys.slice(i, j + 1),
                        total,
                        rank: len === n ? 0 : (i % 2 === 0 && len % 2 === 0 ? 1 : 2)
                    });
                }
            }

            const used = new Set(usedSides);
            const free = runs.filter(r => !r.sides.some(k => used.has(k)));
            const pool = free.length ? free : runs;

            if (recordingSeconds > 0) {
                const scored = pool
                    .filter(r => r.total !== null)
                    .map(r => ({ ...r, diff: Math.abs(r.total - recordingSeconds) / recordingSeconds }));
                if (scored.length) {
                    const bestDiff = Math.min(...scored.map(r => r.diff));
                    if (bestDiff <= SIDE_FIT_TOLERANCE) {
                        const near = scored
                            .filter(r => r.diff <= bestDiff + SIDE_FIT_TIE)
                            .sort((x, y) => x.rank - y.rank || x.diff - y.diff);
                        return { sides: near[0].sides, matched: true, reason: 'length' };
                    }
                }
            }

            // No length match: prefer the first disc of still-unused sides.
            if (used.size > 0 && free.length) {
                const aligned = free.find(r => r.rank === 1 && r.sides.length === 2);
                if (aligned) {
                    return { sides: aligned.sides, matched: false, reason: 'default' };
                }
                const firstFree = keys.find(k => !used.has(k));
                if (firstFree !== undefined) {
                    return { sides: [firstFree], matched: false, reason: 'default' };
                }
            }

            return { sides: keys, matched: false, reason: 'default' };
        },

        /**
         * Reverse track mapping order
         */
        reverseMapping() {
            if (this.analyzing) return;
            this.trackMappingReversed = !this.trackMappingReversed;

            if (this.trackMappingReversed) {
                // Reverse within the chosen sides' tracks (indices into selectedRelease.tracks)
                const targets = this.targetTracks;
                const numTracks = this.detectedTracks.length;
                this.customMapping = Array.from({ length: numTracks }, (_, i) => {
                    if (!targets.length) return numTracks - 1 - i;
                    const target = Math.min(Math.max(numTracks - 1 - i, 0), targets.length - 1);
                    return targets[target].releaseIdx;
                });
            } else {
                this.resetMappingToTargets();
            }
        },

        /**
         * Update custom mapping when user changes dropdown
         */
        updateCustomMapping() {
            if (this.analyzing) return;   // the select is disabled while detecting; see toggleTrackIgnored
            this.trackMappingReversed = false;
        },

        /**
         * Map detected track idx to release track dtIdx (the row's dropdown).  The dropdown is
         * disabled while detecting, which would replace the mapping anyway.
         */
        setMapping(idx, value) {
            if (this.analyzing) return;
            const mapping = [...this.customMapping];
            mapping[idx] = Number(value);
            this.customMapping = mapping;
            this.updateCustomMapping();
        },

        /** Release track mapped to detected track idx, or null (no release / unmapped) */
        mappedTrack(idx) {
            if (!this.selectedRelease || !(idx >= 0)) return null;
            const releaseIdx = Number(this.customMapping[idx]);
            if (!Number.isInteger(releaseIdx)) return null;
            return this.selectedRelease.tracks?.[releaseIdx] || null;
        },

        /** Label of a release track in the mapping dropdown: "B5 - Title" */
        trackOptionLabel(releaseTrack) {
            return `${releaseTrack.position} - ${releaseTrack.title}`;
        },

        /** Discogs length (seconds) of the track mapped to row idx, or null */
        discogsLength(idx) {
            const mapped = this.mappedTrack(idx);
            return mapped ? this.parseTrackDuration(mapped.duration) : null;
        },

        /** Detected minus Discogs length in seconds, or null when either is unknown */
        trackDelta(track, idx) {
            const discogsSeconds = this.discogsLength(idx);
            if (discogsSeconds === null || discogsSeconds === undefined || !Number.isFinite(track.duration)) return null;
            return track.duration - discogsSeconds;
        },

        /**
         * Delta colours.  On the card background (#FAF9F6) the text contrast is 5.1:1 for good,
         * 5.4:1 for warn and 6.2:1 for bad; the sign and the number carry the meaning too.
         */
        deltaColor(delta) {
            if (delta === null || delta === undefined) return '#5E5E5A';
            const magnitude = Math.abs(delta);
            if (magnitude <= DELTA_GOOD_S) return '#0B7A55';
            if (magnitude <= DELTA_WARN_S) return '#8A5F00';
            return '#B3261E';
        },

        deltaText(delta) {
            if (delta === null || delta === undefined) return '—';
            return `${delta < 0 ? '−' : '+'}${Math.round(Math.abs(delta))}s`;
        },

        /** Swatch colour of a table row: the colour of its waveform region */
        trackSwatchColor(track, idx) {
            return this.getTrackColor(idx, track.ignored);
        },

        /** End of the recording in seconds (Infinity while it is not known yet) */
        recordingEnd() {
            return this.playDuration || this.currentFile?.duration || Infinity;
        },

        /**
         * Why a typed Start/End/Length would break the track table, or '' when it is fine.
         * Typing is held to a stricter rule than dragging (which clamps): a typo such as End "5"
         * on a track starting at 10 must be refused, not turned into a 1 s track, and an edit
         * must not squeeze a neighbour below MIN_TRACK_LENGTH.  start/end are already capped.
         */
        typedBoundsProblem(idx, start, end, which = 'end') {
            const track = this.detectedTracks[idx];
            const previous = this.detectedTracks[idx - 1];
            const next = this.detectedTracks[idx + 1];
            if (end <= start) {
                return which === 'start'
                    ? `Track ${track.number}: the start must be before its end.`
                    : `Track ${track.number}: the end must be after its start.`;
            }
            if (end - start < MIN_TRACK_LENGTH - 1e-6) {
                return `Track ${track.number} must last at least ${MIN_TRACK_LENGTH} s.`;
            }
            if (next && end > next.start && next.end - end < MIN_TRACK_LENGTH - 1e-6) {
                return `That would leave track ${next.number} shorter than ${MIN_TRACK_LENGTH} s.`;
            }
            if (previous && start < previous.end && start - previous.start < MIN_TRACK_LENGTH - 1e-6) {
                return `That would leave track ${previous.number} shorter than ${MIN_TRACK_LENGTH} s.`;
            }
            return '';
        },

        /**
         * Typed Start/End (inline Adjust): apply, or refuse with a warning.  The field always ends up
         * showing the stored value, so text that was not applied (or was capped) never stays on screen.
         */
        applyTypedBounds(idx, which, value, input) {
            const track = this.detectedTracks[idx];
            if (!track) return false;
            const typed = parseFloat(value);
            let changed = false;
            if (!isNaN(typed) && typed >= 0) {
                const start = which === 'start' ? typed : track.start;
                const end = Math.min(which === 'end' ? typed : track.end, this.recordingEnd());
                const problem = this.typedBoundsProblem(idx, start, end, which);
                if (problem) {
                    this.notify(problem, { type: 'warning', timeout: 5000 });
                } else {
                    changed = this.applyTrackBounds(idx, start, end);
                    this.syncRegionsAround(idx);
                }
            }
            if (input) input.value = track[which];
            return changed;
        },

        /** Update track start time (inline Adjust) */
        updateTrackStart(idx, value, input = null) {
            return this.applyTypedBounds(idx, 'start', value, input);
        },

        /** Update track end time (inline Adjust) */
        updateTrackEnd(idx, value, input = null) {
            return this.applyTypedBounds(idx, 'end', value, input);
        },

        /** "3:45", "3:45.5", "1:02:03" or plain seconds -> seconds; null if unreadable. */
        parseLengthInput(text) {
            const trimmed = String(text ?? '').trim();
            if (/^\d+(\.\d+)?$/.test(trimmed)) return parseFloat(trimmed);
            const match = trimmed.match(/^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/);
            if (!match) return null;
            const [, hours, minutes, seconds] = match;
            // With hours, minutes and seconds must be below 60; "3:75" is not a length
            if (parseFloat(seconds) >= 60 || (hours && parseInt(minutes, 10) >= 60)) return null;
            return (hours ? parseInt(hours, 10) * 3600 : 0) + parseInt(minutes, 10) * 60 + parseFloat(seconds);
        },

        /**
         * Typed length (e.g. from a streaming service): keep the start, end = start + length.
         * A next track that began exactly at the old end follows the cut, so nothing falls
         * between them; overlaps are limited exactly like dragging.
         */
        setTrackLength(idx, text, input = null) {
            const track = this.detectedTracks[idx];
            if (!track) return false;
            const length = this.parseLengthInput(text);
            const showLength = () => { if (input) input.value = this.formatDuration(track.duration); };
            if (!(length >= MIN_TRACK_LENGTH)) {
                showLength();
                this.notify('Type a length like 3:45 (minutes:seconds).', { type: 'warning', timeout: 4000 });
                return false;
            }
            const next = this.detectedTracks[idx + 1];
            const touching = !!next && Math.abs(next.start - track.end) < TOUCHING_CUTS_S;
            const newEnd = Math.min(track.start + length, this.recordingEnd());   // never past the recording
            const problem = this.typedBoundsProblem(idx, track.start, newEnd);
            if (problem) {
                showLength();
                this.notify(problem, { type: 'warning', timeout: 5000 });
                return false;
            }
            const changed = this.applyTrackBounds(idx, track.start, newEnd);
            if (changed && touching && track.end < next.start) {
                next.start = track.end;
                next.duration = next.end - next.start;
            }
            if (changed) this.syncRegionsAround(idx);
            showLength();
            if (newEnd - track.start < length - 0.05) {
                this.notify(`Track ${track.number} can only be ${this.formatDuration(track.duration)} here (the end of the recording is in the way).`, { type: 'info', timeout: 5000 });
            }
            return changed;
        },

        /**
         * Move track idx to [start, end].  Neighbours are only pushed when the regions would
         * overlap (gaps are preserved); every track keeps at least MIN_TRACK_LENGTH.
         * Returns true when anything changed (and then marks the boundaries hand-edited).
         */
        applyTrackBounds(idx, start, end) {
            const tracks = this.detectedTracks;
            const track = tracks[idx];
            if (!track || !Number.isFinite(start) || !Number.isFinite(end)) return false;
            const prev = tracks[idx - 1];
            const next = tracks[idx + 1];
            const EPS = 1e-6;
            const MIN = MIN_TRACK_LENGTH;

            const startMoved = Math.abs(start - track.start) > EPS;
            const endMoved = Math.abs(end - track.end) > EPS;
            if (!startMoved && !endMoved) return false;

            // Never past the end of the recording (typed Start/End, Length and region updates all land here)
            const limit = this.recordingEnd();
            start = Math.min(Math.max(0, start), limit);
            end = Math.min(end, limit);
            if (startMoved && endMoved) {
                // Whole-region drag: keep the length, stop at the neighbours' minimum
                const length = end - start;
                if (next && end > next.end - MIN) { end = next.end - MIN; start = end - length; }
                if (prev && start < prev.start + MIN) { start = prev.start + MIN; end = start + length; }
                if (start < 0) { end -= start; start = 0; }
            } else if (endMoved) {
                if (next) end = Math.min(end, next.end - MIN);
                end = Math.max(end, start + MIN);
            } else {
                if (prev) start = Math.max(start, prev.start + MIN);
                start = Math.min(start, end - MIN);
            }

            const valid = end - start >= MIN - EPS &&
                (!next || end <= next.start || next.end - end >= MIN - EPS) &&
                (!prev || start >= prev.end || start - prev.start >= MIN - EPS);
            if (!valid) return false;

            const changed = Math.abs(start - track.start) > EPS || Math.abs(end - track.end) > EPS;
            if (!changed) return false;

            track.start = start;
            track.end = end;
            track.duration = end - start;
            if (next && end > next.start) {
                next.start = end;
                next.duration = next.end - next.start;
            }
            if (prev && start < prev.end) {
                prev.end = start;
                prev.duration = prev.end - prev.start;
            }
            this.markEdited();
            return true;
        },

        /** Push a track's values onto its waveform region */
        syncRegion(track) {
            if (!track || !this.waveformRegions) return;
            const region = this.waveformRegions.getRegions().find(r => r.id === `track-${track.number}`);
            if (region && (Math.abs(region.start - track.start) > 1e-6 || Math.abs(region.end - track.end) > 1e-6)) {
                region.setOptions({ start: track.start, end: track.end });
            }
        },

        syncRegionsAround(idx) {
            [idx - 1, idx, idx + 1].forEach(i => this.syncRegion(this.detectedTracks[i]));
        },

        /**
         * Initialize waveform visualization
         */
        async initWaveform() {
            if (!this.currentFileId || this.waveform) return;

            this.waveformLoading = true;

            try {
                if (typeof WaveSurfer === 'undefined') {
                    throw new Error('WaveSurfer library not loaded. Please refresh the page.');
                }

                if (typeof WaveSurfer.Regions === 'undefined') {
                    throw new Error('WaveSurfer Regions plugin not loaded. Please refresh the page.');
                }

                if (this.waveform) {
                    this.waveform.destroy();
                }

                // Default MediaElement backend: renders instantly from the
                // precomputed peaks and streams audio on demand, instead of
                // WebAudio which downloads + decodes the whole file up front.
                //
                // height:'auto' fills the #waveform container, which MUST
                // have a fixed CSS height (see index.html) — a min-height
                // floor lets the canvas/container feedback loop grow the
                // waveform forever on fractional-DPI Windows displays (#63).
                this.waveform = markRaw(WaveSurfer.create({
                    container: '#waveform',
                    waveColor: '#93c5fd',
                    progressColor: '#1d4ed8',   // darker than waveColor: the played part is visible
                    cursorColor: '#1e40af',
                    height: 'auto',
                    normalize: true,
                    barWidth: 2,
                    barGap: 1
                }));

                // With MediaElement + provided peaks, load() resolves without
                // fetching the audio — a later fetch failure only surfaces as
                // an 'error' event, which must clear the loading spinner.
                this.waveform.on('error', (err) => {
                    console.error('Waveform error:', err);
                    this.waveformLoading = false;
                });

                // Transport state follows the player's own events
                this.waveform.on('play', () => { this.isPlaying = true; });
                this.waveform.on('pause', () => { this.isPlaying = false; this.cancelAudition(); });
                this.waveform.on('finish', () => { this.isPlaying = false; this.cancelAudition(); });
                this.waveform.on('timeupdate', (time) => this.onPlayerTime(time));
                // A click or drag on the waveform itself seeks (and abandons any audition)
                this.waveform.on('interaction', (time) => {
                    this.cancelAudition();
                    this.seekTo(time);
                });
                // The media element resets a currentTime set before it knows its length
                // (WaveSurfer renders from the peaks, so it may not have loaded the audio yet)
                const media = this.waveform.getMediaElement?.();
                if (media) {
                    media.preload = 'metadata';   // stream on demand; 'auto' would buffer the whole recording on every rebuild
                    const mediaListeners = {
                        loadedmetadata: () => this.applyPendingSeek(),
                        seeking: () => { this.playerBuffering = true; },
                        waiting: () => { this.playerBuffering = true; },
                        seeked: () => { this.playerBuffering = false; },
                        playing: () => { this.playerBuffering = false; },
                        pause: () => { this.playerBuffering = false; },
                        ended: () => { this.playerBuffering = false; }
                    };
                    for (const [eventName, listener] of Object.entries(mediaListeners)) media.addEventListener(eventName, listener);
                    this.waveform.on('destroy', () => {
                        for (const [eventName, listener] of Object.entries(mediaListeners)) media.removeEventListener(eventName, listener);
                    });
                }

                this.waveformRegions = markRaw(this.waveform.registerPlugin(WaveSurfer.Regions.create()));

                const peaksResponse = await fetch(`/api/waveform-peaks/${this.currentFileId}`);
                if (!peaksResponse.ok) {
                    // e.g. 409/410: an imported file changed or vanished; say which and what to do
                    const data = await peaksResponse.json().catch(() => ({}));
                    throw new Error(data.detail || 'Failed to load waveform peaks');
                }
                const peaksData = await peaksResponse.json();

                this.waveform.on('ready', () => {
                    console.log('Waveform ready');

                    const duration = this.waveform.getDuration();
                    this.playDuration = duration;
                    const container = document.getElementById('waveform');
                    const containerWidth = container ? container.offsetWidth : 1000;

                    const calculatedZoom = containerWidth / duration;
                    this.currentZoom = Math.max(1, Math.min(calculatedZoom, 200));
                    this.waveform.zoom(this.currentZoom);

                    const openWaveformContextMenu = (e) => {
                        const isSecondaryClick = e.type === 'contextmenu' || e.button === 2 || (e.button === 0 && e.ctrlKey);
                        if (!isSecondaryClick) {
                            return;
                        }

                        e.preventDefault();

                        const wrapper = this.waveform.getWrapper();
                        const rect = wrapper.getBoundingClientRect();
                        const relativeX = Math.max(0, Math.min((e.clientX - rect.left) / rect.width, 1));
                        const time = relativeX * this.waveform.getDuration();

                        this.contextMenu.x = e.clientX;
                        this.contextMenu.y = e.clientY;
                        this.contextMenu.time = time;
                        this.contextMenu.show = true;
                    };

                    // Add right-click handler for manual splits
                    container.addEventListener('contextmenu', openWaveformContextMenu);
                    container.addEventListener('mousedown', openWaveformContextMenu);

                    setTimeout(() => {
                        this.addTrackRegions();
                        this.waveformLoading = false;
                    }, 100);
                });

                // Regions cover the waveform and swallow its clicks: seek from here (plain primary click only)
                this.waveformRegions.on('region-clicked', (region, event) => this.onRegionClicked(region, event));

                this.waveformRegions.on('region-updated', (region) => {
                    this.updateTrackFromRegion(region);
                });

                // Add right-click handler for region actions
                this.waveformRegions.on('region-created', (region) => {
                    const regionElement = region.element;
                    if (regionElement) {
                        const openRegionContextMenu = (e) => {
                            const isSecondaryClick = e.type === 'contextmenu' || e.button === 2 || (e.button === 0 && e.ctrlKey);
                            if (!isSecondaryClick) {
                                return;
                            }

                            if (e.target.classList.contains('wavesurfer-handle')) {
                                return;
                            }

                            e.preventDefault();
                            e.stopPropagation();

                            const trackNumber = parseInt(region.id.replace('track-', ''), 10);
                            if (!isNaN(trackNumber)) {
                                const track = this.detectedTracks.find(t => t.number === trackNumber);
                                const wrapper = this.waveform.getWrapper();
                                const rect = wrapper.getBoundingClientRect();
                                const relativeX = Math.max(0, Math.min((e.clientX - rect.left) / rect.width, 1));
                                const time = relativeX * this.waveform.getDuration();

                                this.regionContextMenu.x = e.clientX;
                                this.regionContextMenu.y = e.clientY;
                                this.regionContextMenu.trackNumber = trackNumber;
                                this.regionContextMenu.isIgnored = track ? track.ignored : false;
                                this.regionContextMenu.time = time;
                                this.regionContextMenu.show = true;
                            }
                        };

                        regionElement.addEventListener('mousedown', (e) => { this._regionDownX = e.clientX; });
                        regionElement.addEventListener('contextmenu', openRegionContextMenu);
                        regionElement.addEventListener('mousedown', openRegionContextMenu);
                    }
                });

                // Pass duration only when valid — WaveSurfer can then render
                // immediately from pre-computed peaks without fetching the audio.
                // If duration is missing or 0 (probe failed at upload), omit it
                // so WaveSurfer falls back to fetching the file (slower but safe).
                const knownDuration = peaksData.duration > 0 ? peaksData.duration : undefined;
                await this.waveform.load(`/api/audio/${this.currentFileId}`, [peaksData.peaks], knownDuration);

            } catch (error) {
                console.error('Waveform initialization failed:', error);
                this.notifyError('Failed to load the waveform', error);
                this.waveformLoading = false;
            }
        },

        /**
         * Add draggable region markers for each track
         */
        addTrackRegions() {
            if (!this.waveformRegions || !this.waveform) return;

            const totalDuration = this.waveform.getDuration();

            try {
                this.waveformRegions.clearRegions();
            } catch (e) {
                console.error('Error clearing regions:', e);
            }

            this.detectedTracks.forEach((track, idx) => {
                try {
                    const clampedStart = Math.min(track.start, totalDuration);
                    const clampedEnd = Math.min(track.end, totalDuration);

                    if (clampedEnd <= clampedStart) return;

                    this.waveformRegions.addRegion({
                        start: clampedStart,
                        end: clampedEnd,
                        color: this.getTrackColor(idx, track.ignored),
                        drag: true,
                        resize: true,
                        id: `track-${track.number}`,
                        content: `Track ${track.number}`
                    });
                } catch (e) {
                    console.error(`Failed to create region for Track ${track.number}:`, e);
                }
            });

            this.addExpectedMarkers(totalDuration);

            // Regions were rebuilt: re-apply the current-track emphasis
            this.playheadIdx = -1;
            if (this.playTime > 0) this.updateCurrentTrack(this.playTime, true);
        },

        /**
         * Thin dashed, non-interactive markers at the Discogs-expected breaks.  Their ids
         * start with "expected-", so nothing treats them as tracks.  A dashed line (not
         * colour alone) tells them from the cuts.
         */
        addExpectedMarkers(totalDuration) {
            if (!this.waveformRegions) return;
            const total = totalDuration || this.waveform?.getDuration?.() || Infinity;
            this.expectedBoundaries.forEach((time, i) => {
                if (!(time > 0 && time < total)) return;
                try {
                    const marker = this.waveformRegions.addRegion({
                        start: time,
                        color: EXPECTED_MARKER_COLOR,
                        drag: false,
                        resize: false,
                        id: `expected-${i + 1}`
                    });
                    if (marker && marker.element && marker.element.style) {
                        marker.element.style.pointerEvents = 'none';
                        marker.element.style.borderLeft = `2px dashed ${EXPECTED_MARKER_COLOR}`;
                        marker.element.style.backgroundColor = 'transparent';
                    }
                } catch (e) {
                    console.error('Failed to create expected-boundary marker:', e);
                }
            });
        },

        /** Drop the ghost markers (data and drawn regions) */
        clearExpectedMarkers() {
            this.expectedBoundaries = [];
            if (!this.waveformRegions) return;
            try {
                this.waveformRegions.getRegions()
                    .filter(region => String(region.id).startsWith('expected-'))
                    .forEach(region => region.remove());
            } catch (e) {
                console.error('Error clearing expected markers:', e);
            }
        },

        /**
         * Split track at context menu position
         */
        splitAtContextMenu() {
            this.contextMenu.show = false;

            const time = this.contextMenu.time;

            // Find which track contains this time position (must be strictly inside)
            const trackIndex = this.detectedTracks.findIndex(t =>
                time > t.start && time < t.end
            );

            if (trackIndex === -1) {
                this.notify('Cannot split here: the position must be inside a track, not on a boundary.', { type: 'warning', timeout: 6000 });
                return;
            }

            const trackToSplit = this.detectedTracks[trackIndex];

            // Create two new tracks from the split
            const newTrack1 = {
                number: trackToSplit.number,
                start: trackToSplit.start,
                end: time,
                duration: time - trackToSplit.start,
                editing: false,
                ignored: false
            };

            const newTrack2 = {
                number: trackToSplit.number + 1,
                start: time,
                end: trackToSplit.end,
                duration: trackToSplit.end - time,
                editing: false,
                ignored: false
            };

            // Replace the split track with two new tracks, then renumber cleanly
            this.detectedTracks.splice(trackIndex, 1, newTrack1, newTrack2);
            this.renumberTracks();
            this.markEdited();

            // Refresh waveform regions
            this.addTrackRegions();

            // Clear any selected release (track count changed)
            this.dropReleaseAfterEdit();
        },

        /**
         * Delete track from region context menu
         */
        deleteTrackFromRegionMenu() {
            this.regionContextMenu.show = false;
            this.deleteTrack(this.regionContextMenu.trackNumber);
        },

        /**
         * Split track at region context menu position
         */
        splitAtRegionContextMenu() {
            this.regionContextMenu.show = false;

            const time = this.regionContextMenu.time;

            // Find which track contains this time position (must be strictly inside)
            const trackIndex = this.detectedTracks.findIndex(t =>
                time > t.start && time < t.end
            );

            if (trackIndex === -1) {
                this.notify('Cannot split here: the position must be inside a track, not on a boundary.', { type: 'warning', timeout: 6000 });
                return;
            }

            const trackToSplit = this.detectedTracks[trackIndex];

            // Create two new tracks from the split
            const newTrack1 = {
                number: trackToSplit.number,
                start: trackToSplit.start,
                end: time,
                duration: time - trackToSplit.start,
                editing: false,
                ignored: false
            };

            const newTrack2 = {
                number: trackToSplit.number + 1,
                start: time,
                end: trackToSplit.end,
                duration: trackToSplit.end - time,
                editing: false,
                ignored: false
            };

            // Replace the split track with two new tracks, then renumber cleanly
            this.detectedTracks.splice(trackIndex, 1, newTrack1, newTrack2);
            this.renumberTracks();
            this.markEdited();

            // Refresh waveform regions
            this.addTrackRegions();

            // Clear any selected release (track count changed)
            this.dropReleaseAfterEdit();
        },

        /**
         * Delete a track and renumber the rest.  Immediate; an Undo toast
         * restores the tracks (and the release selection the delete clears)
         * and redraws the regions.
         */
        deleteTrack(trackNumber) {
            const trackIndex = this.detectedTracks.findIndex(t => t.number === trackNumber);

            if (trackIndex === -1) {
                return;
            }

            const fileId = this.currentFileId;
            const snapshot = this.snapshotEdits();

            // Remove the track, then renumber cleanly
            this.detectedTracks.splice(trackIndex, 1);
            this.renumberTracks();
            this.markEdited();

            // Refresh waveform regions
            this.addTrackRegions();

            // Clear any selected release (track count changed)
            this.dropReleaseAfterEdit();

            this.notifyUndo(`Deleted track ${trackNumber}.`, () => this.restoreEdits(snapshot), { fileId });
        },

        /**
         * Splitting or deleting a track changes the track count, so the selected release is
         * dropped, with everything that belonged to it: its sides, mapping and the ghost
         * markers of its lengths.  (Undo of a delete restores them from its snapshot.)
         */
        dropReleaseAfterEdit() {
            if (!this.selectedRelease) return;
            this.selectedRelease = null;
            this.selectedSides = [];
            this.suggestedSides = [];
            this.sidesSuggestionReason = '';
            this.trackCountMismatch = false;
            this.trackMappingReversed = false;
            this.customMapping = [];
            this.clearExpectedMarkers();
        },

        renumberTracks() {
            this.detectedTracks.forEach((track, i) => { track.number = i + 1; });
        },

        /**
         * Get count of non-ignored tracks
         */
        get activeTrackCount() {
            return this.detectedTracks.filter(t => !t.ignored).length;
        },

        /**
         * Toggle track ignored status and update region color
         */
        toggleTrackIgnored(track) {
            if (this.analyzing) return;   // a running detection would discard it or mistake it for a boundary edit
            track.ignored = !track.ignored;

            // The track under the playhead keeps its emphasis (or loses it when ignored) right away
            // instead of waiting for the playhead to change track
            const idx = this.detectedTracks.findIndex(t => t.number === track.number);
            this.setRegionEmphasis(idx, idx === this.playheadIdx);
        },

        /**
         * Get region color for track by index (grey when ignored).
         * Red is deliberately not in the palette so it never reads as ignored.
         */
        getTrackColor(idx, ignored = false) {
            if (ignored) return 'rgba(107, 114, 128, 0.3)';
            const colors = [
                'rgba(59, 130, 246, 0.3)',
                'rgba(16, 185, 129, 0.3)',
                'rgba(245, 158, 11, 0.3)',
                'rgba(139, 92, 246, 0.3)',
                'rgba(236, 72, 153, 0.3)',
                'rgba(6, 182, 212, 0.3)',
            ];
            return colors[idx % colors.length];
        },

        /**
         * Update track data when region is dragged/resized
         */
        updateTrackFromRegion(region) {
            // Ghost markers (and anything else) are not tracks
            if (!region || typeof region.id !== 'string' || !region.id.startsWith('track-')) return;

            const trackNumber = parseInt(region.id.slice('track-'.length), 10);
            const idx = this.detectedTracks.findIndex(t => t.number === trackNumber);
            if (idx < 0) return;

            // setOptions() (colour, a neighbour pushed along) fires this event too: only a
            // real move of the cut is a hand edit, and applyTrackBounds marks only that.
            this.applyTrackBounds(idx, region.start, region.end);
            // Push any clamping / neighbour changes back onto the regions
            this.syncRegionsAround(idx);
        },

        /** Clock text m:ss for the transport readout */
        formatClock(seconds) {
            const whole = Math.max(0, Math.floor(Number(seconds) || 0));
            return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
        },

        /** Start playback, reporting a failure inline instead of silently */
        startPlayback() {
            const waveform = this.waveform;
            if (!waveform) return;
            try {
                const started = waveform.play();
                if (started && typeof started.then === 'function') {
                    started.then(() => { if (this.waveform === waveform) this.playerNotice = ''; }, (err) => {
                        // A reply for a waveform that was destroyed or replaced meanwhile (file switch,
                        // re-analysis, Snap) says nothing about the current one
                        if (this.waveform !== waveform) return;
                        // Pause (or a teardown) before playback started: the user's own action, not a failure
                        if (err && err.name === 'AbortError') return;
                        console.error('Playback failed:', err);
                        this.isPlaying = false;
                        // A load failure can leave the media element "playing"; stop it so the
                        // next click plays instead of pausing
                        waveform.pause();
                        this.playerNotice = 'Could not start playback: ' + ((err && err.message) || err || 'unknown error');
                    });
                } else {
                    this.playerNotice = '';
                }
            } catch (err) {
                console.error('Playback failed:', err);
                this.playerNotice = 'Could not start playback: ' + ((err && err.message) || err);
            }
        },

        /** The one Play/Pause toggle, bound to the real player state */
        togglePlay() {
            if (!this.waveform) return;
            this.cancelAudition();
            if (this.isPlaying) {
                this.waveform.pause();
                return;
            }
            this.startPlayback();
        },

        pauseWaveform() {
            this.cancelAudition();
            if (this.waveform && typeof this.waveform.pause === 'function') this.waveform.pause();
        },

        seekTo(time) {
            if (!this.waveform || !Number.isFinite(time)) return;
            time = Math.max(0, Math.min(time, this.waveform.getDuration()));
            const media = this.waveform.getMediaElement?.();
            this._pendingSeek = media?.readyState === 0 ? time : null;
            this.waveform.setTime(time);
            this.onPlayerTime(time);
        },

        /** Apply a seek that was made before the media element had its metadata */
        applyPendingSeek() {
            if (this._pendingSeek === null || !this.waveform) return;
            const time = this._pendingSeek;
            this._pendingSeek = null;
            this.waveform.setTime(time);
            this.onPlayerTime(time);
        },

        /**
         * Space toggles playback, except while typing, in a dialog, or on a control Space would
         * itself activate (a focused button: its own click handles Space, so toggling here too
         * would fire twice).  preventDefault keeps the page from scrolling.
         */
        onGlobalKeydown(e) {
            if (e.code !== 'Space' && e.key !== ' ') return;
            if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
            if (this.showSettings || this.setupRequired || !this.waveform) return;
            const target = e.target;
            const tag = target && target.tagName ? String(target.tagName).toUpperCase() : '';
            if (SPACE_OWNING_TAGS.has(tag) || (target && target.isContentEditable)) return;
            e.preventDefault();
            // A held key repeats keydown: keep the page from scrolling, but toggle only once
            if (e.repeat) return;
            this.togglePlay();
        },

        /** The playhead moved */
        onPlayerTime(time) {
            this.playTime = time;
            if (this._auditionStop !== null && time >= this._auditionStop) {
                this._auditionStop = null;
                if (this.waveform) this.waveform.pause();
            }
            this.updateCurrentTrack(time);
        },

        /**
         * Track whose [start, end) contains the playhead (-1 in a gap).  Only touches the
         * regions when the index actually changes (timeupdate fires several times a second).
         */
        updateCurrentTrack(time, force = false) {
            let idx = -1;
            for (let i = 0; i < this.detectedTracks.length; i++) {
                const track = this.detectedTracks[i];
                if (time >= track.start && time < track.end) { idx = i; break; }
            }
            if (idx === this.playheadIdx && !force) return;
            const previousIdx = this.playheadIdx;
            this.playheadIdx = idx;
            this.setRegionEmphasis(previousIdx, false);
            this.setRegionEmphasis(idx, true);
        },

        /** Strengthen the region tint of the track under the playhead (an ignored track stays grey) */
        setRegionEmphasis(idx, on) {
            const track = this.detectedTracks[idx];
            if (!track || !this.waveformRegions) return;
            const region = this.waveformRegions.getRegions().find(r => r.id === `track-${track.number}`);
            if (!region) return;
            let color = this.getTrackColor(idx, track.ignored);
            if (on && !track.ignored) color = color.replace(/0\.3\)$/, '0.55)');
            region.setOptions({ color });
        },

        /** Row toggle: play this track in the waveform (continuing onward) or pause */
        toggleTrackPlay(idx) {
            const track = this.detectedTracks[idx];
            if (!track || !this.waveform) return;
            this.cancelAudition();
            if (this.isPlaying && this.playheadIdx === idx) {
                this.waveform.pause();
                return;
            }
            this.seekTo(track.start);
            this.startPlayback();
        },

        /** Hear a cut point: AUDITION_PADDING_S before to after the track's start or end */
        auditionCut(idx, which) {
            const track = this.detectedTracks[idx];
            if (!track || !this.waveform) return;
            const total = this.waveform.getDuration() || Infinity;
            const cut = which === 'end' ? track.end : track.start;
            this.cancelAudition();
            this.seekTo(Math.max(0, cut - AUDITION_PADDING_S));
            this._auditionStop = Math.min(total, cut + AUDITION_PADDING_S);
            this.startPlayback();
        },

        cancelAudition() {
            this._auditionStop = null;
        },

        /** Region click: seek to the click position (primary, undragged clicks only) */
        onRegionClicked(region, e) {
            if (!this.waveform || !e) return;
            if (e.button !== undefined && e.button !== 0) return;
            if (e.ctrlKey || e.metaKey) return;
            if (String(region.id).startsWith('expected-')) return;
            if (e.target && e.target.closest?.('[part~=region-handle]')) return;
            const downX = this._regionDownX;
            this._regionDownX = null;
            if (downX !== null && Math.abs(e.clientX - downX) > REGION_CLICK_SLOP_PX) return;   // the end of a drag
            const rect = this.waveform.getWrapper().getBoundingClientRect();
            const relativeX = Math.max(0, Math.min((e.clientX - rect.left) / rect.width, 1));
            this.cancelAudition();
            this.seekTo(relativeX * this.waveform.getDuration());
        },

        zoomIn() {
            if (this.waveform) {
                this.currentZoom = Math.min(this.currentZoom * 1.5, 500);
                this.waveform.zoom(this.currentZoom);
            }
        },

        zoomOut() {
            if (this.waveform) {
                this.currentZoom = Math.max(this.currentZoom / 1.5, 1);
                this.waveform.zoom(this.currentZoom);
            }
        },

        destroyWaveform() {
            if (this.waveform) {
                this.waveform.destroy();
                this.waveform = null;
                this.waveformRegions = null;
            }
            this.isPlaying = false;
            this.playTime = 0;
            this.playDuration = 0;
            this.playheadIdx = -1;
            this.playerNotice = '';
            this.playerBuffering = false;
            this._pendingSeek = null;
            this._auditionStop = null;
        },

        /**
         * Process file with selected release
         */
        async processFile() {
            if (!this.currentFileId || !this.selectedRelease) return;

            const activeTracks = this.detectedTracks.filter(t => !t.ignored);

            if (activeTracks.length === 0) {
                this.notify('No tracks selected. Un-ignore at least one track to export.', { type: 'warning', timeout: 6000 });
                return;
            }

            const trackMapping = activeTracks.map((track, idx) => {
                const originalIdx = this.detectedTracks.indexOf(track);
                const discogsIdx = this.customMapping[originalIdx];
                const discogsTrack = this.selectedRelease.tracks[discogsIdx];
                return {
                    detected: track.number,
                    discogs: discogsTrack?.position || 'Unknown'
                };
            });

            const trackBoundaries = activeTracks.map(track => ({
                number: track.number,
                start: track.start,
                end: track.end,
                duration: track.duration
            }));

            // Marked before the request: removeFile checks the file's own status, and another
            // file may be opened while the server is still answering.
            const file = this.uploadedFiles.find(f => f.id === this.currentFileId);
            const previousStatus = file?.status;
            try {
                this.isProcessing = true;
                this.processingProgress = 0.1;
                this.processingMessage = 'Starting...';
                this.lastProgressAt = Date.now();
                if (file) file.status = 'processing';

                const response = await fetch('/api/process', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        file_id: this.currentFileId,
                        release_id: this.selectedRelease.id,
                        track_mapping: trackMapping,
                        reversed: this.trackMappingReversed,
                        track_boundaries: trackBoundaries,
                        output_format: this.outputFormat,
                        restoration_level: this.restorationLevel
                        // album_description is left out: the server uses the saved setting, so a box
                        // ticked in Settings and then cancelled never turns the lookups on
                    })
                });

                const data = await response.json();
                if (!response.ok || !data.job_id) {
                    throw new Error(data.detail || 'Failed to start processing');
                }
                console.log('Processing started:', data);
                this.currentJobId = data.job_id;
                this.startProcessPolling(data.job_id);
            } catch (error) {
                console.error('Processing failed:', error);
                if (file && file.status === 'processing') file.status = previousStatus;
                this.stopProcessPolling();
                this.isProcessing = false;
                this.processingProgress = 0;
                this.processingMessage = '';
                this.notifyError('Processing failed', error);
            }
        },

        startProcessPolling(jobId) {
            this.stopProcessPolling();

            this.processPollTimer = setInterval(async () => {
                if (!this.isProcessing || !jobId) {
                    this.stopProcessPolling();
                    return;
                }

                try {
                    const response = await fetch(`/api/process/${jobId}`);
                    if (!response.ok) {
                        return;
                    }

                    const job = await response.json();

                    if (job.status === 'processing') {
                        this.processingProgress = Math.max(this.processingProgress, job.progress || 0.1);
                        this.processingMessage = job.message || 'Processing...';
                        return;
                    }

                    if (job.status === 'complete') {
                        this.stopProcessPolling();
                        this.processingProgress = 1.0;
                        this.processingMessage = 'Complete!';
                        this.isProcessing = false;

                        const tracks = Array.isArray(job.tracks) ? job.tracks : [];
                        if (tracks.length > 0) {
                            this.successMessage = `✅ ${tracks.length} tracks saved to your VinylFlow/output folder\n\nTracks: ${tracks.join(', ')}`;
                        } else {
                            this.successMessage = '✅ Processing complete. Files saved to your VinylFlow/output folder.';
                        }

                        const file = this.uploadedFiles.find(f => f.id === job.file_id);
                        if (file) {
                            file.status = 'completed';
                            if (file.in_place) this.loadImportList();
                        }
                        return;
                    }

                    if (job.status === 'error') {
                        this.stopProcessPolling();
                        this.isProcessing = false;
                        this.processingMessage = `Error: ${job.error || 'Unknown error'}`;

                        const file = this.uploadedFiles.find(f => f.id === job.file_id);
                        if (file) {
                            file.status = 'error';
                        }

                        this.notify(`Processing error: ${job.error || 'Unknown error'}`, { type: 'error' });
                    }
                } catch (error) {
                    console.warn('Process polling failed:', error);
                }
            }, 1500);
        },

        stopProcessPolling() {
            if (this.processPollTimer) {
                clearInterval(this.processPollTimer);
                this.processPollTimer = null;
            }
            this.currentJobId = null;
        },

        /**
         * Reset UI for next file
         */
        resetForNextFile() {
            this.stopProcessPolling();
            this.successMessage = '';
            this.isProcessing = false;
            this.clearEditState();
            this.resetSearchResults();
            this.searchLoading = false;
            this.searchSeq++;
            this.searchQuery = '';
            this.processingProgress = 0;
            this.processingMessage = '';

            const nextFile = this.uploadedFiles.find(f => f.status === 'uploaded');
            if (nextFile) {
                this.selectFile(nextFile.id);
            } else {
                this.currentFileId = null;
                this.currentFile = null;
            }
        },

        /**
         * Clean filename for Discogs search
         */
        cleanFilename(filename) {
            // Remove all supported extensions
            let name = filename.replace(/\.(wav|aiff|aif)$/i, '');
            name = name.replace(/[-_]+/g, ' ');
            name = name.replace(/\s+/g, ' ').trim();
            return name;
        },

        formatSize(bytes) {
            if (!bytes) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
        },

        formatDuration(seconds) {
            if (!seconds) return '0:00';
            const mins = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return `${mins}:${secs.toString().padStart(2, '0')}`;
        },

        formatTime(seconds) {
            return this.formatDuration(seconds);
        },

        statusClass(status) {
            switch (status) {
                case 'uploaded': return 'text-blue-600 font-medium';
                case 'analyzed': return 'text-purple-600 font-medium';
                case 'processing': return 'text-yellow-600 font-medium';
                case 'completed': return 'text-green-600 font-medium';
                case 'error': return 'text-red-600 font-medium';
                default: return 'text-gray-600';
            }
        }
    };
}
