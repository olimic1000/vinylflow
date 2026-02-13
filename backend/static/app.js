/**
 * VinylFlow Frontend Application
 * Alpine.js application for managing vinyl digitization workflow
 */

function vinylApp() {
    return {
        // WebSocket connection
        ws: null,

        // UI State
        dragging: false,
        showSettings: false,
        uploadProgress: 0,
        searchLoading: false,
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

        // Discogs Search
        searchQuery: '',
        searchResults: [],
        selectedRelease: null,
        trackMappingReversed: false,
        customMapping: [],

        // Processing
        processingProgress: 0,
        processingMessage: '',
        successMessage: '',

        // Output format
        outputFormat: 'flac',
        availableFormats: [],

        // Config
        config: {
            silence_threshold: -40,
            min_silence_duration: 1.5,
            min_track_length: 30,
            output_dir: '',
            flac_compression: 8
        },

        // Supported input file extensions
        supportedExtensions: ['.wav', '.aiff', '.aif'],

        /**
         * Initialize the application
         */
        async init() {
            await this.loadConfig();
            await this.loadFormats();
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

            setInterval(() => {
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
                    }
                    break;

                case 'step_complete':
                    if (data.file_id === this.currentFileId) {
                        console.log(data.message);
                    }
                    break;

                case 'complete':
                    if (data.file_id === this.currentFileId) {
                        this.processingProgress = 1.0;
                        this.processingMessage = 'Complete!';
                        this.successMessage = `Album saved to: ${data.output_path}\nTracks: ${data.tracks.join(', ')}`;

                        const file = this.uploadedFiles.find(f => f.id === data.file_id);
                        if (file) {
                            file.status = 'completed';
                        }
                    }
                    break;

                case 'error':
                    if (data.file_id === this.currentFileId) {
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
            const files = Array.from(event.dataTransfer.files).filter(f =>
                this.isSupportedFile(f.name)
            );
            await this.uploadFiles(files);
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

                // Track upload progress
                xhr.upload.addEventListener('progress', (e) => {
                    if (e.lengthComputable) {
                        this.uploadProgress = (e.loaded / e.total) * 100;
                    }
                });

                // Handle completion
                xhr.addEventListener('load', () => {
                    this.uploadProgress = 100;

                    if (xhr.status >= 200 && xhr.status < 300) {
                        try {
                            const data = JSON.parse(xhr.responseText);

                            data.files.forEach(file => {
                                this.uploadedFiles.push({
                                    ...file,
                                    status: 'uploaded'
                                });
                            });

                            if (!this.currentFile && data.files.length > 0) {
                                this.selectFile(data.files[0].id);
                            }

                            alert(`Uploaded ${data.files.length} file(s)`);

                            // Reset progress after a short delay
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
            if (!this.currentFileId) return;

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
            if (!this.searchQuery.trim()) {
                alert('Please enter a search query');
                return;
            }

            this.searchLoading = true;
            this.searchResults = [];

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

                this.searchResults = data.results;

                // Debug: Check URI field
                console.log('Search results:', this.searchResults.map(r => ({
                    title: r.title,
                    uri: r.uri
                })));

                if (this.searchResults.length === 0) {
                    alert('No results found. Try a different search query.');
                }
            } catch (error) {
                console.error('Search failed:', error);
                alert('Search failed: ' + error.message);
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
                if (this.waveform) {
                    this.waveform.destroy();
                }

                this.waveform = WaveSurfer.create({
                    container: '#waveform',
                    waveColor: '#93c5fd',
                    progressColor: '#3b82f6',
                    cursorColor: '#1e40af',
                    height: 128,
                    normalize: true,
                    backend: 'WebAudio',
                    barWidth: 2,
                    barGap: 1
                });

                if (typeof WaveSurfer === 'undefined') {
                    throw new Error('WaveSurfer library not loaded. Please refresh the page.');
                }

                if (typeof WaveSurfer.Regions === 'undefined') {
                    throw new Error('WaveSurfer Regions plugin not loaded. Please refresh the page.');
                }

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

                    // Add right-click handler for manual splits
                    container.addEventListener('contextmenu', (e) => {
                        e.preventDefault();

                        // Find the waveform wrapper
                        const waveformWrapper = container.querySelector('[part="wrapper"]') ||
                                               container.querySelector('div');

                        if (!waveformWrapper) {
                            console.error('Could not find waveform wrapper');
                            return;
                        }

                        // Get the wrapper's bounding rectangle
                        const rect = waveformWrapper.getBoundingClientRect();

                        // Calculate relative position (0-1) accounting for scroll
                        // This mimics WaveSurfer's internal getRelativePointerPosition
                        const x = e.clientX - rect.left + waveformWrapper.scrollLeft;
                        const totalWidth = waveformWrapper.scrollWidth;
                        const relativeX = Math.max(0, Math.min(1, x / totalWidth));

                        // Convert to time
                        const time = relativeX * this.waveform.getDuration();

                        console.log('Split calculation:', {
                            clickX: e.clientX - rect.left,
                            scrollLeft: waveformWrapper.scrollLeft,
                            scrollWidth: totalWidth,
                            relativeX,
                            time
                        });

                        // Show context menu at click position
                        this.contextMenu.x = e.clientX;
                        this.contextMenu.y = e.clientY;
                        this.contextMenu.time = time;
                        this.contextMenu.show = true;
                    });

                    setTimeout(() => {
                        this.addTrackRegions();
                        this.waveformLoading = false;
                    }, 100);
                });

                this.waveformRegions.on('region-updated', (region) => {
                    this.updateTrackFromRegion(region);
                });

                await this.waveform.load(`/api/audio/${this.currentFileId}`, [peaksData.peaks]);

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

            // Find which track contains this time position
            const trackIndex = this.detectedTracks.findIndex(t =>
                time >= t.start && time <= t.end
            );

            if (trackIndex === -1) {
                alert('Cannot split here - position is outside all tracks');
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

            // Renumber tracks after the split
            for (let i = trackIndex + 1; i < this.detectedTracks.length; i++) {
                this.detectedTracks[i].number++;
            }

            // Replace the split track with two new tracks
            this.detectedTracks.splice(trackIndex, 1, newTrack1, newTrack2);

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

            // Remove the track
            this.detectedTracks.splice(trackIndex, 1);

            // Renumber subsequent tracks
            for (let i = trackIndex; i < this.detectedTracks.length; i++) {
                this.detectedTracks[i].number = i + 1;
            }

            // Refresh waveform regions
            this.addTrackRegions();

            // Clear any selected release (track count changed)
            if (this.selectedRelease) {
                this.selectedRelease = null;
                this.trackCountMismatch = false;
            }
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
                this.processingProgress = 0.1;
                this.processingMessage = 'Starting...';

                const response = await fetch('/api/process', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        file_id: this.currentFileId,
                        release_id: this.selectedRelease.id,
                        track_mapping: trackMapping,
                        reversed: this.trackMappingReversed,
                        track_boundaries: trackBoundaries,
                        output_format: this.outputFormat
                    })
                });

                const data = await response.json();
                console.log('Processing started:', data);

                const file = this.uploadedFiles.find(f => f.id === this.currentFileId);
                if (file) {
                    file.status = 'processing';
                }
            } catch (error) {
                console.error('Processing failed:', error);
                alert('Processing failed: ' + error.message);
            }
        },

        /**
         * Reset UI for next file
         */
        resetForNextFile() {
            this.successMessage = '';
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
