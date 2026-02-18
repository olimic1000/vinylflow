# Branching Strategy

This repo uses a two-track model while desktop builds mature.

## Branches

- `main`: stable, Docker-first releases (recommended for most users)
- `desktop-beta`: ongoing desktop work (macOS + Windows beta track)

## Daily workflow

### Desktop-only changes

1. Branch from `desktop-beta`
2. Open PR back into `desktop-beta`
3. Tag beta releases from `desktop-beta` (example: `v1.1.0-beta1`)

### Stable fixes for all users

1. Branch from `main`
2. Open PR back into `main`
3. Back-merge or cherry-pick into `desktop-beta` so branches stay aligned

## Release channels

- Stable channel: releases from `main`
- Beta channel: pre-releases from `desktop-beta`

## Promotion criteria (beta -> stable)

Promote desktop flow to `main` only when:

- install flow is consistently successful
- processing reliability matches Docker path
- support load is manageable
- macOS signing/notarization (or accepted alternative) is in place
- Windows beta reaches acceptable stability

## Guardrails

- Do not merge unfinished desktop work into `main`
- Keep Docker docs and path fully functional on `main`
- Clearly label beta builds and release notes as experimental
