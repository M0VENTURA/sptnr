# Authentik Authentication Setup Guide

This guide explains how to configure Authentik SSO/OAuth authentication for Sptnr.

## Prerequisites

- A running Authentik instance (self-hosted or cloud)
- Admin access to your Authentik instance
- Sptnr running on a publicly accessible URL (or localhost for testing)

## Step 1: Create an OAuth2/OIDC Application in Authentik

1. Log in to your Authentik admin panel
2. Navigate to **Applications** → **Applications**
3. Click **Create** to create a new application
4. Fill in the application details:
   - **Name**: `Sptnr` (or any name you prefer)
   - **Slug**: `sptnr` (used in the OAuth URLs)
   - **Provider**: Create a new provider (see next step)

## Step 2: Create an OAuth2/OIDC Provider

1. When creating the application, click **Create Provider**
2. Select **OAuth2/OIDC Provider**
3. Configure the provider:
   - **Name**: `Sptnr OAuth Provider`
   - **Authorization flow**: `default-provider-authorization-implicit-consent` (or your preferred flow)
   - **Client type**: `Confidential`
   - **Redirect URIs**: Add your Sptnr callback URL:
     - For local development: `http://localhost:5000/auth/callback`
     - For production: `https://your-sptnr-domain.com/auth/callback`
   - **Signing Key**: Select your default certificate
   - **Subject mode**: `Based on the User's username`
   - **Scopes**: Include at least:
     - `openid`
     - `email`
     - `profile`

4. Click **Finish** to create the provider
5. **Important**: Copy the **Client ID** and **Client Secret** - you'll need these for Sptnr configuration

## Step 3: Configure Sptnr

1. Open your `config.yaml` file
2. Find the `authentik` section and update it:

```yaml
authentik:
  enabled: true
  server_url: "https://auth.example.com"  # Your Authentik instance URL
  client_id: "your-client-id-from-step-2"
  client_secret: "your-client-secret-from-step-2"
  app_slug: "sptnr"  # Must match the slug you configured in Authentik (Step 1)
  username_field: "preferred_username"  # Which field to use for username (preferred_username, email, or sub)
```

### Configuration Options Explained:

- **enabled**: Set to `true` to enable Authentik authentication
- **server_url**: The base URL of your Authentik instance (without trailing slash)
- **client_id**: The Client ID from your OAuth2 provider (from Step 2)
- **client_secret**: The Client Secret from your OAuth2 provider (keep this secure!)
- **app_slug**: The application slug you configured in Authentik (must match exactly)
- **username_field**: Which user attribute to use as the username in Sptnr:
  - `preferred_username`: Use the Authentik username (recommended)
  - `email`: Use the user's email address
  - `sub`: Use the unique subject identifier (UUID)

3. Save the file and restart Sptnr

## Step 4: Test the Login

1. Navigate to your Sptnr login page
2. You should now see two options:
   - **Sign In** (traditional Navidrome authentication)
   - **Sign in with Authentik** (OAuth authentication)
3. Click **Sign in with Authentik**
4. You'll be redirected to your Authentik instance to authenticate
5. After successful authentication, you'll be redirected back to Sptnr and logged in

## Troubleshooting

### "Authentik authentication is not properly configured" error

- Verify that all required fields in `config.yaml` are filled in
- Check that `enabled` is set to `true`
- Ensure `server_url` doesn't have a trailing slash

### OAuth redirect errors

- Verify the redirect URI in Authentik matches exactly what Sptnr is using
- Check that the redirect URI includes the protocol (`http://` or `https://`)
- For production, ensure you're using HTTPS

### "Failed to get user information from Authentik" error

- Check that the Authentik provider includes the required scopes (`openid`, `email`, `profile`)
- Verify that your Authentik user has an email address set
- Check Authentik logs for any authorization errors

### Server metadata URL errors

- Ensure your Authentik instance is accessible from the Sptnr server
- The metadata URL should be: `https://your-authentik-domain/application/o/sptnr/.well-known/openid-configuration`
- Test this URL in a browser - it should return JSON configuration

## Security Notes

- Always use HTTPS in production
- Keep your `client_secret` secure and never commit it to version control
- Consider using environment variables for sensitive values
- Regularly rotate your OAuth credentials

## Mixed Authentication

You can keep both Navidrome and Authentik authentication enabled simultaneously. Users can choose their preferred login method:

- **Navidrome login**: For users with Navidrome credentials
- **Authentik login**: For users managed through your SSO system

The authentication method is stored in the session and doesn't affect functionality.

## Disabling Authentik

To disable Authentik authentication:

1. Set `enabled: false` in the `authentik` section of `config.yaml`
2. Restart Sptnr
3. The login page will only show the traditional Navidrome login option
