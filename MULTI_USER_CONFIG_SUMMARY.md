# Multi-User Config UI - Quick Summary

## What Changed

### Before
❌ Single "Navidrome" section with:
- Base URL
- Username  
- Password
- No per-user Spotify or ListenBrainz options

### After
✅ New "Music Users" section with:
- **Add Another User** button at top
- Multiple user cards, each with:
  - Navidrome credentials
  - Spotify API credentials
  - ListenBrainz User Token (NEW!)
  - Display Name for easy identification
  - Remove button (if more than 1 user)

## Visual Layout

```
┌─────────────────────────────────────────────────────────┐
│ Configuration                                           │
│ [Setup Wizard] [Raw YAML]                              │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ 🎵 MUSIC USERS                                          │
│ Configure Navidrome, Spotify, and ListenBrainz...  │
│                                  [+ Add Another User]   │
│                                                         │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ User 1: John's Account                      [Remove] │ │
│ │                                                     │ │
│ │ 🎵 Navidrome                                       │ │
│ │   Display Name: John's Account                    │ │
│ │   Base URL: http://localhost:4533                │ │
│ │   Username: john                                 │ │
│ │   Password: ••••••••                              │ │
│ │                                                     │ │
│ │ 🟢 Spotify API                                    │ │
│ │   Client ID: [     spotify id     ]               │ │
│ │   Client Secret: [  spotify secret ]               │ │
│ │                                                     │ │
│ │ 🔵 ListenBrainz API                               │ │
│ │   User Token: [ listenbrainz token ]               │ │
│ │   Get token from: https://listenbrainz.org...    │ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ User 2: Jane's Account                      [Remove] │ │
│ │ [Same fields as above...]                          │ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│                                [Save] [Cancel]         │
└─────────────────────────────────────────────────────────┘
```

## How to Use

### Add a New User
1. Click **"+ Add Another User"** button
2. Fill in the user details
3. Click **Save** at bottom

### Edit a User
1. Find the user card
2. Change any field
3. Click **Save**

### Remove a User
1. Click **[Remove]** button in user card header
2. Click **Save** to confirm

### Find Your ListenBrainz Token
1. Go to https://listenbrainz.org/settings/profile/
2. Copy "User Token" (NOT API token)
3. Paste into "ListenBrainz API" → "User Token" field

## What Gets Saved

When you click Save, the system creates a `navidrome_users` array in your config.yaml:

```yaml
navidrome_users:
  - username: john
    display_name: John's Account
    navidrome_base_url: http://localhost:4533
    navidrome_password: secret123
    spotify_client_id: my-spotify-id
    spotify_client_secret: my-spotify-secret
    listenbrainz_user_token: my-token
  
  - username: jane
    display_name: Jane's Account
    navidrome_base_url: http://localhost:4533
    navidrome_password: jane_secret
    spotify_client_id: jane-spotify-id
    spotify_client_secret: jane-spotify-secret
    listenbrainz_user_token: jane-token
```

## Features Now Available

With proper credentials configured per user:

| Feature | Needs | Per-User? |
|---------|-------|-----------|
| Navidrome Integration | URL + Username + Password | ✅ Yes |
| Music Library Sync | Navidrome | ✅ Yes |
| Track Popularity | Spotify API | ✅ Yes |
| Artist Info | Spotify API | ✅ Yes |
| Love/Hate Tracking | ListenBrainz Token | ✅ Yes (NEW!) |
| Genre Tags | ListenBrainz Token | ✅ Yes (NEW!) |
| Single Detection | ListenBrainz Token | ✅ Yes (NEW!) |

## Common Tasks

### Set Up First User
```
1. Fill in Display Name (optional)
2. Enter Navidrome Base URL (required)
3. Enter Navidrome Username (required)
4. Enter Navidrome Password (required)
5. (Optional) Add Spotify Client ID and Secret
6. (Optional) Add ListenBrainz User Token
7. Click Save
```

### Add Second User
```
1. Click "Add Another User"
2. Repeat the First User setup
3. Click Save
```

### Add Spotify to Existing User
```
1. Find user card
2. Scroll to "Spotify API" section
3. Enter Client ID and Secret
4. Click Save
```

### Add ListenBrainz to Existing User
```
1. Find user card
2. Scroll to "ListenBrainz API" section
3. Enter User Token (from https://listenbrainz.org/settings/profile/)
4. Click Save
```

## Minimum Required Fields

To save a user configuration:
- **username** (Navidrome login)
- **navidrome_base_url** (Navidrome server URL)

Everything else is optional but recommended.

## Backward Compatibility

If you have an old config with single `navidrome` section:
- Still works on load ✅
- Saves as `navidrome_users` array ✅
- Can migrate using UI ✅
- No data loss ✅

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Config won't save | Check all required fields filled |
| ListenBrainz token rejected | Verify it's the "User Token" not API token |
| Spotify API not working | Check Client ID/Secret aren't expired |
| User removed by mistake | Click "Add Another User" to restore |
| Can't see users on load | Check config.yaml has valid YAML syntax |

## Files Modified

- `templates/config.html` - Updated UI with multi-user section
- `app.py` - Updated config_save_json() to handle navidrome_users array
- `MULTI_USER_CONFIG_GUIDE.md` - Comprehensive documentation

## Next Steps

1. **Test the new UI** at https://your-domain/config
2. **Add your users** with Navidrome credentials
3. **Configure Spotify API** (optional but recommended)
4. **Add ListenBrainz tokens** (optional but enables love tracking)
5. **Save and verify** the config was applied

---

For detailed documentation, see [MULTI_USER_CONFIG_GUIDE.md](MULTI_USER_CONFIG_GUIDE.md)
