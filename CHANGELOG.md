# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add a YouTube search API endpoint (`/api/search-youtube`) that returns album-oriented candidate videos.
- Add a YouTube rip API endpoint (`/api/rip-youtube`) that downloads audio with yt-dlp and writes tagged MP3 output.
- Add chapter-aware album ripping that splits single long videos into per-track files when chapter data is available.
- Add playlist-aware album ripping that downloads each playlist item as a track in playlist order.
- Add MP3 metadata tagging (artist, album, title, track number, year) and cover embedding for YouTube imports.
- Add UI controls to choose Torrent or YouTube as the album search source.
- Add UI fallback action to search YouTube when torrent album search returns no results.
- Add a UI YouTube results panel with per-result Rip actions.

### Changed

- Install ffmpeg in the Docker image to support YouTube audio extraction and chapter splitting.
- Extend README with YouTube workflow, configuration, and import-path setup.
