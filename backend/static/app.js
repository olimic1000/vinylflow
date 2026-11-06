/**
 * VinylFlow Frontend Application
 * Alpine.js application for managing vinyl digitization workflow
 */

// Toast stack cap: past it the oldest toast without an action makes room (an Undo is never evicted)
const MAX_TOASTS = 5;
// How long an Undo toast stays (ms)
const UNDO_TOAST_MS = 8000;

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

        // Import from folder (files read in place, no browser upload)
        importTab: 'upload',            // 'folder' | 'upload'
        importInfo: { enabled: false, dir: '', error: null, files: [] },
        importSelected: [],             // names ticked in the folder list
        importBusy: false,              // POST /api/import in flight
        importLoading: false,           // GET /api/import in flight
        desktopPicker: false,           // the pywebview bridge offers a native file dialog

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
                (data.files || []).forEach(file => {
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
                if (!this.currentFile && data.files?.length > 0) {
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
                                    status: 'uploaded'
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
        },

        /**
         * Select a file from the queue
         */
        selectFile(fileId) {
            this.currentFileId = fileId;
            this.currentFile = this.uploadedFiles.find(f => f.id === fileId);
            this.detectedTracks = [];
            this.searchResults = [];
            this.selectedRelease = null;
            this.trackCountMismatch = false;
            this.searchNotice = '';
            this.successMessage = '';
            this.processingProgress = 0;
            this.processingMessage = '';

            this.destroyWaveform();

            if (this.currentFile) {
                this.searchQuery = this.cleanFilename(this.currentFile.filename);
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
                this.searchResults = [];
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

                await this.initWaveform();

                if (this.searchQuery) {
                    await this.searchDiscogs();
                }
            } catch (error) {
                console.error('Analysis failed:', error);
                this.notifyError('Analysis failed', error);
                this.processingMessage = '';
            } finally {
                this.analyzing = false;
            }
        },

        /**
         * Re-analyze file with current settings.  Runs immediately; an Undo
         * toast restores the previous boundaries, release and search results.
         */
        async reanalyzeFile() {
            if (!this.currentFileId || this.analyzing) return;

            const fileId = this.currentFileId;
            const snapshot = this.snapshotEdits();
            const search = this.snapshotSearch();   // the re-analysis searches again; only its Undo puts these back

            this.clearEditState();
            this.searchResults = [];
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
                await this.restoreSnapshotWithWaveform(snapshot, search);
                return;
            }
            if (this.detectedTracks.length > 0) {
                this.notifyUndo('Re-analyzed. Boundaries and release were reset.',
                    () => this.restoreSnapshotWithWaveform(snapshot, search), { fileId });
            }
        },

        /**
         * Undo of a flow that replaced the whole analysis (re-analyze,
         * duration-based splitting); its Undo toast checks undoBlockedReason first.
         * `search` (a snapshotSearch() copy) is put back too when the flow cleared it.
         */
        async restoreSnapshotWithWaveform(snapshot, search = null) {
            this.currentPlayingTrack = null;
            this.destroyWaveform();
            this.restoreEdits(snapshot);   // regions are redrawn by initWaveform below
            if (search) this.restoreSearch(search);
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
                    this.trackMappingReversed = false;
                    this.customMapping = Array.from({ length: this.detectedTracks.length }, (_, i) => i);
                    this.trackCountMismatch =
                        this.detectedTracks.length !== this.selectedRelease.tracks.length;
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
         * Search Discogs for releases
         */
        async searchDiscogs() {
            if (this.searchLoading) return;
            if (!this.searchQuery.trim()) {
                this.searchNotice = 'Please enter a search query';
                return;
            }

            this.searchLoading = true;
            this.searchResults = [];
            this.searchNotice = '';

            try {
                const response = await fetch('/api/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        query: this.searchQuery,
                        max_results: 12
                    })
                });
                const data = await response.json();

                // Surface the server's error (missing token, network down)
                // instead of corrupting searchResults with undefined.
                if (!response.ok || !Array.isArray(data.results)) {
                    throw new Error(data.detail || 'Search failed');
                }

                this.searchResults = data.results;

                if (this.searchResults.length === 0) {
                    this.searchNotice = 'No results found. Try a different search query.';
                }
            } catch (error) {
                console.error('Search failed:', error);
                this.searchResults = [];
                this.searchNotice = 'Search failed: ' + error.message;
            } finally {
                this.searchLoading = false;
            }
        },

        /**
         * Select a release from search results
         */
        selectRelease(release) {
            this.selectedRelease = release;
            this.trackMappingReversed = false;
            this.customMapping = Array.from({ length: this.detectedTracks.length }, (_, i) => i);

            const activeTrackCount = this.detectedTracks.filter(t => !t.ignored).length;
            const discogsTrackCount = release.tracks.length;

            // Check for track count mismatch
            if (activeTrackCount !== discogsTrackCount) {
                this.trackCountMismatch = true;
            } else {
                this.trackCountMismatch = false;
            }
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

                // Reinitialize waveform
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
            this.detectedTracks = [];
            this.searchResults = [];
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
