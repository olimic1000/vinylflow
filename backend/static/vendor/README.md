# Vendored frontend libraries

VinylFlow's UI runs fully offline: these files are served from `/static/vendor/` instead of a CDN,
and pinned so upstream changes cannot break the UI. `tests/test_vendor_offline.py` checks every
row of the table below (file exists, SHA-256 matches, served with a JS content type).

| File | Version | Source URL | SHA-256 | Licence |
|------|---------|-----------|---------|---------|
| `tailwindcss-3.4.17.js` | 3.4.17 | https://cdn.tailwindcss.com/3.4.17 | `176e894661aa9cdc9a5cba6c720044cbbf7b8bd80d1c9a142a7c24b1b6c50d15` | MIT |
| `alpinejs-3.17.4.min.js` | 3.17.4 | https://cdn.jsdelivr.net/npm/alpinejs@3.17.4/dist/cdn.min.js | `232519394c6c8fdba6f362b1d9da16106db513cdbf899011f00daab4051df31c` | MIT |
| `wavesurfer-7.8.10.min.js` | 7.8.10 | https://unpkg.com/wavesurfer.js@7.8.10/dist/wavesurfer.min.js | `f51029b41aa1761e92798a6b0e352304ba0ca27b944a8699c22f95c5156c5eba` | BSD-3-Clause |
| `wavesurfer-regions-7.8.10.min.js` | 7.8.10 | https://unpkg.com/wavesurfer.js@7.8.10/dist/plugins/regions.min.js | `d87c92071b5b008b0ef2f7817d0566aa35b7580ab3c153f486b6c2c42b1a213d` | BSD-3-Clause |

Notes:
- Tailwind is the v3 Play CDN script (JIT in the browser, no build step). Do not move to v4 without
  re-checking the UI.
- Licences: Tailwind CSS and Alpine.js are MIT. wavesurfer.js declares BSD-3-Clause in its
  package.json (not MIT).

## Updating

1. Download the new file(s) with `curl -fL <url> -o backend/static/vendor/<name>-<version>.js`
   (keep the version in the filename; delete the old file).
2. Compute the hash: `shasum -a 256 backend/static/vendor/*.js`.
3. Update the table above (name, version, URL, SHA-256).
4. Update the `<script src>` paths in `backend/static/index.html` and the filenames in
   `tests/test_vendor_offline.py` if they changed.
5. Run the offline pytest suite and check the UI in a browser.
