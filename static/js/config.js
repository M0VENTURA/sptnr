/**
 * Config page JavaScript
 * Handles all config page interactions: saving, user management, search filters,
 * upcoming releases sources, features/weights, and scheduler controls.
 */

const DEFAULT_STRIP_KEYWORDS = [
  'remastered', 'remaster', 'remastered 2022', 'remastered 2023', 'remastered 2024',
  'remastered 2025', 'remastered 2021', 'remastered 2020', 'remastered 2019',
  'remastered 2018', 'remastered 2017', 'remastered 2016', 'remastered 2015',
  'remastered 2014', 'remastered 2013', 'remastered 2012', 'remastered 2011',
  'remastered 2010', 'remastered 2009', 'remastered 2008', 'remastered 2007',
  'remastered 2006', 'remastered 2005', 'remastered 2004', 'remastered 2003',
  'remastered 2002', 'remastered 2001', 'remastered 2000', 'remastered 1999',
  'remastered 1998', 'remastered 1997', 'remastered 1996', 'remastered 1995',
  'remastered 1994', 'remastered 1993', 'remastered 1992', 'remastered 1991',
  'remastered 1990', 'remastered 1989', 'remastered 1988', 'remastered 1987',
  'remastered 1986', 'remastered 1985', 'remastered 1984', 'remastered 1983',
  'remastered 1982', 'remastered 1981', 'remastered 1980',
  'radio edit', 'radio version', 'single edit', 'album version',
  'extended version', 'extended mix', 'club mix', 'dub mix',
  'instrumental', 'a cappella', 'karaoke version', 'backing track',
  'explicit', 'clean', 'bonus track', 'bonus', 'edit'
];

// ===== UTILITY FUNCTIONS =====

function escapeHtml(text) {
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
  return String(text).replace(/[&<>"']/g, m => map[m]);
}

function showToast(title, message, type) {
  const toastEl = document.getElementById('configToast');
  if (!toastEl) return;
  const toast = new bootstrap.Toast(toastEl);
  document.getElementById('toastTitle').textContent = title;
  document.getElementById('toastMessage').textContent = message;
  toastEl.classList.remove('bg-success', 'bg-danger', 'bg-warning', 'text-white', 'text-dark');
  if (type === 'success') toastEl.classList.add('bg-success', 'text-white');
  else if (type === 'error' || type === 'danger') toastEl.classList.add('bg-danger', 'text-white');
  else if (type === 'warning') toastEl.classList.add('bg-warning', 'text-dark');
  toast.show();
}

// ===== USER CONTEXT =====

function setActiveUserContext(userIndex) {
  localStorage.setItem('selectedNavidromeUser', userIndex);
  const notice = document.getElementById('userContextNotice');
  if (notice) {
    notice.textContent = 'User context updated for this browser.';
    notice.classList.remove('d-none');
  }
}

// ===== SEARCH FILTER KEYWORDS =====

function initializeStripKeywords() {
  const config = window.pageConfig || {};
  const keywords = config.strip_parentheses_filters || [];
  const keywordsList = document.getElementById('stripKeywordsList');
  if (!keywordsList) return;
  const displayKeywords = keywords.length > 0 ? keywords : DEFAULT_STRIP_KEYWORDS;
  displayKeywords.forEach(keyword => addKeywordBadge(keyword));
}

function addKeywordBadge(keyword) {
  const list = document.getElementById('stripKeywordsList');
  if (!list) return;
  const badge = document.createElement('span');
  badge.className = 'badge bg-primary d-flex align-items-center gap-2';
  badge.innerHTML = `
    ${escapeHtml(keyword)}
    <button type="button" class="btn-close btn-close-white" aria-label="Remove" onclick="removeKeywordBadge(this)"></button>
  `;
  list.appendChild(badge);
}

function removeKeywordBadge(btn) {
  const badge = btn.closest('.badge');
  if (badge) badge.remove();
}

function addStripKeyword() {
  const input = document.getElementById('newKeywordInput');
  if (!input) return;
  const keyword = input.value.trim().toLowerCase();
  if (!keyword) {
    showToast('Error', 'Please enter a keyword', 'warning');
    return;
  }
  const existingKeywords = Array.from(document.querySelectorAll('#stripKeywordsList .badge'))
    .map(el => {
      const text = el.textContent.trim();
      return text.substring(0, text.length - 1).trim();
    });
  if (existingKeywords.map(k => k.toLowerCase()).includes(keyword.toLowerCase())) {
    showToast('Warning', 'This keyword already exists', 'warning');
    return;
  }
  addKeywordBadge(keyword);
  input.value = '';
  input.focus();
}

function resetStripKeywords() {
  if (!confirm('Reset search filter keywords to system defaults?')) return;
  const list = document.getElementById('stripKeywordsList');
  if (!list) return;
  list.innerHTML = '';
  DEFAULT_STRIP_KEYWORDS.forEach(keyword => addKeywordBadge(keyword));
  showToast('Success', 'Keywords reset to defaults', 'success');
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && document.activeElement && document.activeElement.id === 'newKeywordInput') {
    e.preventDefault();
    addStripKeyword();
  }
});

// ===== BUILD CONFIG OBJECT =====

