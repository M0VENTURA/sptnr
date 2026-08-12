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
  let queuePanel, queueList, queueCountEl, stopBtn;

  // ── Helpers ────────────────────────────────────────────────────────────────
  function fmtTime(sec) {
    if (!sec || isNaN(sec)) return '0:00';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  function esc(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  // Keep the footer-bar play/pause button in sync with playback state.
  function setPlayPauseIcons(playing) {
    if (!playPauseBtn) return;
    playPauseBtn.innerHTML = playing
      ? '<i class="bi bi-pause-fill"></i>'
      : '<i class="bi bi-play-fill"></i>';
    playPauseBtn.title = playing ? 'Pause' : 'Play';
  }

  // ── Queue panel ────────────────────────────────────────────────────────────
  function renderQueue() {
    if (!queuePanel) return;
    queueCountEl.textContent = queue.length;
    if (!queue.length) {
      queueList.innerHTML = '<div class="text-muted small px-1 py-1">Queue is empty — play an album or artist from any page.</div>';
      return;
    }
    queueList.innerHTML = queue.map(function (t, i) {
      const active = i === currentIndex;
      return '<div class="player-queue-row' + (active ? ' active' : '') + '" data-index="' + i + '" title="' + esc(t.title) + '">' +
        '<span class="text-muted" style="min-width:1.2rem;">' + (i + 1) + '</span>' +
        '<div style="min-width:0;flex:1 1 auto;">' +
          '<div class="text-truncate">' + esc(t.title || 'Unknown') + '</div>' +
          '<div class="text-muted text-truncate" style="font-size:0.72rem;">' + esc(t.artist || '') + '</div>' +
        '</div>' +
        (active ? '<i class="bi bi-volume-up-fill"></i>' : '') +
      '</div>';
    }).join('');
    queueList.querySelectorAll('.player-queue-row').forEach(function (row) {
      row.addEventListener('click', function () {
        loadAndPlay(parseInt(row.dataset.index, 10));
      });
    });
  }

  function toggleQueue() {
    if (!queuePanel) return;
    queuePanel.classList.toggle('d-none');
    if (!queuePanel.classList.contains('d-none')) renderQueue();
  }

  function clearQueue() {
    queue = [];
    currentIndex = -1;
    renderQueue();
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

    renderQueue();
    if (!isVisible) show();
    highlightActiveTrack(track.id);
  }

  function show() {
    playerBar.classList.remove('d-none');
    // Give the body breathing room so the fixed bar doesn't overlap content.
    document.body.style.paddingBottom = '72px';
    // Lift the sticky scan-status bar (and log-flyout stream) above the player.
    document.body.classList.add('player-visible');
    isVisible = true;
  }

  function hide() {
    playerBar.classList.add('d-none');
    document.body.style.paddingBottom = '';
    document.body.classList.remove('player-visible');
    isVisible = false;
  }

  function stopPlayback() {
    audioEl.pause();
    audioEl.removeAttribute('src');
    audioEl.load();
    queue = [];
    currentIndex = -1;
    highlightActiveTrack(null);
    renderQueue();
    hide();
  }

  function togglePlayPause() {
    if (!audioEl) return;
    if (audioEl.paused) {
      if (audioEl.src) audioEl.play();
    } else {
      audioEl.pause();
    }
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
    audioEl.addEventListener('play', function () { setPlayPauseIcons(true); });
    audioEl.addEventListener('pause', function () { setPlayPauseIcons(false); });
    audioEl.addEventListener('ended', function () {
      if (currentIndex < queue.length - 1) {
        loadAndPlay(currentIndex + 1);
      } else {
        setPlayPauseIcons(false);
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
      setPlayPauseIcons(false);
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

  // ── Control buttons (footer bar) ───────────────────────────────────────────
  function bindControlButtons() {
    playPauseBtn.addEventListener('click', togglePlayPause);
    prevBtn.addEventListener('click', function () {
      if (currentIndex > 0) loadAndPlay(currentIndex - 1);
    });
    nextBtn.addEventListener('click', function () {
      if (currentIndex < queue.length - 1) loadAndPlay(currentIndex + 1);
    });
    stopBtn.addEventListener('click', stopPlayback);
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
    queuePanel    = document.getElementById('playerQueuePanel');
    queueList     = document.getElementById('playerQueueList');
    queueCountEl  = document.getElementById('playerQueueCount');
    stopBtn       = document.getElementById('playerStop');

    if (!playerBar || !audioEl) return;

    const queueClearBtn = document.getElementById('playerQueueClear');
    if (queueClearBtn) queueClearBtn.addEventListener('click', clearQueue);
    const queueToggleBtn = document.getElementById('playerQueueToggle');
    if (queueToggleBtn) queueToggleBtn.addEventListener('click', toggleQueue);

    bindAudioEvents();
    bindProgressClick();
    bindVolumeControl();
    bindControlButtons();

    // Keep the sticky scan-status bar clear of the player on every page
    // (previously only the dashboard polled this class).
    isVisible = !playerBar.classList.contains('d-none');
    document.body.classList.toggle('player-visible', isVisible);
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
    toggle: togglePlayPause,
    toggleQueue: toggleQueue,
    stop: stopPlayback,
  };
}());
