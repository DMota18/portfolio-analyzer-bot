"""
Public.com API Helper — Two-step authentication flow.

Public's API requires:
  1. POST your secret key to get a temporary access token
  2. Use that access token as Bearer auth for all subsequent calls

Access tokens expire after the validity period, so we cache and refresh automatically.
"""

import time
import logging

import aiohttp

from config import PUBLIC_SECRET_KEY, PUBLIC_ACCOUNT_ID

logger = logging.getLogger("agent.public_api")

AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"
BASE_URL = "https://api.public.com/userapigateway/trading"
TOKEN_VALIDITY_MINUTES = 55  # Request 55 min, refresh before the 60 min expiry

# ── Token Cache ──────────────────────────────────────────────────────────────
_cached_token = None
_token_expires_at = 0


async def get_access_token(session: aiohttp.ClientSession) -> str | None:
    """
    Get a valid access token, refreshing if needed.
    Uses the secret key to mint a new token from Public's auth endpoint.
    """
    global _cached_token, _token_expires_at

    # Return cached token if still valid (with 60s buffer)
    if _cached_token and time.time() < (_token_expires_at - 60):
        return _cached_token

    if not PUBLIC_SECRET_KEY:
        logger.error("PUBLIC_SECRET_KEY not set")
        return None

    try:
        payload = {
            "validityInMinutes": TOKEN_VALIDITY_MINUTES,
            "secret": PUBLIC_SECRET_KEY,
        }
        async with session.post(
            AUTH_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"Public auth failed ({resp.status}): {error_text}")
                return None
            data = await resp.json()

        token = data.get("accessToken")
        if not token:
            logger.error(f"No accessToken in response: {data}")
            return None

        _cached_token = token
        _token_expires_at = time.time() + (TOKEN_VALIDITY_MINUTES * 60)
        logger.info("Public.com access token refreshed")
        return token

    except Exception as e:
        logger.error(f"Public auth error: {e}")
        return None


async def fetch_portfolio(session: aiohttp.ClientSession) -> dict | None:
    """
    Fetch full portfolio from Public.com API.
    Handles the two-step auth automatically.
    """
    if not PUBLIC_ACCOUNT_ID:
        logger.error("PUBLIC_ACCOUNT_ID not set")
        return None

    token = await get_access_token(session)
    if not token:
        return None

    url = f"{BASE_URL}/{PUBLIC_ACCOUNT_ID}/portfolio/v2"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 401:
                # Token might have expired early — force refresh and retry once
                logger.warning("Got 401, forcing token refresh")
                global _cached_token, _token_expires_at
                _cached_token = None
                _token_expires_at = 0
                token = await get_access_token(session)
                if not token:
                    return None
                headers = {"Authorization": f"Bearer {token}"}
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as retry_resp:
                    if retry_resp.status != 200:
                        logger.error(f"Public API retry failed: {retry_resp.status}")
                        return None
                    return await retry_resp.json()

            if resp.status != 200:
                logger.error(f"Public API returned {resp.status}")
                return None
            return await resp.json()

    except Exception as e:
        logger.error(f"Public API error: {e}")
        return None