function buildConfigObject() {
  function getValue(id, defaultValue) {
    const el = document.getElementById(id);
    return el ? el.value : (defaultValue || '');
  }
  function getChecked(id, defaultValue) {
    const el = document.getElementById(id);
    return el ? el.checked : (defaultValue || false);
  }

  const navidrome_users = [];
  document.querySelectorAll('.user-card').forEach(card => {
    const user = {
      user: card.querySelector('.user-nav-username').value,
      display_name: card.querySelector('.user-display-name').value,
      base_url: card.querySelector('.user-nav-url').value,
      pass: card.querySelector('.user-nav-password').value,
      lastfm_username: card.querySelector('.user-lastfm-username').value,
      listenbrainz_user_token: card.querySelector('.user-listenbrainz-token').value
    };
    if (user.user && user.base_url) {
      navidrome_users.push(user);
    }
  });

  return {
    navidrome_users: navidrome_users,
    qbittorrent: {
      enabled: getChecked('qbit_enabled'),
      web_url: getValue('qbit_web_url'),
      username: getValue('qbit_username'),
      password: getValue('qbit_password'),
      downloads_folder: getValue('qbit_downloads_folder')
    },
    slskd: {
      enabled: getChecked('slskd_enabled'),
      web_url: getValue('slskd_web_url'),
      api_key: getValue('slskd_api_key')
    },
    downloads: {
      folder: getValue('downloads_folder'),
      external_export_path: getValue('external_export_path'),
      file_name_format: getValue('downloads_file_name_format', '{album_artist}/{year} - {album}/{track_number}. {artist} - {title}'),
      quality_filter: (() => {
        const priorities = [];
        if (getChecked('downloads_quality_allow_mp3_320', true)) priorities.push({ format: 'mp3', bitrate_kbps: 320 });
        if (getChecked('downloads_quality_allow_flac', true)) priorities.push({ format: 'flac', bitrate_kbps: null });
        return {
          enabled: getChecked('downloads_quality_enabled'),
          reject_others: getChecked('downloads_quality_reject_others', true),
          bitrate_tolerance: parseInt(getValue('downloads_quality_bitrate_tolerance', '5')) || 5,
          priorities: priorities
        };
      })(),
      conversion: {
        enabled: getChecked('downloads_conversion_enabled'),
        mode: getValue('downloads_conversion_mode', 'flac_to_mp3'),
        mp3_bitrate_kbps: parseInt(getValue('downloads_conversion_mp3_bitrate', '320')) || 320,
        original_handling: getValue('downloads_conversion_original_handling', 'move_to_original'),
        original_subfolder: getValue('downloads_conversion_original_subfolder', 'Original')
      }
    },
    watcher: {
      scan_interval: parseInt(getValue('watcher_scan_interval', '30')) || 30,
      navidrome_sync_wait: parseInt(getValue('watcher_navidrome_sync_wait', '600')) || 600,
      auto_import_enabled: getChecked('watcher_auto_import', true),
      auto_popularity_scan: getChecked('watcher_auto_popularity', true),
      downloads_watcher_enabled: getChecked('watcher_downloads_enabled', true)
    },
    features: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.features) || {},
      {
        retry_scheduler: {
          interval_seconds: parseInt(getValue('retry_scheduler_interval', '60')) || 60,
          auto_start: getChecked('retry_scheduler_enabled', true)
        },
        download_queue_cleanup_scheduler: {
          enabled: getChecked('queue_cleanup_scheduler_enabled', true),
          interval_minutes: parseInt(getValue('queue_cleanup_scheduler_interval_minutes', '60')) || 60
        },
        downloads_duplicate_cleanup: {
          delete_duplicate_files: getChecked('downloads_delete_duplicate_files', true),
          prune_empty_folders: getChecked('downloads_prune_empty_folders', true)
        },
        mature_track_min_age_years: parseInt(getValue('mature_track_min_age_years', '2')) || 2,
        daily_musicbrainz_release_scan_enabled: getChecked('daily_musicbrainz_release_scan_enabled', true),
        daily_musicbrainz_release_lookback_days: parseInt(getValue('daily_musicbrainz_release_lookback_days', '42')) || 42,
        daily_musicbrainz_release_lookahead_days: parseInt(getValue('daily_musicbrainz_release_lookahead_days', '28')) || 28,
        daily_musicbrainz_release_max_artists: parseInt(getValue('daily_musicbrainz_release_max_artists', '500')) || 500,
        daily_musicbrainz_release_per_artist_limit: parseInt(getValue('daily_musicbrainz_release_per_artist_limit', '100')) || 100,
        live_musicbrainz_upcoming_releases_enabled: getChecked('live_musicbrainz_upcoming_releases_enabled', true),
        live_musicbrainz_lookback_days: parseInt(getValue('live_musicbrainz_lookback_days', '14')) || 14,
        live_musicbrainz_lookahead_days: parseInt(getValue('live_musicbrainz_lookahead_days', '180')) || 180,
        live_musicbrainz_added_lookback_days: parseInt(getValue('live_musicbrainz_added_lookback_days', '7')) || 7,
        live_musicbrainz_max_results: parseInt(getValue('live_musicbrainz_max_results', '200')) || 200
      }
    ),
    single_detection: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.single_detection) || {},
      {
        zscore_high_threshold: parseFloat(getValue('zscore_high_threshold', '1.0')) || 1.0,
        zscore_medium_threshold: parseFloat(getValue('zscore_medium_threshold', '0.6')) || 0.6,
        standout_gap_z: parseFloat(getValue('standout_gap_z', '0.75')) || 0.75,
        album_zscore_threshold: parseFloat(getValue('sd_album_zscore', '0.8')) || 0.8,
        artist_zscore_threshold: parseFloat(getValue('sd_artist_zscore', '2.2')) || 2.2,
        artist_top_percentile: parseFloat(getValue('sd_artist_pct', '0.10')) || 0.10,
        artist_min_tracks: parseInt(getValue('sd_artist_min_tracks', '10')) || 10,
        popularity_5star_z_threshold: parseFloat(getValue('popularity_5star_z_threshold', '2.0')) || 2.0,
        lb_unreliable_5star_threshold: parseFloat(getValue('lb_unreliable_5star_threshold', '0.50')) || 0.50,
        listener_5star_z_threshold: parseFloat(getValue('listener_5star_z_threshold', '1.0')) || 1.0,
        single_boost: parseFloat(getValue('single_boost', '1.15')) || 1.15,
        metadata_score_floor: parseFloat(getValue('metadata_score_floor', '5.0')) || 5.0,
        live_weight_penalty: parseFloat(getValue('live_weight_penalty', '0.5')) || 0.5,
        star_5: {
          album_z: parseFloat(getValue('star5_album_z', '1.0')) || 1.0,
          artist_z: parseFloat(getValue('star5_artist_z', '1.2')) || 1.2,
          artist_pct: parseFloat(getValue('star5_pct', '0.10')) || 0.10
        },
        star_4: {
          album_z: parseFloat(getValue('star4_album_z', '0.8')) || 0.8,
          artist_z: parseFloat(getValue('star4_artist_z', '1.0')) || 1.0,
          artist_pct: parseFloat(getValue('star4_pct', '0.20')) || 0.20
        },
        star_3: {
          album_z: parseFloat(getValue('star3_album_z', '0.0')) || 0.0
        },
        star_1: {
          album_z: parseFloat(getValue('star1_album_z', '-1.0')) || -1.0
        }
      }
    ),
    weights: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.weights) || {},
      {
        lastfm: parseFloat(getValue('weight_lastfm', '0.55')) || 0.55,
        listenbrainz: parseFloat(getValue('weight_listenbrainz', '0.35')) || 0.35,
        age: parseFloat(getValue('weight_age', '0.10')) || 0.10
      }
    ),
    popularity: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.popularity) || {},
      {
        weights: {
          lastfm: parseFloat(getValue('pop_weight_lastfm', '0.55')) || 0.55,
          listenbrainz: parseFloat(getValue('pop_weight_listenbrainz', '0.35')) || 0.35,
          age: parseFloat(getValue('pop_weight_age', '0.10')) || 0.10
        }
      }
    ),
    genres: Object.assign(
      {},
      (window.pageConfig && window.pageConfig.genres) || {},
      {
        weights: {
          musicbrainz: parseFloat(getValue('genre_weight_musicbrainz', '0.40')) || 0.40,
          discogs: parseFloat(getValue('genre_weight_discogs', '0.25')) || 0.25,
          audiodb: parseFloat(getValue('genre_weight_audiodb', '0.20')) || 0.20,
          lastfm: parseFloat(getValue('genre_weight_lastfm', '0.10')) || 0.10
        }
      }
    ),
    api_integrations: {
      lastfm: {
        enabled: getChecked('api_lastfm_enabled'),
        api_key: getValue('api_lastfm_api_key')
      },
      listenbrainz: { enabled: getChecked('api_listenbrainz_enabled') },
      discogs: {
        enabled: getChecked('api_discogs_enabled'),
        token: getValue('api_discogs_token')
      },
      musicbrainz: { enabled: getChecked('api_musicbrainz_enabled', true) },
      audiodb: {
        enabled: getChecked('api_audiodb_enabled'),
        api_key: getValue('api_audiodb_api_key')
      },
      google: {
        enabled: getChecked('api_google_enabled'),
        api_key: getValue('api_google_api_key'),
        cse_id: getValue('api_google_cse_id')
      },
      youtube: {
        enabled: getChecked('api_youtube_enabled'),
        api_key: getValue('api_youtube_api_key')
      }
    },
    strip_parentheses_filters: Array.from(document.querySelectorAll('#stripKeywordsList .badge'))
      .map(badge => badge.childNodes[0].textContent.trim())
      .filter(Boolean),
    upcoming_releases: { sources: getUpcomingSourcesForSave() },
    essentia: {
      script_path: getValue('essentia_script_path'),
      models_dir: getValue('essentia_models_dir'),
      mood_threshold: parseFloat(getValue('essentia_mood_threshold', '0.005')) || 0.005,
      per_file_timeout: parseInt(getValue('essentia_per_file_timeout', '300')) || 300,
      json_output_dir: getValue('essentia_json_output_dir'),
      tag_moods: getChecked('essentia_tag_moods', true),
      parse_json_features: getChecked('essentia_parse_json_features', true),
      delete_json_after_import: getChecked('essentia_delete_json_after_import', true),
      tag_genres: getChecked('essentia_tag_genres'),
      num_genres: parseInt(getValue('essentia_num_genres', '3')) || 3,
      genre_threshold: parseFloat(getValue('essentia_genre_threshold', '15')) || 15.0,
      genre_format: getValue('essentia_genre_format', 'parent_child')
    }
  };
}

