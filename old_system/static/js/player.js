/**
 * Popularr built-in audio player
 *
 * Public API (window.Player):
 *   Player.playTrack(trackId, title, artist, albumArtUrl)  – play a single track
 *   Player.playQueue(tracks)  – play an array of {id, title, artist, albumArtUrl}
 *   Player.toggle()           – play / pause
 */
(function () {
  'use strict';

  // ── State ──────────────────────────────────────────────────────────────────
  let queue = [];          // [{id, title, artist, albumArtUrl}, …]
  let currentIndex = -1;
  let isVisible = false;

  // ── DOM refs (populated after DOMContentLoaded) ────────────────────────────
  let playerBar, audioEl, artEl, titleEl, artistEl;
  let playPauseBtn, prevBtn, nextBtn;
  let progressEl, progressBar, currentTimeEl, totalTimeEl;
  let volumeEl;

  // ── Helpers ────────────────────────────────────────────────────────────────
  function fmtTime(sec) {
    if (!sec || isNaN(sec)) return '0:00';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  function setPlayPauseIcon(playing) {
    if (!playPauseBtn) return;
    playPauseBtn.innerHTML = playing
      ? '<i class="bi bi-pause-fill"></i>'
      : '<i class="bi bi-play-fill"></i>';
    playPauseBtn.title = playing ? 'Pause' : 'Play';
  }

  // ── Core playback ──────────────────────────────────────────────────────────
  function loadAndPlay(index) {
    if (index < 0 || index >= queue.length) return;
    currentIndex = index;
    const track = queue[index];

    audioEl.src = `/api/track/${track.id}/audio`;
    audioEl.load();
    audioEl.play().catch(function (err) {
      console.warn('[Player] play() rejected:', err);
    });

    titleEl.textContent = track.title || 'Unknown';
    artistEl.textContent = track.artist || '';
    artEl.src = track.albumArtUrl || '';
    artEl.style.display = track.albumArtUrl ? 'block' : 'none';

    if (!isVisible) show();
    highlightActiveTrack(track.id);
  }

  function show() {
    playerBar.classList.remove('d-none');
    // Give the body breathing room so the fixed bar doesn't overlap content
    document.body.style.paddingBottom = '72px';
    isVisible = true;
  }

  function highlightActiveTrack(trackId) {
    document.querySelectorAll('.player-play-btn').forEach(function (btn) {
      const active = String(btn.dataset.trackId) === String(trackId);
      btn.classList.toggle('btn-success', active);
      btn.classList.toggle('btn-outline-success', !active);
      btn.querySelector('i').className = active
        ? 'bi bi-volume-up-fill'
        : 'bi bi-play-fill';
    });
  }

  // ── Audio element event listeners ──────────────────────────────────────────
  function bindAudioEvents() {
    audioEl.addEventListener('play', function () { setPlayPauseIcon(true); });
    audioEl.addEventListener('pause', function () { setPlayPauseIcon(false); });
    audioEl.addEventListener('ended', function () {
      if (currentIndex < queue.length - 1) {
        loadAndPlay(currentIndex + 1);
      } else {
        setPlayPauseIcon(false);
        highlightActiveTrack(null);
      }
    });
    audioEl.addEventListener('timeupdate', function () {
      if (!audioEl.duration) return;
      const pct = (audioEl.currentTime / audioEl.duration) * 100;
      progressBar.style.width = pct + '%';
      currentTimeEl.textContent = fmtTime(audioEl.currentTime);
    });
    audioEl.addEventListener('loadedmetadata', function () {
      totalTimeEl.textContent = fmtTime(audioEl.duration);
    });
    audioEl.addEventListener('error', function () {
      titleEl.textContent = 'Playback error';
      setPlayPauseIcon(false);
    });
  }

  // ── Progress bar click ─────────────────────────────────────────────────────
  function bindProgressClick() {
    progressEl.addEventListener('click', function (e) {
      if (!audioEl.duration) return;
      const rect = progressEl.getBoundingClientRect();
      const ratio = (e.clientX - rect.left) / rect.width;
      audioEl.currentTime = ratio * audioEl.duration;
    });
  }

  // ── Volume ─────────────────────────────────────────────────────────────────
  function bindVolumeControl() {
    volumeEl.addEventListener('input', function () {
      audioEl.volume = volumeEl.value;
    });
  }

  // ── Control buttons ────────────────────────────────────────────────────────
  function bindControlButtons() {
    playPauseBtn.addEventListener('click', function () {
      if (audioEl.paused) {
        if (audioEl.src) audioEl.play();
      } else {
        audioEl.pause();
      }
    });
    prevBtn.addEventListener('click', function () {
      if (currentIndex > 0) loadAndPlay(currentIndex - 1);
    });
    nextBtn.addEventListener('click', function () {
      if (currentIndex < queue.length - 1) loadAndPlay(currentIndex + 1);
    });
  }

  // ── Init ───────────────────────────────────────────────────────────────────
  function init() {
    playerBar     = document.getElementById('globalPlayerBar');
    audioEl       = document.getElementById('globalAudioEl');
    artEl         = document.getElementById('playerArt');
    titleEl       = document.getElementById('playerTitle');
    artistEl      = document.getElementById('playerArtist');
    playPauseBtn  = document.getElementById('playerPlayPause');
    prevBtn       = document.getElementById('playerPrev');
    nextBtn       = document.getElementById('playerNext');
    progressEl    = document.getElementById('playerProgress');
    progressBar   = document.getElementById('playerProgressBar');
    currentTimeEl = document.getElementById('playerCurrentTime');
    totalTimeEl   = document.getElementById('playerTotalTime');
    volumeEl      = document.getElementById('playerVolume');

    if (!playerBar || !audioEl) return;

    bindAudioEvents();
    bindProgressClick();
    bindVolumeControl();
    bindControlButtons();
  }

  document.addEventListener('DOMContentLoaded', init);

  // ── Public API ─────────────────────────────────────────────────────────────
  window.Player = {
    playTrack: function (trackId, title, artist, albumArtUrl) {
      queue = [{ id: trackId, title: title, artist: artist, albumArtUrl: albumArtUrl }];
      loadAndPlay(0);
    },
    playQueue: function (tracks) {
      if (!tracks || !tracks.length) return;
      queue = tracks;
      loadAndPlay(0);
    },
    toggle: function () {
      if (!audioEl) return;
      if (audioEl.paused) { if (audioEl.src) audioEl.play(); }
      else { audioEl.pause(); }
    },
  };
}());
