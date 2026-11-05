"""Google account AAS token authentication without credential files."""

from __future__ import annotations

import httpx

from goopdl.errors import GoopdlError

AUTH_URL = "https://android.clients.google.com/auth"

# Google codes already recognized by direct authentication; never echo other text.
_AUTH_ERROR_CODES = frozenset({
    "BadAuthentication", "NeedsBrowser", "TokenExpired", "ServiceUnavailable",
    "NoAASToken",
})


class AASTokenError(GoopdlError, ValueError):
    """Google rejected account authentication."""


def fetch_aas_token(email: str, oauth_token: str) -> str:
    """Exchange a one-time EmbeddedSetup ``oauth2_4/`` token for an AAS token."""
    failure = None
    try:
        return _exchange_aas_token(email, oauth_token)
    except AASTokenError as exc:
        failure = AASTokenError(str(exc))
    except httpx.HTTPStatusError as exc:
        request = httpx.Request("POST", AUTH_URL)
        failure = httpx.HTTPStatusError(
            "Google authentication request failed.", request=request,
            response=httpx.Response(exc.response.status_code, request=request),
        )
    except httpx.RequestError as exc:
        failure = type(exc)(
            "Google authentication request failed.",
            request=httpx.Request("POST", AUTH_URL),
        )
    except httpx.HTTPError:
        failure = httpx.HTTPError("Google authentication request failed.")
    except ValueError:
        failure = ValueError("Invalid Google account credentials.")
    finally:
        del email, oauth_token
    # No original traceback, request body, or implicit exception context escapes.
    raise failure from None


def _exchange_aas_token(email: str, oauth_token: str) -> str:
    if not isinstance(oauth_token, str) or not oauth_token.startswith("oauth2_4/"):
        raise ValueError("Invalid Google account credentials.")
    data = {
        "Email": email,
        "Token": oauth_token,
        "ACCESS_TOKEN": "1",
        "add_account": "1",
        "callerPkg": "com.google.android.gms",
        "callerSig": "38918a453d07199354f8b19af05ec6562ced5788",
        "device_country": "us",
        "droidguard_results": "null",
        "get_accountid": "1",
        "google_play_services_version": "240913000",
        "lang": "en",
        "sdk_version": "28",
        "service": "ac2dm",
    }
    response = httpx.post(
        AUTH_URL,
        data=data,
        headers={
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "",
            "app": "com.google.android.gms",
        },
        timeout=30,
    )

    values = {}
    for line in response.text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if values.get("Token"):
        return values["Token"]
    if values.get("Error"):
        reason = values["Error"]
        raise AASTokenError(reason if reason in _AUTH_ERROR_CODES else "unknown error")
    response.raise_for_status()
    raise AASTokenError("NoAASToken")
