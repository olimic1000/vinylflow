/**
 * Vinyl Digitizer Frontend Application
 * Alpine.js application for managing vinyl digitization workflow
 */

function vinylApp() {
    return {
        // WebSocket connection
        ws: null,

        // UI State
        dragging: false,
        showSettings: false,

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
        currentZoom: 50,  // Track current zoom level (pixels per second)

        // Discogs Search
        searchQuery: '',
        searchResults: [],
        selectedRelease: null,
        trackMappingReversed: false,
        customMapping: [],  // Stores custom track order indices

        // Processing
        processingProgress: 0,
        processingMessage: '',
        successMessage: '',

        // Config
        config: {
            silence_threshold: -40,
            min_silence_duration: 1.5,
            min_track_length: 30,
            output_dir: '',
            flac_compression: 8
        },

        /**
         * Initialize the application
         */
        async init() {
            await this.loadConfig();
            this.connectWebSocket();

            // Auto-set search query from filename when file is selected
            this.$watch('currentFile', (file) => {
                if (file && !this.searchQuery) {
                    // Clean filename for search
                    this.searchQuery = this.cleanFilename(file.filename);
                }
            });
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
                const data = JSON.parse(event.data);
                this.handleWebSocketMessage(data);
            };

            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };

            this.ws.onclose = () => {
                console.log('WebSocket disconnected, reconnecting...');
                setTimeout(() => this.connectWebSocket(), 3000);
            };

            // Send ping every 30 seconds to keep connection alive
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
                        this.successMessage = `✓ Album saved to: ${data.output_path}\nTracks: ${data.tracks.join(', ')}`;

                        // Update file status
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

                        // Update file status
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
         * Handle file drop
         */
        async handleDrop(event) {
            this.dragging = false;
            const files = Array.from(event.dataTransfer.files).filter(f =>
                f.name.toLowerCase().endsWith('.wav')
            );
            await this.uploadFiles(files);
        },

        /**
         * Handle file selection from input
         */
        async handleFileSelect(event) {
            const files = Array.from(event.target.files);
            await this.uploadFiles(files);
            event.target.value = ''; // Reset input
        },

        /**
         * Upload files to server
         */
        async uploadFiles(files) {
            if (files.length === 0) return;

            const formData = new FormData();
            files.forEach(file => formData.append('files', file));

            try {
                const response = await fetch('/api/upload', {
                    method: 'POST',
                    body: formData
                });
                const data = await response.json();

                // Add uploaded files to queue
                data.files.forEach(file => {
                    this.uploadedFiles.push({
                        ...file,
                        status: 'uploaded'
                    });
                });

                // Auto-select first file if none selected
                if (!this.currentFile && data.files.length > 0) {
                    this.selectFile(data.files[0].id);
                }

                alert(`Uploaded ${data.files.length} file(s)`);
            } catch (error) {
                console.error('Upload failed:', error);
                alert('Upload failed');
            }
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

            // Destroy existing waveform
            this.destroyWaveform();

            // Set search query from filename
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

                // Check for error response
                if (!response.ok || !data.tracks) {
                    throw new Error(data.detail || 'Analysis failed');
                }

                // Add editing and ignored flags to each track
                this.detectedTracks = data.tracks.map(track => ({
                    ...track,
                    editing: false,
                    ignored: false
                }));
                this.processingMessage = '';

                // Update file status
                const file = this.uploadedFiles.find(f => f.id === this.currentFileId);
                if (file) {
                    file.status = 'analyzed';
                }

                // Initialize waveform visualization
                await this.initWaveform();

                // Auto-search Discogs if we have a query
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

            // Confirm with user
            if (!confirm('Re-analyze this file? Current track boundaries and search results will be reset.')) {
                return;
            }

            // Reset UI state
            this.detectedTracks = [];
            this.selectedRelease = null;
            this.searchResults = [];
            this.currentPlayingTrack = null;
            this.trackMappingReversed = false;
            this.customMapping = [];

            // Stop any playing audio
            if (this.$refs.audioPlayer) {
                this.$refs.audioPlayer.pause();
                this.$refs.audioPlayer.currentTime = 0;
            }

            // Destroy waveform
            this.destroyWaveform();

            // Re-run analysis
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

            try {
                const response = await fetch('/api/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        query: this.searchQuery,
                        max_results: 8
                    })
                });
                const data = await response.json();

                this.searchResults = data.results;

                if (this.searchResults.length === 0) {
                    alert('No results found. Try a different search query.');
                }
            } catch (error) {
                console.error('Search failed:', error);
                alert('Search failed: ' + error.message);
            }
        },

        /**
         * Select a release from search results
         */
        selectRelease(release) {
            this.selectedRelease = release;
            this.trackMappingReversed = false;

            // Initialize custom mapping with default 1:1 order
            this.customMapping = Array.from({ length: this.detectedTracks.length }, (_, i) => i);
        },

        /**
         * Reverse track mapping order
         */
        reverseMapping() {
            this.trackMappingReversed = !this.trackMappingReversed;

            // Reset custom mapping when reversing
            if (this.trackMappingReversed) {
                // Reverse order
                const numTracks = this.detectedTracks.length;
                this.customMapping = Array.from({ length: numTracks }, (_, i) => numTracks - 1 - i);
            } else {
                // Normal order
                this.customMapping = Array.from({ length: this.detectedTracks.length }, (_, i) => i);
            }
        },

        /**
         * Update custom mapping when user changes dropdown
         */
        updateCustomMapping() {
            // Custom mapping changed, clear reversed flag
            this.trackMappingReversed = false;
        },

        /**
         * Play preview of detected track (30 seconds)
         */
        playPreview(trackNumber) {
            if (!this.currentFileId) return;

            const audioPlayer = this.$refs.audioPlayer;
            const track = this.detectedTracks.find(t => t.number === trackNumber);

            // Build preview URL with custom start/end if adjusted
            let previewUrl = `/api/preview/${this.currentFileId}/${trackNumber}`;
            if (track && track.editing) {
                previewUrl += `?start=${track.start}&end=${track.end}`;
            }

            // Set source and play
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

            // Update duration
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

            // Update duration
            const start = this.detectedTracks[idx].start;
            this.detectedTracks[idx].duration = newEnd - start;

            // Update next track's start if it exists
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
                // Destroy existing waveform if any
                if (this.waveform) {
                    this.waveform.destroy();
                }

                // Create WaveSurfer instance
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

                // Register regions plugin
                console.log('📊 Checking WaveSurfer plugins...');
                console.log('WaveSurfer:', typeof WaveSurfer);
                console.log('WaveSurfer.Regions:', typeof WaveSurfer.Regions);

                if (typeof WaveSurfer === 'undefined') {
                    throw new Error('WaveSurfer library not loaded. Please refresh the page.');
                }

                if (typeof WaveSurfer.Regions === 'undefined') {
                    console.error('Available WaveSurfer keys:', Object.keys(WaveSurfer));
                    throw new Error('WaveSurfer Regions plugin not loaded. Please refresh the page and check browser console for errors.');
                }

                console.log('✅ Creating Regions plugin instance...');
                this.waveformRegions = this.waveform.registerPlugin(WaveSurfer.Regions.create());

                // Fetch peaks from backend
                const peaksResponse = await fetch(`/api/waveform-peaks/${this.currentFileId}`);
                if (!peaksResponse.ok) {
                    throw new Error('Failed to load waveform peaks');
                }
                const peaksData = await peaksResponse.json();

                // CRITICAL: Register 'ready' listener BEFORE load to ensure it fires
                this.waveform.on('ready', () => {
                    console.log('🎵 Waveform ready!');

                    // Set initial zoom to show full waveform
                    const duration = this.waveform.getDuration();
                    const container = document.getElementById('waveform');
                    const containerWidth = container ? container.offsetWidth : 1000;

                    console.log(`📊 Duration: ${duration.toFixed(1)}s, Container width: ${containerWidth}px`);

                    // Calculate pixels per second to fit entire waveform in the container
                    // Remove minimum constraint - let it zoom out as much as needed
                    const calculatedZoom = containerWidth / duration;
                    this.currentZoom = Math.max(1, Math.min(calculatedZoom, 200));  // Allow 1-200 px/sec range

                    console.log(`🔍 Setting initial zoom to ${this.currentZoom.toFixed(2)} px/sec (total width: ${(this.currentZoom * duration).toFixed(0)}px)`);
                    this.waveform.zoom(this.currentZoom);

                    // Add regions after zoom is set
                    setTimeout(() => {
                        this.addTrackRegions();
                        this.waveformLoading = false;
                    }, 100);
                });

                // Handle region updates
                this.waveformRegions.on('region-updated', (region) => {
                    console.log('🔧 Region updated:', region.id);
                    this.updateTrackFromRegion(region);
                });

                // Load with peaks data (triggers 'ready' event)
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
            console.log(`📍 Starting to add ${this.detectedTracks.length} track regions...`);
            console.log('📍 Detected tracks:', JSON.stringify(this.detectedTracks, null, 2));

            if (!this.waveformRegions) {
                console.error('❌ waveformRegions is null!');
                return;
            }

            if (!this.waveform) {
                console.error('❌ waveform is null!');
                return;
            }

            const totalDuration = this.waveform.getDuration();
            console.log(`📍 Total audio duration: ${totalDuration.toFixed(1)}s`);

            // Check if tracks fit within audio duration
            const lastTrack = this.detectedTracks[this.detectedTracks.length - 1];
            if (lastTrack && lastTrack.end > totalDuration) {
                console.warn(`⚠️ Last track ends at ${lastTrack.end.toFixed(1)}s but audio is only ${totalDuration.toFixed(1)}s!`);
                console.warn('⚠️ This means the MP3 file is shorter than the original WAV. Audio might be truncated.');
            }

            // Clear existing regions
            try {
                this.waveformRegions.clearRegions();
                console.log('✅ Cleared existing regions');
            } catch (e) {
                console.error('❌ Error clearing regions:', e);
            }

            // Create a region for each track
            const colors = [
                'rgba(59, 130, 246, 0.3)',   // Blue
                'rgba(16, 185, 129, 0.3)',   // Green
                'rgba(245, 158, 11, 0.3)',   // Amber
                'rgba(139, 92, 246, 0.3)',   // Purple
                'rgba(236, 72, 153, 0.3)',   // Pink
                'rgba(239, 68, 68, 0.3)',    // Red
            ];

            let successCount = 0;
            this.detectedTracks.forEach((track, idx) => {
                try {
                    console.log(`📍 Creating region ${idx + 1}/${this.detectedTracks.length} for Track ${track.number}...`);

                    // Clamp track times to audio duration to prevent errors
                    const clampedStart = Math.min(track.start, totalDuration);
                    const clampedEnd = Math.min(track.end, totalDuration);

                    if (clampedEnd <= clampedStart) {
                        console.warn(`⚠️ Skipping Track ${track.number} - beyond audio duration`);
                        return;
                    }

                    this.waveformRegions.addRegion({
                        start: clampedStart,
                        end: clampedEnd,
                        color: colors[idx % colors.length],
                        drag: true,
                        resize: true,
                        id: `track-${track.number}`,
                        content: `Track ${track.number}`
                    });

                    successCount++;
                    console.log(`✅ Region ${idx + 1} created: Track ${track.number} (${clampedStart.toFixed(1)}s - ${clampedEnd.toFixed(1)}s, duration: ${(clampedEnd - clampedStart).toFixed(1)}s)`);
                } catch (e) {
                    console.error(`❌ Failed to create region for Track ${track.number}:`, e);
                }
            });

            console.log(`🎉 Successfully created ${successCount}/${this.detectedTracks.length} regions`);

            // Verify regions exist
            const createdRegions = this.waveformRegions.getRegions();
            console.log(`📊 Actual regions count: ${createdRegions.length}`);
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

            // Update region color
            if (this.waveformRegions) {
                const region = this.waveformRegions.getRegions().find(r => r.id === `track-${track.number}`);
                if (region) {
                    const newColor = track.ignored
                        ? 'rgba(239, 68, 68, 0.2)'  // Red for ignored
                        : this.getTrackColor(track.number - 1);  // Original color
                    region.setOptions({ color: newColor });
                }
            }
        },

        /**
         * Get color for track by index
         */
        getTrackColor(idx) {
            const colors = [
                'rgba(59, 130, 246, 0.3)',   // Blue
                'rgba(16, 185, 129, 0.3)',   // Green
                'rgba(245, 158, 11, 0.3)',   // Amber
                'rgba(139, 92, 246, 0.3)',   // Purple
                'rgba(236, 72, 153, 0.3)',   // Pink
                'rgba(239, 68, 68, 0.3)',    // Red
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

                // Mark as editing to show we've manually adjusted
                track.editing = true;

                // Update next track's start if it exists
                const idx = this.detectedTracks.findIndex(t => t.number === trackNumber);
                if (idx >= 0 && idx + 1 < this.detectedTracks.length) {
                    this.detectedTracks[idx + 1].start = region.end;

                    // Also update the next region
                    const nextRegion = this.waveformRegions.getRegions().find(r => r.id === `track-${trackNumber + 1}`);
                    if (nextRegion) {
                        nextRegion.setOptions({ start: region.end });
                    }
                }
            }
        },

        /**
         * Play waveform audio
         */
        playWaveform() {
            if (this.waveform) {
                this.waveform.play();
            }
        },

        /**
         * Stop waveform audio
         */
        stopWaveform() {
            if (this.waveform) {
                this.waveform.pause();
            }
        },

        /**
         * Zoom in on waveform
         */
        zoomIn() {
            if (this.waveform) {
                // Increase zoom (more pixels per second = more detail)
                this.currentZoom = Math.min(this.currentZoom * 1.5, 500);
                this.waveform.zoom(this.currentZoom);
                console.log(`🔍 Zoomed in to ${this.currentZoom.toFixed(1)} px/sec`);
            }
        },

        /**
         * Zoom out on waveform
         */
        zoomOut() {
            if (this.waveform) {
                // Decrease zoom (fewer pixels per second = wider view)
                this.currentZoom = Math.max(this.currentZoom / 1.5, 10);
                this.waveform.zoom(this.currentZoom);
                console.log(`🔍 Zoomed out to ${this.currentZoom.toFixed(1)} px/sec`);
            }
        },

        /**
         * Destroy waveform instance
         */
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

            // Filter out ignored tracks
            const activeTracks = this.detectedTracks.filter(t => !t.ignored);

            if (activeTracks.length === 0) {
                alert('No tracks selected for processing. Please un-ignore at least one track.');
                return;
            }

            // Build track mapping using custom mapping indices (only for active tracks)
            const trackMapping = activeTracks.map((track, idx) => {
                const originalIdx = this.detectedTracks.indexOf(track);
                const discogsIdx = this.customMapping[originalIdx];
                const discogsTrack = this.selectedRelease.tracks[discogsIdx];
                return {
                    detected: track.number,
                    discogs: discogsTrack?.position || 'Unknown'
                };
            });

            // Include track boundaries (only for active tracks)
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
                        track_boundaries: trackBoundaries
                    })
                });

                const data = await response.json();
                console.log('Processing started:', data);

                // Update file status
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

            // Find next uploaded file
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
            // Remove extension
            let name = filename.replace(/\.wav$/i, '');

            // Replace separators with spaces
            name = name.replace(/[-_]+/g, ' ');

            // Remove extra spaces
            name = name.replace(/\s+/g, ' ').trim();

            return name;
        },

        /**
         * Format file size for display
         */
        formatSize(bytes) {
            if (!bytes) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
        },

        /**
         * Format duration in seconds to MM:SS
         */
        formatDuration(seconds) {
            if (!seconds) return '0:00';
            const mins = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return `${mins}:${secs.toString().padStart(2, '0')}`;
        },

        /**
         * Format timestamp for display
         */
        formatTime(seconds) {
            return this.formatDuration(seconds);
        },

        /**
         * Get CSS class for status badge
         */
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
