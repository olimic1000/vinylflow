/**
 * VinylFlow Frontend Application
 * Alpine.js application for managing vinyl digitization workflow
 */

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
        selectedRelease: null,
        trackMappingReversed: false,
        customMapping: [],

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

        // Config
        config: {
            silence_threshold: -40,
            min_silence_duration: 1.5,
            track_numbering: 'vinyl',
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
                        alert(`Processing error: ${data.message}`);

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

                alert(`✅ Successfully connected as ${data.username}!`);

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
                alert('Settings saved!');
            } catch (error) {
                console.error('Failed to save config:', error);
                alert('Failed to save settings');
            }
        },

        /**
         * Open desktop folder picker for output directory
         */
        async chooseOutputFolder() {
            try {
                if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.select_output_folder) {
                    alert('Folder picker is available in the desktop app only.');
                    return;
                }

                const selected = await window.pywebview.api.select_output_folder(this.config.output_dir || '');
                if (selected) {
                    this.config.output_dir = selected;
                }
            } catch (error) {
                console.error('Failed to choose output folder:', error);
                alert('Failed to open folder picker');
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
                            alert('Upload failed: Invalid response');
                            this.uploadProgress = 0;
                            reject(error);
                        }
                    } else {
                        console.error('Upload failed:', xhr.statusText);
                        alert('Upload failed');
                        this.uploadProgress = 0;
                        reject(new Error(xhr.statusText));
                    }
                });

                // Handle errors
                xhr.addEventListener('error', () => {
                    console.error('Upload failed');
                    alert('Upload failed');
                    this.uploadProgress = 0;
                    this.uploadFinalizing = false;
                    reject(new Error('Upload failed'));
                });

                // Send the request
                xhr.open('POST', '/api/upload');
                xhr.send(formData);
            });
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
         * Remove file from queue
         */
        async removeFile(fileId) {
            if (!confirm('Remove this file from the queue?')) return;

            try {
                await fetch(`/api/queue/${fileId}`, { method: 'DELETE' });
                this.uploadedFiles = this.uploadedFiles.filter(f => f.id !== fileId);

                if (this.currentFileId === fileId) {
                    this.currentFileId = null;
                    this.currentFile = null;
                    this.detectedTracks = [];
                }
            } catch (error) {
                console.error('Failed to remove file:', error);
            }
        },

        /**
         * Analyze file for silence detection
         */
        async analyzeFile() {
            if (!this.currentFileId || this.analyzing) return;

            this.analyzing = true;
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
                alert('Analysis failed: ' + error.message);
                this.processingMessage = '';
            } finally {
                this.analyzing = false;
            }
        },

        /**
         * Re-analyze file with current settings
         */
        async reanalyzeFile() {
            if (!this.currentFileId) return;

            if (!confirm('Re-analyze this file? Current track boundaries and search results will be reset.')) {
                return;
            }

            this.detectedTracks = [];
            this.selectedRelease = null;
            this.searchResults = [];
            this.currentPlayingTrack = null;
            this.trackMappingReversed = false;
            this.customMapping = [];

            if (this.$refs.audioPlayer) {
                this.$refs.audioPlayer.pause();
                this.$refs.audioPlayer.currentTime = 0;
            }

            this.destroyWaveform();
            await this.analyzeFile();
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
            const activeTrackCount = this.detectedTracks.filter(t => !t.ignored).length;
            const discogsTrackCount = this.selectedRelease.tracks.length;

            // Check if Discogs has duration information
            const hasDiscogsDurations = this.selectedRelease.tracks.every(t => t.duration);

            if (!hasDiscogsDurations) {
                alert(`❌ Duration-Based Splitting Unavailable\n\nDiscogs doesn't have duration information for this release.\n\nSuggestions:\n• Manually adjust track boundaries in the waveform\n• Search for a different release with duration data`);
                return;
            }

            const message = `🎯 Try Duration-Based Splitting?\n\nSilence detection found ${activeTrackCount} tracks, but this release has ${discogsTrackCount} tracks.\n\nDuration-based splitting uses Discogs track lengths to divide the album instead of detecting silence.\n\nContinue?`;

            if (confirm(message)) {
                await this.analyzeFileDurationBased();
            }
        },

        /**
         * Analyze file using duration-based splitting from Discogs track lengths
         */
        async analyzeFileDurationBased() {
            if (!this.currentFileId || !this.selectedRelease) return;

            this.processingMessage = 'Creating duration-based splits...';

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

                alert(`✅ Duration-Based Splitting Complete\n\nCreated ${this.detectedTracks.length} tracks based on Discogs durations.\n\nReview boundaries in the waveform and adjust if needed before processing.`);

            } catch (error) {
                console.error('Duration-based analysis failed:', error);
                alert('❌ Duration-based analysis failed: ' + error.message);
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
            if (track && track.editing) {
                previewUrl += `?start=${track.start}&end=${track.end}`;
            }

            audioPlayer.src = previewUrl;
            audioPlayer.load();
            audioPlayer.play().catch(err => {
                console.error('Playback failed:', err);
                alert('Failed to play preview');
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
                alert('Failed to load waveform: ' + error.message);
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

            const colors = [
                'rgba(59, 130, 246, 0.3)',
                'rgba(16, 185, 129, 0.3)',
                'rgba(245, 158, 11, 0.3)',
                'rgba(139, 92, 246, 0.3)',
                'rgba(236, 72, 153, 0.3)',
                'rgba(239, 68, 68, 0.3)',
            ];

            this.detectedTracks.forEach((track, idx) => {
                try {
                    const clampedStart = Math.min(track.start, totalDuration);
                    const clampedEnd = Math.min(track.end, totalDuration);

                    if (clampedEnd <= clampedStart) return;

                    this.waveformRegions.addRegion({
                        start: clampedStart,
                        end: clampedEnd,
                        color: colors[idx % colors.length],
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
                alert('Cannot split here - position must be inside a track (not at boundaries)');
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
            
            const trackNumber = this.regionContextMenu.trackNumber;
            const trackIndex = this.detectedTracks.findIndex(t => t.number === trackNumber);

            if (trackIndex === -1) {
                return;
            }

            if (!confirm(`Delete Track ${trackNumber}? This cannot be undone.`)) {
                return;
            }

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
                alert('Cannot split here - position must be inside a track (not at boundaries)');
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
         * Delete a track and renumber remaining tracks
         */
        deleteTrack(trackNumber) {
            const trackIndex = this.detectedTracks.findIndex(t => t.number === trackNumber);

            if (trackIndex === -1) {
                return;
            }

            if (!confirm(`Delete Track ${trackNumber}? This cannot be undone.`)) {
                return;
            }

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
                    const newColor = track.ignored
                        ? 'rgba(239, 68, 68, 0.2)'
                        : this.getTrackColor(track.number - 1);
                    region.setOptions({ color: newColor });
                }
            }
        },

        /**
         * Get color for track by index
         */
        getTrackColor(idx) {
            const colors = [
                'rgba(59, 130, 246, 0.3)',
                'rgba(16, 185, 129, 0.3)',
                'rgba(245, 158, 11, 0.3)',
                'rgba(139, 92, 246, 0.3)',
                'rgba(236, 72, 153, 0.3)',
                'rgba(239, 68, 68, 0.3)',
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
                alert('No tracks selected for processing. Please un-ignore at least one track.');
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

            try {
                this.isProcessing = true;
                this.processingProgress = 0.1;
                this.processingMessage = 'Starting...';
                this.lastProgressAt = Date.now();

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

                const file = this.uploadedFiles.find(f => f.id === this.currentFileId);
                if (file) {
                    file.status = 'processing';
                }
            } catch (error) {
                console.error('Processing failed:', error);
                this.stopProcessPolling();
                this.isProcessing = false;
                this.processingProgress = 0;
                this.processingMessage = '';
                alert('Processing failed: ' + error.message);
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

                        alert(`Processing error: ${job.error || 'Unknown error'}`);
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
