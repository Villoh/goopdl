# Authentication

**English** · [Español](es/authentication.md)

goopdl talks to Google Play as an Android device, so every request needs a
token. You have two ways to get one:

| Mode | You need | Best for |
| ---- | -------- | -------- |
| [Anonymous](#anonymous-default) (default) | Nothing | Most downloads |
| [Google account](#google-account) | A throwaway account, signed in with `goopdl auth login` | Apps the anonymous accounts can't reach, or when the dispenser rate-limits you |

`goopdl auth status` shows which mode is active.

## Anonymous (default)

By default goopdl asks [Aurora Store's](https://auroraoss.com/) public token
dispenser (`https://auroraoss.com/api/auth`) for an anonymous token. You don't
need a Google account.

```bash
goopdl auth token                    # arm64 token
goopdl auth token --arch armv7       # token for older 32-bit devices
goopdl auth token --country IN       # register the device with India's MCC/MNC
goopdl auth logout                   # delete every cached token and device record
```

You don't have to run `auth token` first. Other commands fetch a token when
they need one. See [`auth token`](commands.md#auth-token) for every flag.

### Where tokens live

Tokens are cached in goopdl's config directory, the first of these that
applies:

1. `$GOOPDL_CONFIG_DIR`, if it is an absolute path. `~` is expanded.
2. `$XDG_CONFIG_HOME/goopdl`, if `XDG_CONFIG_HOME` is an absolute path.
3. `~/.config/goopdl`.

A relative path in either variable is ignored, so the cache never ends up in
the current directory. On Linux and macOS the directory is created with
permissions `0700` and token files are written `0600`.

> [!NOTE]
> Up to 1.4.x goopdl always used `~/.config/goopdl`. If `XDG_CONFIG_HOME` is
> set to somewhere else, goopdl now uses `$XDG_CONFIG_HOME/goopdl` and fetches
> new tokens the first time. To keep the old location, set
> `GOOPDL_CONFIG_DIR=~/.config/goopdl`.

| File in that directory | Contents |
| ---------------------- | -------- |
| `auth-{arch}[-{country}][-profile-{key}].json` | Token from the last `auth token` run |
| `token-pool-{arch}[-{country}][-profile-{key}].json` | Pool of up to 5 tokens used by `download`, `latest`, and `fast-download` |
| `direct-device-{account}-{arch}[-{country}]-profile-{key}.json` | [Google account](#what-account-mode-saves) mode only: the registered device, no token |
| `account.json` | The [saved Google account](#where-the-account-is-saved): email and AAS token |

The `-profile-{key}` part appears when you pin a device with `--profile`, so
each profile keeps its own tokens. `goopdl auth logout` deletes all of these
files. `goopdl auth token` prints the path it saved the token to, and
`goopdl auth status` shows the directory and how many files it holds.

Tokens expire after 50 minutes. [How it works](how-it-works.md#token-pool)
explains the pool.

### Custom dispenser

Point any command at a self-hosted dispenser with `--dispenser` / `-d`:

```bash
goopdl auth token -d https://my-dispenser.example.com/api/auth
goopdl download com.whatsapp -d https://my-dispenser.example.com/api/auth
```

## Google account

Sign in once and goopdl skips the dispenser and signs in to Google directly:

```bash
goopdl auth login              # opens a browser and saves the account
goopdl download com.whatsapp   # uses it
goopdl auth status             # shows the account in use, never the token
goopdl auth logout             # removes it and every cached token
```

You can also pass the account in two variables, for example in CI or a
container:

```bash
export GOOPDL_ACCOUNT_EMAIL="account@example.com"
export GOOPDL_AAS_TOKEN="aas_et/..."
goopdl download com.whatsapp
```

- The variables always win over the saved account.
- If you set only one variable, goopdl exits with an error. It does not fall
  back to the saved account or to anonymous mode.
- The AAS token must start with `aas_et/`.
- Short-lived Google Play bearer tokens stay in memory and are never written
  to disk. Each process signs in once per account, architecture, country, and
  device profile, and reuses the token until it expires (Google's expiry minus
  5 minutes, or 50 minutes) or Google rejects it.
- Besides `account.json`, goopdl writes only the device registration to disk,
  see below.
- Only the CLI reads `account.json`. From Python, a
  [`Client`](library.md#credentials) takes the account as `Credentials` and
  ignores both the variables and the file.

> [!WARNING]
> An AAS token is a long-lived credential with full Play Store access to the
> account. Use a throwaway Google account, never your main one.
> `goopdl auth login` saves the token on disk; use `--print` if you'd rather
> keep it in a secret manager. Changing the account password revokes the
> token.

### Where the account is saved

`goopdl auth login` writes `account.json` in the
[config directory](#where-tokens-live): the email and the AAS token, as JSON.

- The file is written atomically with permissions `0600`, in the `0700`
  directory.
- On Linux and macOS goopdl refuses the file if other users can read it, or if
  it isn't a regular file you own. Run `chmod 600` on it, or sign in again.
  On Windows goopdl doesn't check permissions: the file relies on your user
  profile's.
- A malformed file is an error too. Run `goopdl auth login` again, or
  `goopdl auth logout`.
- `goopdl auth logout` deletes it, together with every cached token.
  `auth.clear_auth()` and `Client.clear_cache()` don't touch it.

### What account mode saves

Signing in registers an Android device on the account. So that every command
doesn't add another one, goopdl saves the device it registered and reuses it
in later runs and in other processes:

| File in the [config directory](#where-tokens-live) | Contents |
| -------------------------------------------------- | -------- |
| `direct-device-{account}-{arch}[-{country}]-profile-{key}.json` | The device registration: GSF ID, check-in consistency token, and device config token |

- `{account}` is a hash of the email address. The email, the AAS token, and
  the bearer token are never written to it, neither in the name nor in the
  file.
- The file is written atomically with permissions `0600`, in the `0700`
  directory. A `.lock` file next to it lets several processes share one
  registration, so they register a single device.
- If Google rejects two tokens in a row on that device (HTTP 401), goopdl
  registers a new one.
- If the directory isn't writable, goopdl keeps the registration in memory
  for the current process and logs a warning.
- `goopdl auth logout` deletes these files. From Python, use
  `auth.clear_auth()` or [`Client.clear_cache()`](library.md#clearing-the-cache).
- `latest`, `download --version`, and the retries with other device profiles
  still sign in on a new device for each probe: they need different GSF IDs.

### Getting an AAS token

Pick whichever is easiest:

- **`goopdl auth login`** (recommended on desktop). See below.
- **`goopdl auth login --manual`**, if browser automation doesn't work.
- **[goopdl-auth](https://github.com/Villoh/goopdl-auth)**: a small Android
  app. Sign in on the phone and it shows the AAS token. It stores no
  credentials or tokens. Put the token in the variables above.

`goopdl auth login` saves the account. Add `--print` to get only the token on
stdout instead, for the variables or a secret manager:

```bash
TOKEN=$(goopdl auth login --print)
```

Prompts and messages go to stderr, so only the token lands in `TOKEN`.
goopdl no longer signs in with a password: Google rejects both account
passwords and app passwords with `BadAuthentication`.

#### Browser sign-in (recommended)

```bash
goopdl auth login
```

goopdl opens Chrome, Edge, Chromium, or Brave with a fresh temporary profile.
Sign in and select **I agree**. goopdl captures the email and the one-time
`oauth_token`, closes the browser, deletes the profile, and exchanges the
`oauth_token` for an AAS token.

If goopdl can't find your browser, set `GOOPDL_BROWSER` to its executable path
or use `--manual`.

#### Manual sign-in

```bash
goopdl auth login --manual
```

goopdl opens [Google EmbeddedSetup](https://accounts.google.com/EmbeddedSetup)
in your usual browser. Use a private window if it is signed in to another
Google account.

1. Sign in and select **I agree**.
2. Open developer tools (`F12`).
3. In Chrome or Edge, go to **Application → Cookies →
   `https://accounts.google.com`**.
4. Copy the `oauth_token` cookie. It starts with `oauth2_4/`.
5. Back in the terminal, enter the same email and paste the token. The token
   isn't shown as you paste it.

The `oauth_token` works once and expires quickly, so paste it right away.
