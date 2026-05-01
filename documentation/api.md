# External API Reference – sptnr

This document describes the external APIs used by the sptnr service, their purpose within the system, and where authoritative documentation can be found.

These APIs must be used intentionally and within their documented constraints.

---

## slskd (Soulseek Daemon API)

**Purpose**
- Automates interaction with a self-hosted Soulseek daemon.
- Used for searching, queueing, downloading, and monitoring files.

**Key Characteristics**
- Stateful and side-effect heavy.
- Requires token-based authentication.
- Intended for automation and integration, not exploratory access.

**Usage Guidelines**
- Do not initiate downloads or alter queues unless explicitly required.
- Prefer inspecting state and explaining behavior over changing logic.
- Treat all download operations as high risk.

**Documentation**
- https://slskd-api.readthedocs.io/en/latest/

---

## Last.fm API

**Purpose**
- Provides social and historical music metadata.
- Used for enrichment (scrobbles, charts, popularity), not identity resolution.

**Key Characteristics**
- Requires API key and identifiable User-Agent.
- Subject to rate limiting and acceptable-use rules.

**Usage Guidelines**
- Treat all data as supplemental.
- Do not treat Last.fm data as canonical.
- Cache and batch requests where possible.

**Documentation**
- https://www.last.fm/api

---

## ListenBrainz API

**Purpose**
- Provides user-controlled listen history, statistics, and recommendations.
- Part of the open MetaBrainz ecosystem.

**Key Characteristics**
- HTTPS-only API.
- Requires user token authentication via Authorization header.
- Explicitly rate limited via response headers.

**Usage Guidelines**
- Treat user tokens as sensitive secrets.
- Always respect rate-limit headers.
- Prefer aggregation over per-track calls.

**Documentation**
- https://listenbrainz.readthedocs.io/en/latest/users/api/index.html

---

## MusicBrainz API

**Purpose**
- Provides canonical music metadata and MBID-based identity resolution.
- Acts as the source of truth for artist, release, and recording identity.

**Key Characteristics**
- No API key required, but User-Agent is mandatory.
- Polite rate limiting required.
- Primarily read-only.

**Usage Guidelines**
- Use MusicBrainz for identity resolution and normalization.
- Do not scrape or make excessive parallel requests.
- Refer to allowed submission fields before attempting any write operations.

**Documentation**
- https://musicbrainz.org/doc/MusicBrainz_API

---

## General Notes

- External APIs are not interchangeable.
- Canonical identity must come from MusicBrainz.
- Social or behavioral data must not override canonical metadata.
- Side-effecting APIs (e.g. slskd) must be treated conservatively.