// ===== SAVE CONFIG =====

function saveConfig() {
  const saveBtn = document.getElementById('saveBtn');
  const originalText = saveBtn ? saveBtn.innerHTML : '';
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Saving...';
  }

  // The button must ALWAYS be restored — if config collection throws (a
  // missing input, malformed field) the request never starts and without
  // this guard the page would sit on "Saving..." forever.
  let config;
  try {
    config = buildConfigObject();
  } catch (err) {
    console.error('buildConfigObject failed:', err);
    if (saveBtn) { saveBtn.disabled = false; saveBtn.innerHTML = originalText; }
    showToast('Error', 'Could not collect config: ' + (err && err.message ? err.message : err), 'error');
    return;
  }

  const urlElement = document.querySelector('form#configForm')?.dataset?.saveUrl || '/config/save-json';

  // Give the server a hard deadline so a hung request (e.g. a background
  // task blocking the worker) surfaces as an error instead of an eternal
  // spinner.
  const controller = new AbortController();
  const saveTimeout = setTimeout(() => controller.abort(), 45000);

  fetch(urlElement, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
    signal: controller.signal
  })
  .then(response => {
    const contentType = response.headers.get('content-type');
    if (!contentType || !contentType.includes('application/json')) {
      return response.text().then(text => {
        throw new Error('Server returned non-JSON response: ' + text.substring(0, 100));
      });
    }
    return response.json().then(data => ({ status: response.status, data }));
  })
  .then(result => {
    if (result.status !== 200) {
      showToast('Error', result.data.error || 'Failed to save configuration', 'error');
    } else if (result.data.success) {
      showToast('Success', 'Configuration saved successfully', 'success');
      window.pageConfig = config;
    } else {
      showToast('Error', result.data.error || 'Failed to save configuration', 'error');
    }
  })
  .catch(error => {
    if (error && error.name === 'AbortError') {
      showToast('Error', 'Save timed out after 45s — the server did not respond. Check the app logs.', 'error');
    } else {
      showToast('Error', error.message || 'Network error', 'error');
    }
  })
  .finally(() => {
    clearTimeout(saveTimeout);
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.innerHTML = originalText;
    }
  });
}

