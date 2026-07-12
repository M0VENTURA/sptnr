"""Shared metadata tag constants."""

EDITABLE_FIELDS = {
    "album", "artist", "title", "album_artist", "albumartist",
    "albumartistsort", "artistsort", "titlesort", "albumsort",
    "composersort", "lyricistsort", "artistssort", "albumartistssort",
    "artists", "albumartists", "arranger", "composer", "mixer", "producer",
    "writer", "performer", "conductor", "director", "djmixer", "engineer",
    "remixer", "lyricist", "label", "releasecountry", "releasestatus",
    "releasetype", "media", "barcode", "catalognumber", "asin", "recordlabel",
    "copyright", "releasedate", "year", "originalyear", "originaldate", "date",
    "track_number", "tracktotal", "disc_number", "totaldiscs", "genres", "work",
    "mood", "lyrics", "subtitle", "discsubtitle", "albumversion", "grouping",
    "movement", "movementname", "movementtotal", "key", "language", "script",
    "bpm", "danceability", "isrc", "encodedby", "encodersettings", "website",
    "license", "explicitstatus", "replaygain_track_gain", "replaygain_track_peak",
    "replaygain_album_gain", "replaygain_album_peak", "r128_track_gain",
    "r128_album_gain", "musicbrainz_albumartistid", "musicbrainz_albumid",
    "musicbrainz_albumtype", "musicbrainz_albumstatus", "musicbrainz_releasegroupid",
    "musicbrainz_releasetrackid", "musicbrainz_workid", "mbid",
}

JSON_ARRAY_FIELDS = {"artists", "performer", "producer", "writer"}

ALBUM_LEVEL_FIELDS = {
    "album", "label", "releasecountry", "releasestatus", "releasetype", "media",
    "barcode", "catalognumber", "asin", "year", "originalyear", "originaldate",
    "totaldiscs", "musicbrainz_albumid", "musicbrainz_albumtype",
    "musicbrainz_albumstatus", "musicbrainz_releasegroupid",
}

CONFLICT_PRONE_FIELDS = {
    "album_artist": "albumartist",
    "artist": "album_artist",
}
