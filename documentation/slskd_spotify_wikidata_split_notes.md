# slskd, Spotify and Wikidata split notes

## slskd

New structure:

```text
api_clients/slskd_http.py
services/downloads/slskd_service.py
api_clients/slskd.py
```

`slskd_http.py` owns raw HTTP only.
`slskd_service.py` owns search workflow, quality filtering, transfer parsing and download cleanup.
`slskd.py` remains a compatibility facade.

## Spotify

New structure:

```text
api_clients/spotify_http.py
services/enrichment/spotify_service.py
api_clients/spotify.py
```

`spotify_http.py` owns token/auth/raw API calls.
`spotify_service.py` owns artist ID caching, singles track ID detection, track search and metadata wrappers.
`spotify.py` remains a compatibility facade.

Note: Spotify single detection should feed `services.enrichment.single_detection_service`, not popularity math directly.

## Wikidata

New structure:

```text
api_clients/wikidata_http.py
services/enrichment/artist_bio_service.py
api_clients/wikidata.py
```

`wikidata_http.py` owns Wikidata/Wikipedia HTTP.
`artist_bio_service.py` owns choosing the best entity and returning a biography.
`wikidata.py` remains a compatibility facade.

## Apply guidance

Overwrite the old api_clients files only after adding the new service modules.
Keep the facade files while you migrate imports gradually.
