"""Waveform transients must retain their source times even on full-album files."""
import io
import shutil
import uuid
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import backend.api as api
from audio_processor import AudioProcessor


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='ffmpeg not installed')
def test_real_decode_preserves_transient_positions_and_partial_tail(tmp_path):
    samples = np.zeros(520024, dtype='<i2')  # 65.003 seconds at 8 kHz
    samples[96987] = -32768
    samples[519659] = 16384
    source = tmp_path / 'Björk – timing.wav'
    with wave.open(str(source), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(samples.tobytes())
    body = AudioProcessor().compute_waveform_peaks(source)
    assert body['duration'] == pytest.approx(65.003)
    nonzero = [(index / body['length'] * body['duration'], value)
               for index, value in enumerate(body['peaks']) if value]
    assert len(nonzero) == 2
    for (position, value), sample, amplitude in zip(nonzero, [96987, 519659], [1, .5]):
        assert abs(position - sample / 8000) < .011
        assert value == amplitude


def test_album_peaks_keep_ten_ms_detail_and_replace_old_coarse_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'UPLOAD_DIR', tmp_path / 'uploads')
    sid = str(uuid.uuid4())
    source = tmp_path / 'source.wav'
    source.touch()
    api.session_store.create(session_id=sid, source_audio=source, source_filename='album.wav',
                             source_duration=2400.003, source_size=0)
    folder = api.get_session_path(sid)
    folder.mkdir(parents=True)
    # Existing sessions must not keep the old, ~0.8-second-per-bin waveform.
    (folder / 'peaks.json').write_text('{"peaks":[0],"duration":2400.003,"length":1}')
    calls = []

    def decode(args, **kwargs):
        calls.append(args)
        output = kwargs.get('stdout') or io.BytesIO()
        for second in range(2400):
            samples = np.zeros(8000, dtype='<i2')
            if second == 123:
                samples[987] = -32768   # 123.123375 s; exercise abs(int16 min)
            if second == 2345:
                samples[5421] = 16384   # 2345.677625 s
            output.write(samples.tobytes())
        output.write(np.zeros(24, dtype='<i2').tobytes())
        return SimpleNamespace(stdout=output.getvalue() if isinstance(output, io.BytesIO) else None)

    monkeypatch.setattr('audio_processor.run_ffmpeg', decode)
    monkeypatch.setattr(api, 'run_ffmpeg', decode)
    try:
        client = TestClient(api.app)
        response = client.get(f'/api/waveform-peaks/{sid}')
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['length'] == 240001
        assert body['duration'] == pytest.approx(2400.003)
        # Positions are what WaveSurfer draws: equal bins over the duration.
        nonzero = [(i, p) for i, p in enumerate(body['peaks']) if p]
        assert len(nonzero) == 2
        for (index, peak), time, amplitude in zip(nonzero, [123.123375, 2345.677625], [1, .5]):
            assert abs(index / body['length'] * body['duration'] - time) < .011
            assert peak == amplitude
        assert client.get(f'/api/waveform-peaks/{sid}').json() == body
        assert len(calls) == 1, 'the detailed peaks should be cached'
    finally:
        api.session_store.remove(sid)