// ===== RAW YAML =====

function saveRawYaml() {
  const saveBtn = document.querySelector('#rawYamlModal .btn-primary');
  saveBtn.disabled = true;
  const originalText = saveBtn.textContent;
  saveBtn.textContent = 'Saving...';

  const configContent = document.getElementById('config_content').value;
  const formData = new FormData();
  formData.append('config_content', configContent);

  const urlElement = document.querySelector('#rawYamlModal')?.dataset?.saveUrl || '/config/editor';

  fetch(urlElement, {
    method: 'POST',
    body: formData
  })
  .then(() => {
    showToast('Success', 'Configuration saved successfully', 'success');
    const modalEl = document.getElementById('rawYamlModal');
    if (modalEl) bootstrap.Modal.getInstance(modalEl)?.hide();
  })
  .catch(error => {
    showToast('Error', 'Network error: ' + error.message, 'error');
  })
  .finally(() => {
    saveBtn.disabled = false;
    saveBtn.textContent = originalText;
  });
}

// ===== SYNC RATINGS =====

function syncRatingsNow() {
  const btn = document.getElementById('syncRatingsNowBtn');
  if (!btn) return;
  btn.disabled = true;
  const originalHtml = btn.innerHTML;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Syncing...';

  fetch('/api/navidrome/ratings/sync-now', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({})
  })
  .then(async response => {
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, data };
  })
  .then(result => {
    const data = result.data || {};
    if (!result.ok || data.success === false) {
      showToast('Sync Failed', data.error || 'Failed to sync ratings', 'error');
      return;
    }
    const users = Array.isArray(data.users) ? data.users : [];
    const usersWithErrors = users.filter(u => (u.failed || 0) > 0);
    const summary = `Synced ${data.synced_total || 0}/${data.attempted_total || 0} rating updates across ${data.users_total || 0} user(s)`;
    if (usersWithErrors.length > 0) {
      const failingNames = usersWithErrors.map(u => u.display_name || u.username || 'Unknown user').join(', ');
      showToast('Partial Sync Complete', `${summary}. Issues: ${failingNames}`, 'warning');
    } else {
      showToast('Sync Complete', summary, 'success');
    }
  })
  .catch(error => {
    showToast('Sync Failed', error.message || 'Network error while syncing ratings', 'error');
  })
  .finally(() => {
    btn.disabled = false;
    btn.innerHTML = originalHtml;
  });
}

function toggleSyncRatingsToAllUsers(enabled) {
  fetch('/api/features/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sync_ratings_to_all_users: enabled })
  })
  .then(async response => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.success === false) {
      showToast('Error', data.error || 'Failed to update setting', 'error');
      const toggle = document.getElementById('syncRatingsToAllUsersToggle');
      if (toggle) toggle.checked = !enabled;
      return;
    }
    showToast('Setting Saved', enabled
      ? 'Star ratings will be synced to all Navidrome users after each scan and for individual track ratings.'
      : 'Star ratings will only be synced to the primary Navidrome user.', 'success');
  })
  .catch(error => {
    showToast('Error', error.message || 'Network error', 'error');
    const toggle = document.getElementById('syncRatingsToAllUsersToggle');
    if (toggle) toggle.checked = !enabled;
  });
}

