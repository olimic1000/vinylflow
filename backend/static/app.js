/**
 * VinylFlow Frontend Application
 * Alpine.js application for managing vinyl digitization workflow
 */

// Toast stack cap: past it the oldest toast without an action makes room (an Undo is never evicted)
const MAX_TOASTS = 5;
// How long an Undo toast stays (ms)
const UNDO_TOAST_MS = 8000;
// Discogs results per search (one page; each card's tracklist loads separately)
const SEARCH_PAGE_SIZE = 12;
// Tracklists fetched at once while the result cards fill in (the server rate-limits Discogs anyway)
const RELEASE_DETAIL_CONCURRENCY = 2;
// A side set "fits" the recording when its total length is within this fraction of the music length
const SIDE_FIT_TOLERANCE = 0.15;
// Candidates this close (as a fraction) to the best are ties; the full album / a whole disc wins them
const SIDE_FIT_TIE = 0.01;

/** A search result card; tracks: null = tracklist not loaded yet (filled in progressively). */
function newSearchCard(result) {
    return { ...result, tracks: result.tracks ?? null, detailStatus: 'pending', opening: false };
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
        trackCountMismatch: false,
        autoRetryAttempts: 0,
        maxAutoRetries: 3,

        // Files and Queue
        uploadedFiles: [],
        currentFileId: null,
        currentFile: null,

        // Analysis
        detectedTracks: [],
        currentPlayingTrack: null,

        // Waveform
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
        onlyWithLengths: false,   // Result filter: hide releases without track lengths
        detailsSettled: false,    // True once every result's tracklist load finished or failed
        searchSeq: 0,             // Bumped by every search and file switch: stale replies are dropped
        _detailInflight: {},      // `${searchSeq}:${releaseId}` -> promise of a tracklist load in progress
        selectedRelease: null,
        trackMappingReversed: false,
        customMapping: [],

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
            output_dir: ''
        },

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

            // Files removed from the queue are deleted server-side only after
            // an undo window; flush them if the page goes away first.
            window.addEventListener('pagehide', () => {
                this.commitAllRemovals({ keepalive: true });
            });

            await this.loadConfig();
            await this.loadFormats();
            await this.checkStatus();
            this.connectWebSocket();

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
                        }
                    }
                    break;

                case 'error':
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
                this.notify('Settings saved.', { type: 'success' });
            } catch (error) {
                console.error('Failed to save config:', error);
                this.notify('Failed to save settings.', { type: 'error' });
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
                customMapping: [...this.customMapping]
            };
        },

        /** Put a snapshotEdits() copy back and redraw the waveform regions */
        restoreEdits(snapshot) {
            this.detectedTracks = snapshot.detectedTracks.map(track => ({ ...track }));
            this.selectedRelease = snapshot.selectedRelease;
            this.trackCountMismatch = snapshot.trackCountMismatch;
            this.trackMappingReversed = snapshot.trackMappingReversed;
            this.customMapping = [...snapshot.customMapping];
            this.addTrackRegions();
        },

        /** Reset the fields snapshotEdits() covers (no tracks, no release, no mapping) */
        clearEditState() {
            this.detectedTracks = [];
            this.selectedRelease = null;
            this.trackCountMismatch = false;
            this.trackMappingReversed = false;
            this.customMapping = [];
        },

        /** Search results and query, for the flows that clear them (re-analyze, removing the open file) */
        snapshotSearch() {
            return { results: this.searchResults, query: this.searchQuery };
        },

        restoreSearch(saved) {
            this.searchResults = saved.results;
            this.searchQuery = saved.query;
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
            this.currentFileId = fileId;
            this.currentFile = this.uploadedFiles.find(f => f.id === fileId);
            if (this.currentFile && !(this.currentFile.musicDuration > 0)) {
                this.fetchScan(fileId);
            }
            this.detectedTracks = [];
            this.resetSearchResults();
            this.selectedRelease = null;
            this.trackCountMismatch = false;
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
            }

            entry.toastId = this.notifyUndo(`Removed "${file.filename}" from the queue.`, () => this.undoRemoveFile(fileId), {
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
            if (!this.currentFileId || this.analyzing) return;

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

                this.detectedTracks = data.tracks.map(track => ({
                    ...track,
                    editing: false,
                    ignored: false
                }));
                this.processingMessage = '';

                const file = this.uploadedFiles.find(f => f.id === this.currentFileId);
                if (file) {
                    file.status = 'analyzed';
                }

                // A release may have been picked before detection ran (Discogs first):
                // its mapping and track-count check need the tracks that just arrived.
                // The release itself and the search results are left alone.
                if (this.selectedRelease) {
                    this.resetMappingToDetected();
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
                // duration-based options) instead of an empty mapping.
                if (this.selectedRelease) {
                    this.resetMappingToDetected();
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
            if (!this.currentFileId || this.analyzing) return;

            const fileId = this.currentFileId;
            const snapshot = this.snapshotEdits();

            // The Discogs search is independent of detection: its results stay.
            this.clearEditState();
            this.currentPlayingTrack = null;

            if (this.$refs.audioPlayer) {
                this.$refs.audioPlayer.pause();
                this.$refs.audioPlayer.currentTime = 0;
            }

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
         * Undo of a flow that replaced the whole analysis (re-analyze,
         * duration-based splitting); its Undo toast checks undoBlockedReason first.
         */
        async restoreSnapshotWithWaveform(snapshot) {
            this.currentPlayingTrack = null;
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
            if (!this.currentFileId || this.analyzing) return;
            if (!target || target < 1 || target > 99) {
                this.rescanNotice = 'Enter how many songs the recording contains (1–99) first.';
                return;
            }

            this.analyzing = true;
            this.rescanNotice = '';
            this.processingMessage = `Rescanning for ${target} tracks...`;
            this.currentPlayingTrack = null;

            if (this.$refs.audioPlayer) {
                this.$refs.audioPlayer.pause();
                this.$refs.audioPlayer.currentTime = 0;
            }

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

                this.detectedTracks = data.tracks.map(track => ({
                    ...track,
                    editing: false,
                    ignored: false
                }));
                this.processingMessage = '';

                const file = this.uploadedFiles.find(f => f.id === this.currentFileId);
                if (file) {
                    file.status = 'analyzed';
                }

                // Boundary edits from the old analysis no longer apply.
                if (this.selectedRelease) {
                    this.resetMappingToDetected();
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

            try {
                const response = await fetch('/api/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        query: this.searchQuery,
                        max_results: SEARCH_PAGE_SIZE
                    })
                });
                const data = await response.json();
                if (seq !== this.searchSeq) return;   // a file switch superseded this search

                // Surface the server's error (missing token, network down)
                // instead of corrupting searchResults with undefined.
                if (!response.ok || !Array.isArray(data.results)) {
                    throw new Error(data.detail || 'Search failed');
                }

                this.searchResults = data.results.map(newSearchCard);

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

        /** Forget the shown results (not the query box) */
        resetSearchResults() {
            this.searchResults = [];
            this.detailsSettled = false;
        },

        /**
         * Fetch full tracklists for the given results (default: all shown) in display
         * order, RELEASE_DETAIL_CONCURRENCY at a time.  Stops as soon as searchSeq moves
         * on (new search or file switch).
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
         * with its tracks.
         */
        async chooseResult(result) {
            if (result.opening) return;
            if (result.tracks) {
                this.selectRelease(result);
                return;
            }
            const seq = this.searchSeq;
            result.opening = true;
            try {
                const ok = await this.loadReleaseDetails(result.id, seq);
                if (ok && seq === this.searchSeq) {
                    const fresh = this.searchResults.find(r => r.id === result.id);
                    if (fresh) this.selectRelease(fresh);
                }
            } finally {
                result.opening = false;
            }
        },

        /**
         * Select a release from search results.  Only a release whose tracklist has
         * loaded can be selected (everything downstream reads release.tracks).
         * Detection runs after the pick when there are no tracks yet; analyzeFile
         * builds the mapping once they arrive.
         */
        selectRelease(release) {
            if (!Array.isArray(release?.tracks)) return;
            this.selectedRelease = release;
            this.resetMappingToDetected();
            if (this.detectedTracks.length === 0) {
                this.analyzeFile();
            }
        },

        /**
         * Map detected track i to release track i, un-reverse, and recompute the
         * track-count warning.  Run whenever the release or the detected tracks change.
         */
        resetMappingToDetected() {
            this.trackMappingReversed = false;
            this.customMapping = Array.from({ length: this.detectedTracks.length }, (_, i) => i);
            const activeTrackCount = this.detectedTracks.filter(t => !t.ignored).length;
            this.trackCountMismatch = !!this.selectedRelease && activeTrackCount !== this.selectedRelease.tracks.length;
        },

        /**
         * The count warning, except while the first detection runs: a pick starts it, and
         * "0 detected" is no finding yet (its duration-splitting button would race it too).
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
            this.selectedRelease = null;
            this.trackMappingReversed = false;
            this.trackCountMismatch = false;
            this.customMapping = [];
        },

        /** Back to the search card (tracks exist, no release chosen); searches only if it has no results */
        tryDiscogsAgain() {
            if (this.analyzing) return;
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
         * Offer duration-based splitting when silence detection fails
         */
        async offerDurationBasedSplitting() {
            // Check if Discogs has duration information
            const hasDiscogsDurations = this.selectedRelease.tracks.every(t => t.duration);

            if (!hasDiscogsDurations) {
                this.notify("Duration-based splitting isn't available: Discogs has no track lengths for this release. Adjust the boundaries by hand or pick another release.", { type: 'warning' });
                return;
            }

            // The button is the user's explicit choice, so no confirmation:
            // the previous boundaries come back with Undo (see below).
            await this.analyzeFileDurationBased();
        },

        /**
         * Analyze file using duration-based splitting from Discogs track lengths
         */
        async analyzeFileDurationBased() {
            if (!this.currentFileId || !this.selectedRelease) return;

            this.processingMessage = 'Creating duration-based splits...';
            const fileId = this.currentFileId;
            const snapshot = this.snapshotEdits();

            try {
                // Extract durations from Discogs tracks
                const durations = this.selectedRelease.tracks.map(t => {
                    // Parse duration string: "5:24" -> 324 seconds
                    if (!t.duration) return 0;

                    const parts = t.duration.split(':');
                    if (parts.length === 2) {
                        return parseInt(parts[0]) * 60 + parseInt(parts[1]);
                    } else if (parts.length === 3) {
                        return parseInt(parts[0]) * 3600 + parseInt(parts[1]) * 60 + parseInt(parts[2]);
                    }
                    return 0;
                }).filter(d => d > 0);

                if (durations.length !== this.selectedRelease.tracks.length) {
                    throw new Error('Could not parse all track durations from Discogs');
                }

                // Call backend API
                const response = await fetch('/api/analyze-duration-based', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        file_id: this.currentFileId,
                        discogs_durations: durations
                    })
                });

                const data = await response.json();

                if (!response.ok) {
                    throw new Error(data.detail || 'Duration-based analysis failed');
                }

                // Update detected tracks (same format as regular analysis)
                this.detectedTracks = data.tracks.map(track => ({
                    ...track,
                    editing: false,
                    ignored: false,
                    durationBased: true  // Flag for UI indication
                }));

                this.processingMessage = '';

                // Update file status
                const file = this.uploadedFiles.find(f => f.id === this.currentFileId);
                if (file) {
                    file.status = 'analyzed (duration-based)';
                }

                // The mapping and count check follow the new tracks. Reachable with no tracks at all
                // (a pick whose detection found none), when every one would export as "Unknown".
                this.resetMappingToDetected();

                // initWaveform keeps an existing waveform, and with it the old regions
                this.destroyWaveform();
                await this.initWaveform();

                this.notifyUndo(`Created ${this.detectedTracks.length} tracks from the Discogs lengths. Review the boundaries before exporting.`,
                    () => this.restoreSnapshotWithWaveform(snapshot), { fileId, type: 'success' });

            } catch (error) {
                console.error('Duration-based analysis failed:', error);
                this.notifyError('Duration-based analysis failed', error);
                this.processingMessage = '';
            }
        },

        /**
         * Reverse track mapping order
         */
        reverseMapping() {
            this.trackMappingReversed = !this.trackMappingReversed;

            if (this.trackMappingReversed) {
                const numTracks = this.detectedTracks.length;
                this.customMapping = Array.from({ length: numTracks }, (_, i) => numTracks - 1 - i);
            } else {
                this.customMapping = Array.from({ length: this.detectedTracks.length }, (_, i) => i);
            }
        },

        /**
         * Update custom mapping when user changes dropdown
         */
        updateCustomMapping() {
            this.trackMappingReversed = false;
        },

        /**
         * Play preview of detected track (30 seconds)
         */
        playPreview(trackNumber) {
            if (!this.currentFileId) return;

            const audioPlayer = this.$refs.audioPlayer;
            const track = this.detectedTracks.find(t => t.number === trackNumber);

            let previewUrl = `/api/preview/${this.currentFileId}/${trackNumber}`;
            // Always send the range: after a split/merge/delete the backend's
            // stored boundaries are stale, so the track number alone is wrong.
            if (track) {
                previewUrl += `?start=${track.start}&end=${track.end}`;
            }

            audioPlayer.src = previewUrl;
            audioPlayer.load();
            audioPlayer.play().catch(err => {
                // AbortError: a newer Play/Stop click interrupted this one
                // before it started. Not a real failure.
                if (err.name === 'AbortError') return;
                console.error('Playback failed:', err);
                this.notify('Failed to play the preview.', { type: 'error' });
            });

            this.currentPlayingTrack = trackNumber;
        },

        /**
         * Stop audio preview
         */
        stopPreview() {
            const audioPlayer = this.$refs.audioPlayer;
            audioPlayer.pause();
            audioPlayer.currentTime = 0;
            this.currentPlayingTrack = null;
        },

        /**
         * Update track start time
         */
        updateTrackStart(idx, value) {
            const newStart = parseFloat(value);
            if (isNaN(newStart) || newStart < 0) return;

            this.detectedTracks[idx].start = newStart;
            const end = this.detectedTracks[idx].end;
            this.detectedTracks[idx].duration = end - newStart;
        },

        /**
         * Update track end time
         */
        updateTrackEnd(idx, value) {
            const newEnd = parseFloat(value);
            if (isNaN(newEnd) || newEnd < 0) return;

            this.detectedTracks[idx].end = newEnd;
            const start = this.detectedTracks[idx].start;
            this.detectedTracks[idx].duration = newEnd - start;

            if (idx + 1 < this.detectedTracks.length) {
                this.detectedTracks[idx + 1].start = newEnd;
            }
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
                this.waveform = WaveSurfer.create({
                    container: '#waveform',
                    waveColor: '#93c5fd',
                    progressColor: '#93c5fd',
                    cursorColor: '#1e40af',
                    height: 'auto',
                    normalize: true,
                    barWidth: 2,
                    barGap: 1
                });

                // With MediaElement + provided peaks, load() resolves without
                // fetching the audio — a later fetch failure only surfaces as
                // an 'error' event, which must clear the loading spinner.
                this.waveform.on('error', (err) => {
                    console.error('Waveform error:', err);
                    this.waveformLoading = false;
                });

                this.waveformRegions = this.waveform.registerPlugin(WaveSurfer.Regions.create());

                const peaksResponse = await fetch(`/api/waveform-peaks/${this.currentFileId}`);
                if (!peaksResponse.ok) {
                    throw new Error('Failed to load waveform peaks');
                }
                const peaksData = await peaksResponse.json();

                this.waveform.on('ready', () => {
                    console.log('Waveform ready');

                    const duration = this.waveform.getDuration();
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

            // Refresh waveform regions
            this.addTrackRegions();

            // Clear any selected release (track count changed)
            if (this.selectedRelease) {
                this.selectedRelease = null;
                this.trackCountMismatch = false;
            }
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

            // Refresh waveform regions
            this.addTrackRegions();

            // Clear any selected release (track count changed)
            if (this.selectedRelease) {
                this.selectedRelease = null;
                this.trackCountMismatch = false;
            }
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

            // Refresh waveform regions
            this.addTrackRegions();

            // Clear any selected release (track count changed)
            if (this.selectedRelease) {
                this.selectedRelease = null;
                this.trackCountMismatch = false;
            }

            this.notifyUndo(`Deleted track ${trackNumber}.`, () => this.restoreEdits(snapshot), { fileId });
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
            track.ignored = !track.ignored;

            if (this.waveformRegions) {
                const region = this.waveformRegions.getRegions().find(r => r.id === `track-${track.number}`);
                if (region) {
                    region.setOptions({ color: this.getTrackColor(track.number - 1, track.ignored) });
                }
            }
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
            const trackNumber = parseInt(region.id.replace('track-', ''));
            const track = this.detectedTracks.find(t => t.number === trackNumber);

            if (track) {
                track.start = region.start;
                track.end = region.end;
                track.duration = region.end - region.start;
                track.editing = true;

                const idx = this.detectedTracks.findIndex(t => t.number === trackNumber);
                if (idx >= 0 && idx + 1 < this.detectedTracks.length) {
                    this.detectedTracks[idx + 1].start = region.end;

                    const nextRegion = this.waveformRegions.getRegions().find(r => r.id === `track-${trackNumber + 1}`);
                    if (nextRegion) {
                        nextRegion.setOptions({ start: region.end });
                    }
                }
            }
        },

        playWaveform() {
            if (this.waveform) this.waveform.play();
        },

        stopWaveform() {
            if (this.waveform) this.waveform.pause();
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
            this.detectedTracks = [];
            this.resetSearchResults();
            this.searchLoading = false;
            this.searchSeq++;
            this.selectedRelease = null;
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
