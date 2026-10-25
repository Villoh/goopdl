# How it works

**English** · [Español](es/how-it-works.md)

## The download pipeline

1. **Authenticate.** Get a token, either anonymously from the dispenser
   (rotating device profiles until one works) or from your Google account.
   See [authentication](authentication.md).
2. **Fetch details.** Read the app's metadata (version, size, splits) from
   Google Play's protobuf API.
3. **Purchase.** "Buy" the free app to get download authorization.
4. **Get delivery.** Ask for download URLs for the base APK, split APKs, OBB
   files, and asset packs.
5. **Download.** Stream the files from Google's CDN in parallel, with progress
   bars. Play Asset Delivery packs are gzip-decompressed on the fly.

## Token pool

`download` and `latest` draw tokens from a pool of 5 anonymous tokens per
architecture, country, and pinned profile, stored in
`~/.config/goopdl/token-pool-{arch}[-{country}][-profile-{key}].json`.

- Before each call, goopdl counts the unexpired tokens and requests only the
  missing ones. A full pool means no dispenser calls at all.
- Tokens expire after 50 minutes.
- If Google rejects a token mid-request (HTTP 401), goopdl replaces it.
- Tokens are handed out round-robin, and the position survives between runs.
- Several goopdl processes can share a pool: updates are locked and written
  atomically.
- If the dispenser answers HTTP 429, goopdl stops trying other profiles
  instead of hammering it.

The pool keeps goopdl under Aurora's rate limit of about 20 requests per hour.
In [Google account mode](authentication.md#google-account) there is no pool.

## Finding the latest version

Google rolls out app updates in stages, by device cohort. The cohort comes
from the device's GSF ID (Google Services Framework ID), which is assigned
when the token is issued. Two devices can see different versions of the same
app on the same day, such as 434 and 435.

So `latest` asks through several GSF IDs and keeps the highest version code:

- Each probe uses a different GSF ID, so it may land in a different cohort.
- Probing stops once the highest version code holds for `--stable` probes in
  a row.
- The device profile sets the SDK level Google sees. Profiles with newer SDKs
  tend to get new versions first, which is why `latest` defaults to the
  top-ranked one.

`download --version 434.0.0.44.74` works the same way: it probes GSF IDs until
one sees the requested version.

## Device profiles

goopdl ships device profiles as `.properties` files in
[`goopdl/profiles/`](../goopdl/profiles). They describe a real device (model,
Android version, ABI, Play Store version) so Google serves the right build.

During anonymous authentication goopdl rotates through profiles until the
dispenser accepts one. Run `goopdl profiles` to list them, and pin one with
`--profile`.

## Architectures

| Flag | ABI | Devices |
| ---- | --- | ------- |
| `arm64` (default) | arm64-v8a | Phones from about 2017 on |
| `armv7` | armeabi-v7a | Older 32-bit phones |
| `x86` | x86 | 32-bit Intel/AMD emulators and devices |
| `x86_64` | x86_64 | 64-bit Intel/AMD emulators and devices |
| `tv` | arm64-v8a / armeabi-v7a | Android TV boxes and streaming devices |

`download` accepts several, comma-separated (`-a arm64,armv7`), and downloads
each in turn.