// ===== MULTI-USER MANAGEMENT =====

function addNewUser() {
  const container = document.getElementById('usersContainer');
  const emptyMessage = document.getElementById('emptyUsersMessage');
  if (emptyMessage) emptyMessage.remove();
  const existingCards = container.querySelectorAll('.user-card');
  const userIndex = existingCards.length;

  const userCard = document.createElement('div');
  userCard.className = 'user-card mb-4 p-3 border rounded';
  userCard.setAttribute('data-user-index', userIndex);
  userCard.innerHTML = `
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h6 class="mb-0">User ${userIndex + 1}: <span class="user-display-value">New User</span></h6>
      <button type="button" class="btn btn-sm btn-outline-danger" onclick="removeUser(${userIndex})">
        <i class="bi bi-trash"></i> Remove
      </button>
    </div>
    <div class="mb-3">
      <h6 class="text-primary mb-2"><i class="bi bi-music-note-beamed"></i> Navidrome</h6>
      <div class="row g-2">
        <div class="col-12">
          <label class="form-label form-label-sm">Display Name</label>
          <input type="text" class="form-control form-control-sm user-display-name" placeholder="My Name" onchange="updateUserTitle(this)">
        </div>
        <div class="col-12">
          <label class="form-label form-label-sm">Navidrome Base URL</label>
          <input type="text" class="form-control form-control-sm user-nav-url" placeholder="http://localhost:4533">
        </div>
        <div class="col-12 col-sm-6">
          <label class="form-label form-label-sm">Username</label>
          <input type="text" class="form-control form-control-sm user-nav-username" placeholder="username">
        </div>
        <div class="col-12 col-sm-6">
          <label class="form-label form-label-sm">Password</label>
          <input type="password" class="form-control form-control-sm user-nav-password" placeholder="••••••••">
        </div>
      </div>
    </div>
    <div class="mb-3">
      <h6 class="text-info mb-2"><i class="bi bi-database-fill"></i> Last.fm</h6>
      <div class="row g-2">
        <div class="col-12">
          <label class="form-label form-label-sm">Last.fm Username</label>
          <input type="text" class="form-control form-control-sm user-lastfm-username" placeholder="lastfm_username">
          <small class="form-text text-muted">Your Last.fm username (for personalized recommendations)</small>
        </div>
      </div>
    </div>
    <div class="mb-3">
      <h6 class="text-secondary mb-2"><i class="bi bi-headphones"></i> ListenBrainz</h6>
      <div class="row g-2">
        <div class="col-12">
          <label class="form-label form-label-sm">ListenBrainz User Token</label>
          <input type="text" class="form-control form-control-sm user-listenbrainz-token" placeholder="listenbrainz_user_token">
          <small class="form-text text-muted">Your ListenBrainz personal token from <a href="https://listenbrainz.org/settings/" target="_blank">listenbrainz.org/settings/</a></small>
        </div>
      </div>
    </div>
  `;
  container.appendChild(userCard);
}

function removeUser(idx) {
  const userCards = document.querySelectorAll('.user-card');
  if (userCards[idx]) userCards[idx].remove();
}

function updateUserTitle(displayNameInput) {
  const userCard = displayNameInput.closest('.user-card');
  if (!userCard) return;
  const displayValue = userCard.querySelector('.user-display-value');
  if (displayValue) displayValue.textContent = displayNameInput.value.trim() || 'New User';
}

// ===== FEATURES & WEIGHTS =====

function saveFeaturesWeights() {
  const config = buildConfigObject();

  document.querySelectorAll('.feature-field').forEach(input => {
    let key = input.name.replace('feature_', '');
    let val = input.value;
    if (input.tagName === 'SELECT') {
      if (val === 'true' || val === 'false') val = (val === 'true');
    } else if (window.pageConfig && window.pageConfig.features && Array.isArray(window.pageConfig.features[key])) {
      val = val.split(',').map(s => s.trim()).filter(Boolean);
    } else if (!isNaN(val) && val !== '') {
      val = Number(val);
    }
    config.features[key] = val;
  });

  const saveBtn = event.target;
  saveBtn.disabled = true;
  const originalText = saveBtn.textContent;
  saveBtn.textContent = 'Saving...';

  fetch('/config/save-json', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config)
  })
  .then(r => r.json())
  .then(resp => {
    if (resp.success) {
      showToast('Success', 'Features saved successfully', 'success');
      setTimeout(() => location.reload(), 1000);
    } else {
      showToast('Error', resp.error || 'Unknown error', 'error');
    }
  })
  .catch(err => {
    showToast('Error', 'Network error: ' + err.message, 'error');
  })
  .finally(() => {
    saveBtn.disabled = false;
    saveBtn.textContent = originalText;
  });
}

// ===== ESSENTIA =====

function toggleEssentiaGenreSettings() {
  const cb = document.getElementById('essentia_tag_genres');
  const panel = document.getElementById('essentia-genre-settings');
  if (panel) panel.classList.toggle('d-none', !cb || !cb.checked);
}

let _essentiaDownloadPollTimer = null;

