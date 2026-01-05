# Multi-User Configuration Guide

## Overview

The config page has been updated to support multiple Navidrome users, each with their own Spotify API and ListenBrainz credentials. This enables per-user music discovery and tracking features.

## Features

### 1. **Music Users Section**
Located at the top of the config page, replacing the old single Navidrome configuration.

#### Button: "Add Another User"
- Click to add a new user configuration
- Each user gets its own collapsible card with all settings

### 2. **Per-User Configuration**

Each user card contains three sections:

#### **Navidrome** (Blue header)
- **Display Name**: Friendly name for this user (e.g., "John's Account")
- **Navidrome Base URL**: URL to the Navidrome server (e.g., `http://localhost:4533`)
- **Username**: Navidrome login username
- **Password**: Navidrome login password

#### **Spotify API** (Green header)
- **Client ID**: Spotify application Client ID
- **Client Secret**: Spotify application Client Secret
- [Get credentials here](https://developer.spotify.com/dashboard/applications)

#### **ListenBrainz API** (Blue info header - NEW)
- **User Token**: Your ListenBrainz user API token
- Required for love/hate tracking and genre tags
- [Get your token here](https://listenbrainz.org/settings/profile/)

### 3. **User Management**

**Remove Button** (red X button in user header)
- Deletes the entire user configuration
- Disabled when only one user exists (at least one user is required)

**Display Name Title**
- Updates in real-time as you type
- Shows current user: "User 1: John's Account"
- Helps identify users at a glance

## Configuration Examples

### Single User Setup
```yaml
navidrome_users:
  - username: admin
    display_name: Admin User
    navidrome_base_url: http://localhost:4533
    navidrome_password: password123
    spotify_client_id: your_spotify_id
    spotify_client_secret: your_spotify_secret
    listenbrainz_user_token: your_listenbrainz_token
```

### Multi-User Setup (2 users)
```yaml
navidrome_users:
  - username: john
    display_name: Johns Account
    navidrome_base_url: http://localhost:4533
    navidrome_password: john_password
    spotify_client_id: johns_spotify_id
    spotify_client_secret: johns_spotify_secret
    listenbrainz_user_token: johns_listenbrainz_token
  
  - username: jane
    display_name: Janes Account
    navidrome_base_url: http://localhost:4533
    navidrome_password: jane_password
    spotify_client_id: janes_spotify_id
    spotify_client_secret: janes_spotify_secret
    listenbrainz_user_token: janes_listenbrainz_token
```

## How to Add a User

1. **Click "Add Another User"** button at the top
2. **Fill in the user information**:
   - Display Name (optional, defaults to "New User")
   - Navidrome Base URL
   - Navidrome Username
   - Navidrome Password
3. **Add API credentials** (optional):
   - Spotify Client ID and Secret
   - ListenBrainz User Token
4. **Save Configuration** using the Save button at the bottom

## How to Remove a User

1. **Scroll to the user card** you want to remove
2. **Click the "Remove" button** (red X) in the user header
3. **Confirm** the deletion by saving configuration
4. If this is the last user, add a new one before saving

## How to Update an Existing User

1. **Find the user card** by Display Name
2. **Edit any field** directly in the form
3. **Click Save** to persist changes

## ListenBrainz Integration

The ListenBrainz API field is **NEW** and enables:

### Features Enabled
- ✅ Love/Hate track tracking (user-specific)
- ✅ Genre tag fetching from public ListenBrainz database
- ✅ Artist genre recommendations
- ✅ Integration with single track detection

### Getting Your Token
1. Go to [ListenBrainz Settings](https://listenbrainz.org/settings/profile/)
2. Scroll to "API Tokens" section
3. Copy your "User Token" (NOT the API token)
4. Paste into the ListenBrainz User Token field

## Spotify API Configuration

### Getting Your Credentials
1. Visit [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/applications)
2. Create a new application
3. Copy **Client ID** and **Client Secret**
4. Accept the terms and create

### What It's Used For
- Album metadata and release dates
- Artist information and popularity scores
- Track popularity metrics

## Backward Compatibility

If you have an old single `navidrome` config section:
```yaml
navidrome:
  base_url: http://localhost:4533
  user: admin
  pass: password123
```

The system will:
1. Still read it on load
2. BUT save new config as `navidrome_users` array
3. Legacy section will be preserved if no users are configured

**Recommendation**: Use the multi-user UI to migrate your settings.

## Troubleshooting

### Missing Users on Page Load
- Ensure `navidrome_users` array exists in config.yaml
- Check YAML syntax is valid
- Try the "Raw YAML" editor to verify structure

### ListenBrainz Token Not Working
- Verify you copied the "User Token" NOT the "API token"
- Ensure token hasn't expired (some tokens require periodic renewal)
- Check ListenBrainz website to confirm account is active

### Spotify Credentials Rejected
- Double-check Client ID and Secret (no spaces at ends)
- Verify application hasn't been deleted from Spotify Dashboard
- Ensure credentials match the right application

### Config Won't Save
- Check browser console for JavaScript errors
- Verify all required fields have values (at least username and URL)
- Try "Raw YAML" editor for direct editing

## Technical Details

### Storage Format
Multi-user config is stored as YAML array in `config.yaml`:

```yaml
navidrome_users:
  - username: user1
    display_name: User One
    navidrome_base_url: http://...
    navidrome_password: ...
    spotify_client_id: ...
    spotify_client_secret: ...
    listenbrainz_user_token: ...
  - username: user2
    ...
```

### API Endpoints
- **Config Save**: `POST /config/save-json` (sends JSON, saves as YAML)
- **Config View**: `GET /config` (renders template with config data)

### Required Fields
Minimum configuration to save a user:
- `username` (Navidrome username)
- `navidrome_base_url` (Navidrome server URL)

Optional fields will be saved as empty strings if not provided.

## Security Notes

⚠️ **Important**: 
- Credentials are stored in plain text in config.yaml
- Protect your config file with proper file permissions
- Use environment variables for sensitive data in production
- Never commit credentials to version control

## Support

For issues with the multi-user config:
1. Check the Raw YAML editor for syntax errors
2. Verify all credentials are correct
3. Review browser console for JavaScript errors
4. Check `/config/webui.log` for backend errors
