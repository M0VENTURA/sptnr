/**
 * Folder Groups Integration with MusicBrainz Releases
 * 
 * Displays MusicBrainz releases as integrated "green" folders in the folder groups section.
 * Shows progress and discovered files as they arrive.
 */

async function loadFolderGroupsWithMusicBrainz() {
  try {
    const section = document.getElementById('folderGroupsSection');
    // Only run on pages that have the folder groups section
    if (!section) return;

    const response = await fetch('/api/downloads/folder-groups');
    const data = await response.json();

    // Don't hide the section when there are no MusicBrainz-managed folders —
    // the Download Queue renderers (monitor.js loadFolderGroups /
    // downloads.js renderQueueSection) own the section and show real queue
    // items (or their own empty state). Hiding it here raced with those
    // renderers and made the queue flash then disappear.
    if (!data.success || data.count === 0) {
      return;
    }
    
    section.style.display = 'block';
    const badge = document.getElementById('folderGroupsBadge');
    if (badge) badge.textContent = data.count;
    
    const html = data.folder_groups.map((group) => {
      const isMusicBrainz = group.type === 'musicbrainz';
      const badgeColor = isMusicBrainz ? 'bg-success' : 'bg-secondary';
      const bgColor = isMusicBrainz ? 'background-color: #f0fff4;' : '';
      const icon = isMusicBrainz ? 'bi-disc' : 'bi-folder';
      const progressBarColor = isMusicBrainz ? 'bg-success' : 'bg-info';
      
      // Show actual files if discovered, or waiting message
      let filesDisplay = '';
      if (group.files && group.files.length > 0) {
        // Show up to 3 files discovered
        filesDisplay = group.files.slice(0, 3).map(f => 
          `<small class="text-success d-block"><i class="bi bi-file-earmark-music"></i> ${f.name}</small>`
        ).join('');
        
        if (group.files.length > 3) {
          filesDisplay += `<small class="text-muted d-block"><em>... and ${group.files.length - 3} more files</em></small>`;
        }
      } else {
        filesDisplay = `<small class="text-muted"><i class="bi bi-hourglass-split"></i> Waiting for ${group.total_tracks} tracks...</small>`;
      }
      
      // Status label
      let statusLabel = '';
      if (group.discovered_count >= group.total_tracks) {
        statusLabel = '<span class="badge bg-success ms-2">Ready to Finalize</span>';
      } else if (group.discovered_count > 0) {
        statusLabel = '<span class="badge bg-info ms-2">In Progress</span>';
      } else {
        statusLabel = '<span class="badge bg-warning ms-2">Waiting</span>';
      }
      
      return `
        <div class="list-group-item" style="${bgColor}border-left: 4px solid ${isMusicBrainz ? '#28a745' : '#6c757d'};">
          <div class="d-flex justify-content-between align-items-start">
            <div style="flex: 1;">
              <!-- Header: Title + Badge -->
              <div class="d-flex align-items-center gap-2 mb-2">
                <i class="bi ${icon}" style="font-size: 1.2rem;"></i>
                <h6 class="mb-0">${group.display_name}</h6>
                <span class="badge ${badgeColor}" style="font-size: 0.75rem;">
                  ${isMusicBrainz ? 'Release' : 'Folder'}
                </span>
                ${statusLabel}
              </div>
              
              <!-- Progress Bar -->
              <div class="progress mb-2" style="height: 20px;">
                <div class="progress-bar ${progressBarColor}" 
                     style="width: ${group.progress_percent}%">
                  <small style="color: white; font-weight: bold;">${group.progress_percent}%</small>
                </div>
              </div>
              
              <!-- Files Display -->
              <div style="font-size: 0.9rem; margin: 0.5rem 0;">
                ${filesDisplay}
              </div>
              
              <!-- Stats -->
              <small class="text-muted d-block mt-2">
                <i class="bi bi-file-earmark-music"></i> ${group.discovered_count} of ${group.total_tracks} tracks discovered
                ${group.metadata ? `<span class="ms-2">•</span> <i class="bi bi-calendar"></i> ${group.metadata.year}` : ''}
              </small>
            </div>
            
            <!-- Action Buttons -->
            <div class="btn-group btn-group-sm ms-2" role="group">
              <button class="btn btn-outline-info" 
                      onclick="viewFolderContents('${group.name}')" 
                      title="View folder contents"
                      data-bs-toggle="tooltip">
                <i class="bi bi-folder-open"></i>
              </button>
              ${isMusicBrainz ? `
                <button class="btn btn-outline-warning" 
                        onclick="retryMatchingForRelease('${group.release_id}')" 
                        title="Retry file matching"
                        data-bs-toggle="tooltip">
                  <i class="bi bi-arrow-repeat"></i>
                </button>
              ` : ''}
              <button class="btn btn-outline-danger" 
                      onclick="cancelFolderDownloads('${group.name}')" 
                      title="Cancel this folder"
                      data-bs-toggle="tooltip">
                <i class="bi bi-x"></i>
              </button>
            </div>
          </div>
        </div>
      `;
    }).join('');
    
    const list = document.getElementById('folderGroupsList');
    if (list) {
      list.innerHTML = `<div class="list-group list-group-flush">${html}</div>`;
    }
    
    // Initialize tooltips
    const tooltipElements = document.querySelectorAll('[data-bs-toggle="tooltip"]');
    tooltipElements.forEach(el => new bootstrap.Tooltip(el));
    
  } catch (error) {
    console.error('Error loading folder groups:', error);
  }
}