function startEssentiaDownload() {
  const btn = document.getElementById('btn-essentia-download');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span> Starting…';

  fetch('/api/essentia/download-models', { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'already_running') {
        _essentiaSetStatusText('Download already in progress…');
        document.getElementById('essentia-download-status').classList.remove('d-none');
      }
      pollEssentiaDownloadStatus();
    })
    .catch(err => {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Download Models &amp; Script';
      showToast('Error', 'Could not start Essentia download: ' + err, 'error');
    });
}

function pollEssentiaDownloadStatus() {
  if (_essentiaDownloadPollTimer) clearTimeout(_essentiaDownloadPollTimer);
  const CLONE_PCT = 10;
  const DOWNLOAD_RANGE_PCT = 85;
  const EXPECTED_FILES = 5;

  fetch('/api/essentia/download-status')
    .then(r => r.json())
    .then(data => {
      const statusDiv = document.getElementById('essentia-download-status');
      const bar = document.getElementById('essentia-download-bar');
      const btn = document.getElementById('btn-essentia-download');
      if (!data || data.status === 'idle') {
        if (statusDiv) statusDiv.classList.add('d-none');
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-cloud-download"></i> Download Models &amp; Script';
        return;
      }
      if (statusDiv) statusDiv.classList.remove('d-none');
      const total = data.files_total || EXPECTED_FILES;
      const done = data.files_done || 0;
      bar.classList.remove('bg-success', 'bg-danger');
      bar.classList.add('progress-bar-striped', 'progress-bar-animated');

      if (data.status === 'complete') {
        bar.style.width = '100%';
        bar.classList.remove('progress-bar-animated');
        bar.classList.add('bg-success');
        _essentiaSetStatusText('✅ Download complete — models saved to ' + (data.models_dir || '/opt/essentia_models'));
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-cloud-download"></i> Download Models &amp; Script';
      } else if (data.status === 'error') {
        bar.classList.remove('progress-bar-animated');
        bar.classList.add('bg-danger');
        bar.style.width = '100%';
        _essentiaSetStatusText('❌ Error: ' + (data.error || 'unknown error'));
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-cloud-download"></i> Retry Download';
      } else {
        const pct = data.status === 'cloning_script'
          ? CLONE_PCT
          : Math.max(CLONE_PCT, Math.round(CLONE_PCT + (done / total) * DOWNLOAD_RANGE_PCT));
        bar.style.width = pct + '%';
        _essentiaSetStatusText(data.current_step || 'Downloading…');
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span> Downloading…';
        _essentiaDownloadPollTimer = setTimeout(pollEssentiaDownloadStatus, 2000);
      }
    })
    .catch(() => {
      _essentiaDownloadPollTimer = setTimeout(pollEssentiaDownloadStatus, 3000);
    });
}

function _essentiaSetStatusText(msg) {
  const el = document.getElementById('essentia-download-status-text');
  if (el) el.textContent = msg;
}

// ===== UPCOMING RELEASES SOURCES =====

let upcomingSources = [];

function initUpcomingSourcesUI() {
  const config = window.pageConfig || {};
  const savedSources = ((config.upcoming_releases || {}).sources || []);
  if (Array.isArray(savedSources) && savedSources.length > 0) {
    upcomingSources = savedSources.map(s => Object.assign({}, s));
  } else {
    const y = new Date().getFullYear();
    upcomingSources = [
      { key: `${y}_albums`,      name: `General ${y} Albums`,        url: `https://en.wikipedia.org/wiki/List_of_${y}_albums`,            columns: ['day','artist','album','genre'], enabled: true },
      { key: `${y}_heavy_metal`, name: `Heavy Metal ${y}`,           url: `https://en.wikipedia.org/wiki/${y}_in_heavy_metal_music`,      columns: ['day','artist','album'],         enabled: true },
      { key: `${y}_rock`,        name: `Rock Music ${y}`,            url: `https://en.wikipedia.org/wiki/${y}_in_rock_music`,             columns: ['day','artist','album'],         enabled: true },
      { key: `${y}_kpop`,        name: `K-Pop/Korean Music ${y}`,    url: `https://en.wikipedia.org/wiki/${y}_in_South_Korean_music`,     columns: ['day','album','artist'],         enabled: true },
      { key: `${y}_american`,    name: `American Music ${y}`,        url: `https://en.wikipedia.org/wiki/${y}_in_American_music`,         columns: ['day','album','artist'],         enabled: true },
    ];
  }
  renderUpcomingSourcesList();
}

function renderUpcomingSourcesList() {
  const container = document.getElementById('upcomingSourcesList');
  if (!container) return;
  if (upcomingSources.length === 0) {
    container.innerHTML = '<p class="text-muted fst-italic">No sources configured. Add a source below.</p>';
    return;
  }
  container.innerHTML = upcomingSources.map((src, idx) => buildUpcomingSourceRow(src, idx)).join('');
}

