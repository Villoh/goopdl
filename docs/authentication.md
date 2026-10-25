# Authentication

**English** · [Español](es/authentication.md)

goopdl talks to Google Play as an Android device, so every request needs a
token. You have two ways to get one:

| Mode | You need | Best for |
| ---- | -------- | -------- |
| [Anonymous](#anonymous-default) (default) | Nothing | Most downloads |
| [Google account](#google-account) | A throwaway account and its AAS token | Apps the anonymous accounts can't reach, or when the dispenser rate-limits you |

## Anonymous (default)

By default goopdl asks [Aurora Store's](https://auroraoss.com/) public token
dispenser (`https://auroraoss.com/api/auth`) for an anonymous token. You don't
need a Google account.

```bash
goopdl auth                    # arm64 token
goopdl auth --arch armv7       # token for older 32-bit devices
goopdl auth --country IN       # register the device with India's MCC/MNC
goopdl auth --clear            # delete every cached token
```

You don't have to run `auth` first. Other commands fetch a token when they need
one. See [`auth`](commands.md#auth) for every flag.

### Where tokens live

| File | Contents |
| ---- | -------- |
| `~/.config/goopdl/auth-{arch}[-{country}][-profile-{key}].json` | Token from the last `auth` run |
| `~/.config/goopdl/token-pool-{arch}[-{country}][-profile-{key}].json` | Pool of up to 5 tokens used by `download`, `latest`, and `fast-download` |

The `-profile-{key}` part appears when you pin a device with `--profile`, so
each profile keeps its own tokens. `goopdl auth --clear` deletes all of them.

Tokens expire after 50 minutes. [How it works](how-it-works.md#token-pool)
explains the pool.

### Custom dispenser

Point any command at a self-hosted dispenser with `--dispenser` / `-d`:

```bash
goopdl auth -d https://my-dispenser.example.com/api/auth
goopdl download com.whatsapp -d https://my-dispenser.example.com/api/auth
```

## Google account

Set both variables and goopdl skips the dispenser and signs in to Google
directly:

```bash
export GOOPDL_ACCOUNT_EMAIL="account@example.com"
export GOOPDL_AAS_TOKEN="aas_et/..."
goopdl download com.whatsapp
```

- The AAS token must start with `aas_et/`.
- If you set only one variable, goopdl exits with an error. It does not fall
  back to anonymous mode.
- goopdl reads no config files, secret files, or CLI flags for this mode.
- Short-lived Google Play bearer tokens stay in memory and are never cached.

> [!WARNING]
> An AAS token is a long-lived credential with full Play Store access to the
> account. Use a throwaway Google account, never your main one. Changing the
> account password revokes the token.

### Getting an AAS token

Pick whichever is easiest:

- **[goopdl-auth](https://github.com/Villoh/goopdl-auth)**: a small Android
  app. Sign in on the phone and it shows the AAS token. It stores no
  credentials or tokens.
- **`goopdl aastoken --browser`** (recommended on desktop). See below.
- **`goopdl aastoken --oauth`**, if browser automation doesn't work.
- **`goopdl aastoken`** with a password. Google usually rejects this.

Each mode prints the token to the console. goopdl never creates
`credentials.txt` or `token.txt` files.

#### Browser OAuth (recommended)

```bash
goopdl aastoken --browser
```

goopdl opens Chrome, Edge, Chromium, or Brave with a fresh temporary profile.
Sign in and select **I agree**. goopdl captures the email and the one-time
`oauth_token`, closes the browser, deletes the profile, and exchanges the
`oauth_token` for an AAS token.

If goopdl can't find your browser, set `GOOPDL_BROWSER` to its executable path.

#### Manual OAuth

```bash
goopdl aastoken --oauth
```

1. Open [Google EmbeddedSetup](https://accounts.google.com/EmbeddedSetup).
2. Sign in and select **I agree**.
3. Open developer tools (`F12`).
4. In Chrome or Edge, go to **Application → Cookies →
   `https://accounts.google.com`**.
5. Copy the `oauth_token` cookie. It starts with `oauth2_4/`.
6. Run `goopdl aastoken --oauth`, enter the same email, and paste the token.

The `oauth_token` works once and expires quickly, so paste it right away.

#### Password or app password

```bash
goopdl aastoken                               # prompts for email, hides the password
goopdl aastoken user@example.com password     # password ends up in shell history
```

Google usually rejects both account passwords and 16-character app passwords
with `BadAuthentication`. Keep 2-Step Verification on and use an OAuth mode
instead.
