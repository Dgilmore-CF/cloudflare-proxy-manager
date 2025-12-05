# Cloudflare Proxy Manager

A Python script to manage Cloudflare proxy settings across multiple accounts. This tool allows you to:

- Scan all zones and disable proxies for all hostnames
- Save the original proxy state
- Restore proxies to their original state
- Support multiple Cloudflare accounts
- Generate detailed logs and reports
- Perform dry runs before making changes

## Prerequisites

- Python 3.7+
- Cloudflare API tokens with appropriate permissions
- Required Python packages (install via `pip install -r requirements.txt`)

## Setup

1. Clone this repository:
   ```bash
   git clone https://github.com/yourusername/cloudflare-proxy-manager.git
   cd cloudflare-proxy-manager
   ```

2. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Create a `.env` file in the project root with your Cloudflare API tokens:
   ```env
   # Format: CLOUDFLARE_API_TOKEN_ACCOUNTNAME=your_api_token_here
   # Format: CLOUDFLARE_ACCOUNT_ID_ACCOUNTNAME=your_account_id_here (optional but recommended)
   
   # Example for a production account
   CLOUDFLARE_API_TOKEN_PRODUCTION=abc123...
   CLOUDFLARE_ACCOUNT_ID_PRODUCTION=1234567890abcdef1234567890abcdef
   
   # Example for a staging account
   CLOUDFLARE_API_TOKEN_STAGING=def456...
   CLOUDFLARE_ACCOUNT_ID_STAGING=fedcba0987654321fedcba0987654321
   ```

   Replace `ACCOUNTNAME` with a descriptive name for each account (e.g., `PRODUCTION`, `STAGING`).
   
   **Important**: Including the account ID is highly recommended to ensure the script operates on the correct Cloudflare account, especially if your API token has access to multiple accounts. Without an account ID, the script will retrieve all zones accessible by the token.

## Usage

### Disable Proxies

To disable proxies for all hostnames across all accounts:

```bash
python cloudflare_proxy_manager.py disable
```

For a dry run (no changes will be made):

```bash
python cloudflare_proxy_manager.py disable --dry-run
```

### Restore Proxies

To restore proxies to their original state:

```bash
python cloudflare_proxy_manager.py restore
```

For a dry run (no changes will be made):

```bash
python cloudflare_proxy_manager.py restore --dry-run
```

### Check Status

To view the current proxy status:

```bash
python cloudflare_proxy_manager.py status
```

### Verify Account Configuration

To verify your account configuration and see which Cloudflare accounts are accessible:

```bash
python cloudflare_proxy_manager.py verify
```

This command will:
- Display the email associated with each API token
- Show all Cloudflare accounts accessible by each token
- Verify that configured account IDs are valid
- Warn if account IDs are not configured

**It's recommended to run this command first** to ensure your configuration is correct before making any changes.

## How Account Identification Works

The script uses two pieces of information to identify Cloudflare accounts:

1. **API Token** (Required): Used to authenticate with the Cloudflare API
2. **Account ID** (Optional but Recommended): Used to filter zones to a specific Cloudflare account

When an account ID is provided:
- The script will only operate on zones belonging to that specific account
- All API calls are filtered by the account ID
- Logs will include the account ID for better auditing

When no account ID is provided:
- The script will operate on ALL zones accessible by the API token
- This could include zones from multiple Cloudflare accounts if the token has access to them
- A warning will be logged

To find your Cloudflare Account ID:
1. Log in to the Cloudflare Dashboard
2. Select any website
3. Look in the right sidebar under "API" or scroll down to the "Account ID" section
4. Copy the Account ID (a 32-character hexadecimal string)

## Logging

Detailed logs are stored in the `logs/` directory in JSON format for easy parsing and analysis.

## Deployment to GitHub

1. Create a new repository on GitHub
2. Initialize git in your project directory:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   ```
3. Add the remote repository:
   ```bash
   git remote add origin https://github.com/yourusername/cloudflare-proxy-manager.git
   ```
4. Push the code:
   ```bash
   git push -u origin main
   ```
5. **Important**: Add `.env` and `proxy_state.json` to your `.gitignore` file to prevent committing sensitive information.

## Security Considerations

- Never commit your Cloudflare API tokens to version control
- The `proxy_state.json` file contains information about your DNS records; keep it secure
- Use the principle of least privilege when creating API tokens

## License

MIT