function buildUpcomingSourceRow(src, idx) {
  const enabled = src.enabled !== false;
  const cols = Array.isArray(src.columns) ? src.columns : ['day','artist','album'];
  const colBadges = cols.map(c => {
    if (c === 'genre') {
      return `<span class="badge bg-secondary" title="Skipped during parsing" style="text-decoration:line-through;">${escapeHtml(c)}</span>`;
    }
    return `<span class="badge bg-primary">${escapeHtml(c)}</span>`;
  }).join(' <i class="bi bi-arrow-right-short text-muted"></i> ');

  return `<div class="border rounded p-3 mb-2" id="srcRow_${idx}" style="background: var(--secondary-bg, #1e1e1e);">
    <div class="d-flex flex-column flex-md-row align-items-md-start gap-3">
      <div class="form-check form-switch pt-1 flex-shrink-0">
        <input class="form-check-input" type="checkbox" role="switch" id="srcEnabled_${idx}"
               ${enabled ? 'checked' : ''} onchange="toggleUpcomingSourceEnabled(${idx})">
      </div>
      <div class="flex-grow-1 overflow-hidden">
        <div class="d-flex flex-wrap align-items-center gap-2 mb-1">
          <strong>${escapeHtml(src.name)}</strong>
          <code class="text-muted small">${escapeHtml(src.key)}</code>
          ${!enabled ? '<span class="badge bg-secondary">Disabled</span>' : ''}
        </div>
        <div class="text-muted small mb-2 text-truncate">
          <i class="bi bi-link-45deg"></i>
          <a href="${escapeHtml(src.url)}" target="_blank" rel="noopener noreferrer" class="text-muted">${escapeHtml(src.url)}</a>
        </div>
        <div class="d-flex flex-wrap align-items-center gap-1">
          <small class="text-muted me-1">Column order:</small>
          ${colBadges}
        </div>
      </div>
      <div class="d-flex gap-2 flex-shrink-0">
        <button type="button" class="btn btn-outline-secondary btn-sm" onclick="editUpcomingSource(${idx})" title="Edit source">
          <i class="bi bi-pencil"></i>
        </button>
        <button type="button" class="btn btn-outline-danger btn-sm" onclick="removeUpcomingSource(${idx})" title="Remove source">
          <i class="bi bi-trash"></i>
        </button>
      </div>
    </div>
  </div>`;
}

function toggleUpcomingSourceEnabled(idx) {
  const cb = document.getElementById('srcEnabled_' + idx);
  if (cb && upcomingSources[idx]) upcomingSources[idx].enabled = cb.checked;
}

function editUpcomingSource(idx) {
  const src = upcomingSources[idx];
  if (!src) return;
  const row = document.getElementById('srcRow_' + idx);
  if (!row) return;
  const cols = Array.isArray(src.columns) ? src.columns.join(', ') : 'day, artist, album';
  row.innerHTML = `<div class="row g-2 mb-2">
    <div class="col-12 col-md-4">
      <label class="form-label small fw-bold">Source Key</label>
      <input type="text" class="form-control form-control-sm" id="editSrcKey_${idx}" value="${escapeHtml(src.key)}" placeholder="e.g. 2026_jazz">
      <div class="form-text">Must include a 4-digit year</div>
    </div>
    <div class="col-12 col-md-4">
      <label class="form-label small fw-bold">Display Name</label>
      <input type="text" class="form-control form-control-sm" id="editSrcName_${idx}" value="${escapeHtml(src.name)}">
    </div>
    <div class="col-12 col-md-4">
      <label class="form-label small fw-bold">Column Order</label>
      <input type="text" class="form-control form-control-sm" id="editSrcCols_${idx}" value="${escapeHtml(cols)}">
      <div class="form-text">e.g. <code>day, artist, album</code></div>
    </div>
    <div class="col-12">
      <label class="form-label small fw-bold">Wikipedia URL</label>
      <input type="url" class="form-control form-control-sm" id="editSrcUrl_${idx}" value="${escapeHtml(src.url)}">
    </div>
  </div>
  <div class="d-flex gap-2">
    <button type="button" class="btn btn-primary btn-sm" onclick="saveUpcomingSourceEdit(${idx})">
      <i class="bi bi-check-circle"></i> Save
    </button>
    <button type="button" class="btn btn-outline-secondary btn-sm" onclick="renderUpcomingSourcesList()">Cancel</button>
  </div>`;
}

function saveUpcomingSourceEdit(idx) {
  const key  = (document.getElementById('editSrcKey_' + idx)?.value  || '').trim();
  const name = (document.getElementById('editSrcName_' + idx)?.value || '').trim();
  const url  = (document.getElementById('editSrcUrl_' + idx)?.value  || '').trim();
  const colsRaw = (document.getElementById('editSrcCols_' + idx)?.value || '').trim();
  if (!key || !name || !url) { showToast('Error', 'Key, name, and URL are required', 'warning'); return; }
  if (!/20[2-9]\d/.test(key)) { showToast('Warning', 'Source key should include a year (e.g. 2026_...) for date parsing', 'warning'); }
  const cols = parseUpcomingCols(colsRaw);
  if (!cols.includes('artist') || !cols.includes('album')) {
    showToast('Error', 'Column order must include both "artist" and "album"', 'warning'); return;
  }
  upcomingSources[idx] = { ...upcomingSources[idx], key, name, url, columns: cols };
  renderUpcomingSourcesList();
}

function removeUpcomingSource(idx) {
  const src = upcomingSources[idx];
  if (!src) return;
  if (!confirm('Remove source "' + src.name + '"?')) return;
  upcomingSources.splice(idx, 1);
  renderUpcomingSourcesList();
}