async function viewFolderContents(folderPath) {
  /**
   * Show detailed view of folder contents
   */
  try {
    // Extract folder name from path
    const folderName = folderPath.split('/').pop();
    
    const response = await fetch(`/api/downloads/folder/${folderName}`);
    const data = await response.json();
    
    if (!data.success) {
      alert('❌ Error: ' + (data.error || 'Could not load folder contents'));
      return;
    }
    
    // Create modal with file listing
    const modal = document.createElement('div');
    modal.className = 'modal fade';
    modal.id = 'folderModalView';
    modal.tabindex = '-1';
    modal.innerHTML = `
      <div class="modal-dialog modal-lg">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">
              <i class="bi bi-folder-open"></i> ${data.name}
            </h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <p class="text-muted small">
              <i class="bi bi-file-earmark-music"></i> ${data.audio_files} audio files
              <span class="ms-2">•</span>
              <i class="bi bi-file"></i> ${data.file_count} total files
            </p>
            
            <div style="max-height: 400px; overflow-y: auto;">
              <div class="list-group">
                ${data.files.map(f => `
                  <div class="list-group-item ${f.is_audio ? 'list-group-item-success' : ''}">
                    <div class="d-flex justify-content-between">
                      <div>
                        <h6 class="mb-1">
                          <i class="bi ${f.is_audio ? 'bi-file-earmark-music' : 'bi-file-earmark'}"></i>
                          ${f.name}
                        </h6>
                        <small class="text-muted">
                          ${formatFileSize(f.size)} • 
                          ${new Date(f.modified).toLocaleString()}
                        </small>
                      </div>
                      ${f.is_audio ? `
                        <span class="badge bg-success">Audio</span>
                      ` : `
                        <span class="badge bg-secondary">${f.extension}</span>
                      `}
                    </div>
                  </div>
                `).join('')}
              </div>
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
          </div>
        </div>
      </div>
    `;
    
    document.body.appendChild(modal);
    const bsModal = new bootstrap.Modal(modal);
    bsModal.show();
    
    // Cleanup
    modal.addEventListener('hidden.bs.modal', () => modal.remove());
    
  } catch (error) {
    console.error('Error viewing folder:', error);
    alert('❌ Error viewing folder contents');
  }
}


async function retryMatchingForRelease(releaseId) {
  /**
   * Retry file matching for a MusicBrainz release
   */
  try {
    if (!confirm('Retry file matching for this release?')) {
      return;
    }
    
    const response = await fetch(`/api/musicbrainz/release/${releaseId}/retry-match`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    
    const data = await response.json();
    
    if (data.success) {
      alert(`✅ Retry initiated for ${data.unmatched_tracks.length} unmatched tracks`);
      
      // Refresh folder groups after a delay
      setTimeout(() => loadFolderGroupsWithMusicBrainz(), 1000);
    } else {
      alert('❌ Error: ' + (data.error || 'Retry failed'));
    }
    
  } catch (error) {
    console.error('Error retrying match:', error);
    alert('❌ Network error');
  }
}


async function cancelFolderDownloads(folderPath) {
  /**
   * Cancel all downloads for a folder
   */
  try {
    if (!confirm('Are you sure you want to cancel this folder? This will remove it from the queue.')) {
      return;
    }
    
    // Extract folder name from path
    const folderName = folderPath.split('/').pop();
    
    const response = await fetch(`/api/downloads/folder/${folderName}/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    
    const data = await response.json();
    
    if (data.success) {
      alert('✅ Folder cancelled and removed from queue');
      
      // Refresh folder groups
      setTimeout(() => loadFolderGroupsWithMusicBrainz(), 500);
    } else {
      alert('❌ Error: ' + (data.error || 'Cancellation failed'));
    }
    
  } catch (error) {
    console.error('Error cancelling folder:', error);
    alert('❌ Network error');
  }
}


function formatFileSize(bytes) {
  /**
   * Format file size for display
   */
  if (bytes === 0) return '0 B';
  
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}


// Auto-refresh folder groups every 5 seconds when page is visible
let folderGroupRefreshInterval = null;

function startFolderGroupRefresh() {
  if (folderGroupRefreshInterval) return;
  
  folderGroupRefreshInterval = setInterval(() => {
    if (document.hidden) return;  // Don't refresh if tab is not visible
    loadFolderGroupsWithMusicBrainz();
  }, 5000);
}

function stopFolderGroupRefresh() {
  if (folderGroupRefreshInterval) {
    clearInterval(folderGroupRefreshInterval);
    folderGroupRefreshInterval = null;
  }
}

// Start refresh on page load
document.addEventListener('DOMContentLoaded', () => {
  loadFolderGroupsWithMusicBrainz();
  startFolderGroupRefresh();
});

// Stop/start based on visibility
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopFolderGroupRefresh();
  } else {
    loadFolderGroupsWithMusicBrainz();
    startFolderGroupRefresh();
  }
});


function setFolderGroupFilter(filterType) {
  /**
   * Filter folder groups by type (all, release, folder)
   */
  const items = document.querySelectorAll('#folderGroupsList .list-group-item');
  
  // Update button states
  document.getElementById('folderFilterAll').classList.toggle('active', filterType === 'all');
  document.getElementById('folderFilterRelease').classList.toggle('active', filterType === 'release');
  document.getElementById('folderFilterFolder').classList.toggle('active', filterType === 'folder');
  
  // Filter items
  items.forEach(item => {
    let show = false;
    
    if (filterType === 'all') {
      show = true;
    } else if (filterType === 'release') {
      show = item.style.borderLeftColor === 'rgb(40, 167, 69)';  // Green for releases
    } else if (filterType === 'folder') {
      show = item.style.borderLeftColor === 'rgb(108, 117, 125)';  // Gray for folders
    }
    
    item.style.display = show ? '' : 'none';
  });
}
