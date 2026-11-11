# Google sign-in walkthrough: create the client ID and secret

This guide gives each step to make Google sign-in work for one app. It covers the Google Cloud Console, the redirect URIs, and where to put the client ID and the secret. Do it once for each app.

For the design of the sign-in code, see [google-auth-setup-guide.md](google-auth-setup-guide.md). For the words in this guide, see the [glossary](beginner-guide.md#glossary).

## What you make

You register your app with Google. Google gives you two values:

- The **client ID**. It names your app at Google. It is not secret.
- The **client secret**. Only your app's backend knows it. Never put it in Git, in the frontend, or in a chat.

You also tell Google one or more **redirect URIs**. A redirect URI is the address of your app that Google sends the browser back to after sign-in. For every app from `agentic-core` it ends in `/auth/callback`.

![How Google sign-in works: the browser asks the app backend to sign in, the backend sends the browser to Google, Google sends it back with a code, the backend trades the code for an ID token, checks the token and the email allowlist, and sets a session cookie.](diagrams/google-sign-in-sequence.svg)

The browser never holds a Google token. The backend checks the ID token, keeps only the user ID and the email in a signed session cookie, and drops the Google tokens.

## The order of the steps

![Set up a Google OAuth client: apply the Render Blueprint and copy the service URL, then in Google Cloud Console create a project, set branding and audience, create a Web application client with the redirect URI, copy the ID and secret, and paste them into Render; a second client is for local work.](diagrams/google-console-setup.svg)

Google needs the exact address of your app. Render gives the address only after you apply the Blueprint. So the order is:

1. Apply the Render Blueprint. Leave the Google settings empty. The app starts and shows "Sign-in is not configured".
2. Copy the service URL from the Render dashboard, for example `https://my-app.onrender.com`.
3. Do the Google steps below with that URL.
4. Put the values into Render.

For work on your computer only, skip steps 1 and 2. Use `http://localhost:8000` as the address.

## Before you start

- A Google account. A personal Gmail account works.
- The app's address, for example `https://my-app.onrender.com`, or `http://localhost:8000` on your computer.
- The list of email addresses that may sign in.

## Step 1: Create a Google Cloud project

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Click the project picker at the top of the page.
3. Click **New project**.
4. Type a project name, for example `my-app`. Click **Create**.
5. Make sure that the project picker shows the new project.

Use one project for each app. Then the clients, secrets, and settings of two apps never mix.

## Step 2: Set the branding

1. Open the menu, then **Google Auth Platform** > **Branding**. On an older console, the page is **APIs & Services** > **OAuth consent screen**. If Google asks, click **Get started**.
2. Type the **App name**. People see this name on the Google sign-in page.
3. Select the **User support email**.
4. Type your email under **Developer contact information**.
5. Do not upload a logo. A logo can make Google ask for a brand verification review before the app can go live.
6. Click **Save** (or **Create**).

## Step 3: Choose the audience

1. Open **Google Auth Platform** > **Audience**.
2. Set **User type** to **External**. Select **Internal** only if every user is in your own Google Workspace organization.
3. Choose the publishing status:
   - **In production**: click **Publish app**. Any Google account can reach the sign-in page. Your app still lets in only the emails in `AUTH_ALLOWED_EMAILS`. The app asks only for `openid` and `email`, so Google does not require verification.
   - **Testing**: keep this status and add each person under **Test users**. Google blocks every other account with an "access blocked" page.

In both cases, `AUTH_ALLOWED_EMAILS` decides who gets in. **Testing** adds a second list at Google, which you must keep the same as `AUTH_ALLOWED_EMAILS`.

You do not need to add scopes on the **Data access** page. The app asks for `openid email` when it starts sign-in.

## Step 4: Create the OAuth client

1. Open **Google Auth Platform** > **Clients**.
2. Click **Create client**.
3. Set **Application type** to **Web application**.
4. Type a name, for example `my-app production`.
5. Leave **Authorized JavaScript origins** empty. The app sends no request to Google from browser JavaScript.
6. Under **Authorized redirect URIs**, click **Add URI** and type the redirect URI from the table below.
7. Click **Create**.

| Where the app runs | Redirect URI | Also set |
| --- | --- | --- |
| Render | `https://my-app.onrender.com/auth/callback` | Nothing. The app reads `RENDER_EXTERNAL_URL`. |
| Render with a custom domain | `https://app.example.com/auth/callback` | `APP_BASE_URL=https://app.example.com` on Render |
| Your computer, backend only | `http://localhost:8000/auth/callback` | `APP_BASE_URL=http://localhost:8000` in `.env` |
| Your computer, with the Vite dev server | `http://localhost:5173/auth/callback` | `APP_BASE_URL=http://localhost:5173` in `.env` |
| Railway | `https://<public URL>/auth/callback` | `APP_BASE_URL=https://<public URL>` on Railway |

The redirect URI must match exactly: `http` or `https`, the host, the port, and the path. A missing port or an extra `/` at the end gives the Google error `redirect_uri_mismatch`. Google can take a few minutes to apply a change.

Make **two clients**: one for the deployed app and one for your computer. Then the production secret stays on Render only.

## Step 5: Copy the client ID and the secret

Google shows the client ID and the client secret after you click **Create**.

1. Copy the **Client ID**. It ends in `.apps.googleusercontent.com`.
2. Copy the **Client secret**. Google shows the full secret only once. If you lose it, open the client, add a new secret, and disable the old one.
3. Keep both in a password manager until you finish the next step.

Type or paste each value yourself. Do not paste the secret into a chat with an agent.

## Step 6: Put the values in the app

### On Render

1. Open the Render dashboard, then the web service, then **Environment**.
2. Set these values:
   - `GOOGLE_CLIENT_ID`: the client ID.
   - `GOOGLE_CLIENT_SECRET`: the client secret.
   - `AUTH_ALLOWED_EMAILS`: the emails that may sign in, separated by commas, for example `me@gmail.com,friend@gmail.com`.
3. Click **Save**. Render restarts the service.

Render made `SESSION_SECRET` when you applied the Blueprint. Do not change it unless you want to sign everybody out.

### On your computer

Edit `.env` in the app folder:

```bash
GOOGLE_CLIENT_ID=<the local client ID>
GOOGLE_CLIENT_SECRET=<the local client secret>
SESSION_SECRET=<at least 32 random characters>
AUTH_ALLOWED_EMAILS=me@gmail.com
APP_BASE_URL=http://localhost:5173
```

Make a random `SESSION_SECRET` with this command:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Restart the backend after you change `.env`.

## Step 7: Test the sign-in

1. Open the app at the exact `APP_BASE_URL` address. On Render, open the service URL.
2. Click **Sign in with Google**. Select an account in `AUTH_ALLOWED_EMAILS`.
3. Open `/auth/session`, for example `https://my-app.onrender.com/auth/session`. It shows your `user_id`, for example `google:1234567890`. You need this value to make an [agent key](google-auth-setup-guide.md#machine-access-agent-keys).

## When sign-in fails

| What you see | Cause | Fix |
| --- | --- | --- |
| "Sign-in is not configured" or 503 `auth_not_configured` | A required setting is missing or invalid. | Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `AUTH_ALLOWED_EMAILS`, and `SESSION_SECRET` (32+ characters). On your computer, also set `APP_BASE_URL`. |
| Google error `redirect_uri_mismatch` | The redirect URI at Google is not exactly `<APP_BASE_URL>/auth/callback`. | Fix the URI in the Google client. Check `http`/`https`, the port, and the end of the path. |
| Google error `invalid_client` | The client ID or secret is wrong, or the secret is disabled. | Copy both values again from the same client. |
| Google page "access blocked" or "has not completed the Google verification process" | The audience is **Testing** and this account is not a test user. | Add the test user, or publish the app. |
| Login page: "not on the list" | The email is not in `AUTH_ALLOWED_EMAILS`. | Add the email, then restart the service. |
| Login page: email not verified | Google has not verified the account's email. | Verify the email in the Google account. |
| Login page: "Sign-in did not finish" | The sign-in started on one address and ended on another, or a check failed. | Open the app at the exact `APP_BASE_URL` address, for example `localhost`, not `127.0.0.1`. Try again. |
| 400 `base_url_host_mismatch` | A proxy changes the host name, so it never matches `APP_BASE_URL`. | Set `APP_BASE_URL` to the address that people open. |

The generated app's `README.md` has the full table of failure responses, including the agent key errors.

## Checklist

- [ ] One Google Cloud project for this app.
- [ ] Branding: app name and support email. No logo.
- [ ] Audience: External. Published, or each person is a test user.
- [ ] A **Web application** client for production, with `<service URL>/auth/callback`.
- [ ] A second client for your computer, with `http://localhost:8000/auth/callback` or `http://localhost:5173/auth/callback`.
- [ ] `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `AUTH_ALLOWED_EMAILS` set on Render.
- [ ] The same settings, plus `SESSION_SECRET` and `APP_BASE_URL`, set in the local `.env`.
- [ ] Sign-in works, and `/auth/session` shows your `user_id`.

## Keep it safe

- Use one Google client for each app. A shared client lets one app's ID tokens pass another app's audience check.
- To rotate the secret: add a new secret in the Google client, set it on Render, then disable the old secret.
- To remove a person: remove the email from `AUTH_ALLOWED_EMAILS`, and remove every `AGENT_CREDENTIALS` entry for that person. The allowlist does not apply to agent keys.