function toggleAddUpcomingSourceForm() {
  const form = document.getElementById('addUpcomingSourceForm');
  if (form) form.classList.toggle('d-none');
}

function addUpcomingSource() {
  const key     = (document.getElementById('newSrcKey')?.value     || '').trim();
  const name    = (document.getElementById('newSrcName')?.value    || '').trim();
  const url     = (document.getElementById('newSrcUrl')?.value     || '').trim();
  const colsRaw = (document.getElementById('newSrcColumns')?.value || '').trim();
  if (!key || !name || !url) { showToast('Error', 'Key, name, and URL are required', 'warning'); return; }
  if (upcomingSources.some(s => s.key === key)) {
    showToast('Error', 'Source key "' + key + '" already exists', 'warning'); return;
  }
  if (!/20[2-9]\d/.test(key)) { showToast('Warning', 'Source key should include a year (e.g. 2026_...) for date parsing', 'warning'); }
  const cols = parseUpcomingCols(colsRaw || 'day, artist, album');
  if (!cols.includes('artist') || !cols.includes('album')) {
    showToast('Error', 'Column order must include both "artist" and "album"', 'warning'); return;
  }
  upcomingSources.push({ key, name, url, columns: cols, enabled: true });
  document.getElementById('newSrcKey').value = '';
  document.getElementById('newSrcName').value = '';
  document.getElementById('newSrcUrl').value = '';
  document.getElementById('newSrcColumns').value = 'day, artist, album';
  toggleAddUpcomingSourceForm();
  renderUpcomingSourcesList();
}

function parseUpcomingCols(raw) {
  const valid = ['day','artist','album','genre'];
  return raw.split(',').map(c => c.trim().toLowerCase()).filter(c => valid.includes(c));
}

function getUpcomingSourcesForSave() {
  upcomingSources.forEach((src, idx) => {
    const cb = document.getElementById('srcEnabled_' + idx);
    if (cb) src.enabled = cb.checked;
  });
  return upcomingSources.map(s => ({
    key: s.key,
    name: s.name,
    url: s.url,
    columns: Array.isArray(s.columns) ? s.columns : ['day','artist','album'],
    enabled: s.enabled !== false,
  }));
}

// ===== RETRY SCHEDULER =====

function getRetrySchedulerStatus() {
  fetch('/api/downloads/scheduler/status')
    .then(r => r.json())
    .then(data => {
      const statusIcon = document.getElementById('schedulerStatusIcon');
      const statusText = document.getElementById('schedulerStatusText');
      const statusDetail = document.getElementById('schedulerStatusDetail');
      const startBtn = document.getElementById('startSchedulerBtn');
      const stopBtn = document.getElementById('stopSchedulerBtn');
      const isRunning = data.running || false;
      if (statusIcon) {
        statusIcon.className = isRunning ? 'bi bi-circle-fill text-success' : 'bi bi-circle-fill text-muted';
        statusIcon.title = isRunning ? 'Running' : 'Stopped';
      }
      if (statusText) statusText.textContent = isRunning ? 'Running' : 'Stopped';
      if (statusDetail) {
        statusDetail.innerHTML = isRunning
          ? '<span class="badge bg-success"><i class="bi bi-play-circle"></i> Running</span> - Retry scheduler is actively monitoring and retrying failed downloads every configured interval.'
          : '<span class="badge bg-secondary"><i class="bi bi-stop-circle"></i> Stopped</span> - Retry scheduler is not running. Click "Start" to begin automatic retry management.';
      }
      if (startBtn) startBtn.disabled = isRunning;
      if (stopBtn) stopBtn.disabled = !isRunning;
    })
    .catch(e => {
      console.error('Error getting scheduler status:', e);
      const statusDetail = document.getElementById('schedulerStatusDetail');
      if (statusDetail) statusDetail.textContent = 'Unable to load status';
    });
}

function startRetryScheduler() {
  const btn = document.getElementById('startSchedulerBtn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Starting...';
  }
  fetch('/api/downloads/scheduler/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' }
  })
  .then(r => r.json())
  .then(data => {
    if (data.success) {
      showToast('Success', 'Retry scheduler started', 'success');
      setTimeout(getRetrySchedulerStatus, 500);
    } else {
      showToast('Error', data.error || 'Failed to start scheduler', 'error');
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> Start'; }
    }
  })
  .catch(e => {
    showToast('Error', 'Network error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> Start'; }
  });
}

function stopRetryScheduler() {
  const btn = document.getElementById('stopSchedulerBtn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Stopping...';
  }
  fetch('/api/downloads/scheduler/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' }
  })
  .then(r => r.json())
  .then(data => {
    if (data.success) {
      showToast('Warning', 'Retry scheduler stopped', 'warning');
      setTimeout(getRetrySchedulerStatus, 500);
    } else {
      showToast('Error', data.error || 'Failed to stop scheduler', 'error');
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-stop-fill"></i> Stop'; }
    }
  })
  .catch(e => {
    showToast('Error', 'Network error: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-stop-fill"></i> Stop'; }
  });
}

// ===== INIT =====

document.addEventListener('DOMContentLoaded', function() {
  if (document.getElementById('stripKeywordsList')) {
    initializeStripKeywords();
  }
  if (document.getElementById('upcomingSourcesList')) {
    initUpcomingSourcesUI();
  }
  getRetrySchedulerStatus();
});
