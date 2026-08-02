"""Subscription credential manager — keeps a Claude OAuth token alive.

Problem: a Claude subscription credential is an OAuth chain (short-lived access
token + long-lived, *rotating* refresh token). In k8s the sandbox runs in
ephemeral Pods, so any refresh the CLI does is lost when the Pod dies, and the
static copy in the secret goes stale (and dies entirely if the refresh token is
used/rotated elsewhere).

Solution: make the orchestrator the single authoritative holder. It seeds from
the mounted secret, persists the credential on its PersistentVolume, refreshes
the access token *before* expiry (writing back the rotated refresh token), and
keeps a fresh `credentials.json` that every sandbox session reads — so the
sandbox CLI never has to refresh and the rotating chain is never lost.

NOTE: the Claude Code OAuth token endpoint + client id are not officially
documented; the defaults below come from the public OAuth client and can be
overridden via TERRA_OAUTH_TOKEN_URL / TERRA_OAUTH_CLIENT_ID. Set
TERRA_CREDS_VERIFY=1 to force a refresh on startup and confirm the flow works.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("terrarium.creds")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Opener handler that refuses HTTP 3xx — used for the OAuth refresh POST so the
    refresh token can never be forwarded to a redirect-named host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)

# OAuth token (refresh) endpoint. Empirically (2026-06, bogus-token probe from two
# IPs), platform.claude.com AND console.anthropic.com answer EVERY refresh_token grant
# with a blanket HTTP 429 `rate_limit_error` — even an obviously-invalid token — so a
# real refresh never succeeds and the 429-backoff just masks the misroute. claude.ai
# actually validates the grant (bad token → 400 invalid_grant) and refreshes a real
# one, so it is the correct host. Override with TERRA_OAUTH_TOKEN_URL.
OAUTH_TOKEN_URL = os.environ.get("TERRA_OAUTH_TOKEN_URL", "https://claude.ai/v1/oauth/token")
# Public Claude Code OAuth client id.
OAUTH_CLIENT_ID = os.environ.get("TERRA_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
# The token endpoint sits behind Cloudflare, which 1010-bans non-browser agents
# (e.g. Python-urllib). A browser UA passes; overridable for the real client UA.
OAUTH_USER_AGENT = os.environ.get(
    "TERRA_OAUTH_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
REFRESH_SKEW_S = 15 * 60      # refresh when within 15 min of expiry
LOOP_INTERVAL_S = 4 * 60      # check every 4 min


def _now_ms() -> int:
    return int(time.time() * 1000)


def _extract_oauth(data: dict | None) -> dict | None:
    """Pull the `claudeAiOauth` object from a credentials.json (or accept a bare one)."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("claudeAiOauth"), dict):
        return data["claudeAiOauth"]
    return data if "accessToken" in data else None


class CredentialManager:
    def __init__(self, *, seed_path: Path | None, store_path: Path, kek: str | None = None,
                 token_url: str = OAUTH_TOKEN_URL, client_id: str = OAUTH_CLIENT_ID,
                 user_agent: str = OAUTH_USER_AGENT):
        self.seed_path = seed_path      # read-only mount from the secret (optional)
        self._sealed = None             # encrypted durable store (SecretStore) when a KEK is set
        if kek:
            from .secret_store import SecretStore
            self.path = store_path.with_name("credentials.sealed.json")
            self._sealed = SecretStore(self.path, kek)
            self._migrate_plaintext(store_path)   # one-time: seal a pre-existing plaintext store
        else:
            self.path = store_path      # managed plaintext copy on the PVC (no KEK → unencrypted)
        self.token_url = token_url
        self.client_id = client_id
        self.user_agent = user_agent
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_refresh: str | None = None
        self._last_error: str | None = None
        self._fail_count = 0       # consecutive refresh failures (drives the backoff)
        self._next_retry_at = 0    # epoch-ms before which we won't retry a refresh (rate-limit backoff)
        self.on_change = None      # async callback run when the credential changes (propagate to live sessions)
        # The 429 backoff MUST survive an orchestrator restart — otherwise every redeploy
        # forgets "Anthropic said try again later" and immediately re-hammers the token
        # endpoint, feeding the rate-limit spiral. Persist it (timestamps/counts only — not
        # a secret) on the PVC next to the store.
        self._state_path = store_path.with_name("credentials.refresh_state.json")
        # An explicit operator revocation must override a read-only seed mount. Without
        # this tombstone, DELETE removes the managed copy and `_current()` immediately
        # falls back to the still-mounted credential.
        self._revoked_path = store_path.with_name("credentials.revoked")
        self._load_state()

    # ---- refresh backoff state (persisted so a restart respects an active 429 backoff) --
    def _load_state(self) -> None:
        try:
            s = json.loads(self._state_path.read_text())
            self._next_retry_at = int(s.get("next_retry_at", 0))
            self._fail_count = int(s.get("fail_count", 0))
            self._last_refresh = s.get("last_refresh")
            self._last_error = s.get("last_error")
        except Exception:
            pass  # missing/corrupt → defaults (no backoff); a fresh PVC starts clean

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".state.tmp")
            tmp.write_text(json.dumps({
                "next_retry_at": self._next_retry_at, "fail_count": self._fail_count,
                "last_refresh": self._last_refresh, "last_error": self._last_error,
            }))
            tmp.replace(self._state_path)
        except OSError:
            pass

    def _migrate_plaintext(self, old: Path) -> None:
        """If a previous deploy left a plaintext credentials.json on the PVC, seal it
        into the encrypted store once and delete the cleartext copy."""
        if self._sealed.get("oauth") or not old.exists():
            return
        oauth = _extract_oauth(self._read(old))
        if oauth:
            self._sealed.put("oauth", json.dumps({"claudeAiOauth": oauth}))
            try:
                old.unlink()
            except OSError:
                pass
            log.info("creds: migrated a plaintext store into the encrypted store")

    # ---- durable IO (encrypted store when KEK set, else plaintext file) ---------
    def _read(self, p: Path) -> dict | None:
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    def _load_durable(self) -> dict | None:
        """The persisted credential — decrypted from the sealed store, or read from the
        plaintext file when no KEK is configured."""
        if self._sealed is not None:
            raw = self._sealed.get("oauth")
            try:
                return json.loads(raw) if raw else None
            except Exception:
                return None
        return self._read(self.path)

    def _has_durable(self) -> bool:
        return self._load_durable() is not None

    def _write(self, oauth: dict) -> None:
        if self._sealed is not None:
            self._sealed.put("oauth", json.dumps({"claudeAiOauth": oauth}))  # encrypted, atomic
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"claudeAiOauth": oauth}))
        os.chmod(tmp, 0o600)  # this file holds the refresh+access token in cleartext (no-KEK dev path)
        tmp.replace(self.path)  # atomic — readers never see a partial file

    def _current(self) -> dict | None:
        if self._revoked_path.exists():
            return None
        store = _extract_oauth(self._load_durable())
        seed = _extract_oauth(self._read(self.seed_path)) if self.seed_path else None
        if store and seed:
            # adopt whichever is fresher — lets an externally-updated mount (or a
            # just-set store) win without a manual store-clear.
            return seed if int(seed.get("expiresAt", 0)) > int(store.get("expiresAt", 0)) else store
        return store or seed

    def current_creds(self) -> dict | None:
        """Full credentials.json content for session provisioning — served from RAM
        (decrypted). The runner uses this so no plaintext credential is read off the PVC."""
        oauth = self._current()
        return {"claudeAiOauth": oauth} if oauth else None

    # ---- public API ------------------------------------------------------------
    def status(self) -> dict:
        """Credential health for the UI — never includes the token itself."""
        oauth = self._current()
        now = _now_ms()
        if not oauth:
            return {"present": False, "valid": False, "expires_at": None, "expires_in_s": None,
                    "subscription_type": None, "last_refresh": self._last_refresh, "last_error": self._last_error}
        exp = int(oauth.get("expiresAt") or 0)
        return {
            "present": True,
            "valid": exp > now,
            "expires_at": exp,
            "expires_in_s": max(0, (exp - now) // 1000),
            "subscription_type": oauth.get("subscriptionType"),
            "last_refresh": self._last_refresh,
            "last_error": self._last_error,
        }

    async def set_credentials(self, raw: dict | str) -> dict:
        """Set the credential from a pasted ~/.claude/.credentials.json (or oauth object)."""
        if isinstance(raw, str):
            raw = json.loads(raw)
        oauth = _extract_oauth(raw)
        if not oauth or not oauth.get("accessToken") or not oauth.get("refreshToken"):
            raise ValueError("not a valid Claude credentials JSON (need claudeAiOauth.accessToken + refreshToken)")
        async with self._lock:
            try:
                self._revoked_path.unlink()
            except FileNotFoundError:
                pass
            self._write(oauth)
            self._fail_count = 0
            self._next_retry_at = 0  # a freshly pasted token clears any rate-limit backoff
            self._last_error = None
            self._save_state()
            self._ensure_loop()
        await self._notify_change()  # push the pasted token to running sessions' Warden
        await self.ensure_fresh()    # refresh only if already near expiry
        return self.status()

    async def clear(self) -> None:
        """Revoke the managed credential and propagate that revocation to live Wardens."""
        async with self._lock:
            if self._sealed is not None:
                self._sealed.delete("oauth")
            else:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
            self._revoked_path.parent.mkdir(parents=True, exist_ok=True)
            self._revoked_path.write_text("revoked\n")
            os.chmod(self._revoked_path, 0o600)
            self._fail_count = 0
            self._next_retry_at = 0
            self._last_error = None
            self._save_state()
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._notify_change()

    # ---- OAuth refresh (blocking; run in a thread) -----------------------------
    def _refresh_call(self, refresh_token: str) -> dict:
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }).encode()
        req = urllib.request.Request(
            self.token_url, data=body,
            headers={"content-type": "application/json", "accept": "application/json", "user-agent": self.user_agent},
            method="POST",
        )
        # Refuse redirects: the token endpoint is fixed, so a 3xx on a refresh-token
        # POST is never legitimate — following one would forward the refresh token to
        # whatever host the redirect names. A refused redirect raises (fail-closed).
        with _NO_REDIRECT_OPENER.open(req, timeout=30) as r:
            return json.loads(r.read())

    @staticmethod
    def _merge(oauth: dict, resp: dict) -> dict:
        new = dict(oauth)
        if resp.get("access_token"):
            new["accessToken"] = resp["access_token"]
        if resp.get("refresh_token"):           # rotation — persist the new one
            new["refreshToken"] = resp["refresh_token"]
        if resp.get("expires_in"):
            new["expiresAt"] = _now_ms() + int(resp["expires_in"]) * 1000
        return new

    async def ensure_fresh(self, force: bool = False) -> bool:
        """Refresh if the access token is missing/expiring. Returns True if usable."""
        async with self._lock:
            oauth = self._current()
            if not oauth:
                log.warning("creds: no subscription credentials found to manage")
                return False
            if not self._has_durable():
                self._write(oauth)  # seed the managed store

            exp = int(oauth.get("expiresAt") or 0)
            if not force and exp - _now_ms() > REFRESH_SKEW_S * 1000:
                if self._next_retry_at or self._fail_count:
                    self._fail_count = 0
                    self._next_retry_at = 0  # healthy token → clear any stale backoff
                    self._save_state()
                return True  # still valid

            # Don't hammer the token endpoint while it's rate-limiting us — a 429
            # ("try again later") stays open as long as we keep retrying every loop.
            # Back off; report the token as usable until it actually expires.
            if not force and self._next_retry_at and _now_ms() < self._next_retry_at:
                return exp - _now_ms() > 0

            rt = oauth.get("refreshToken")
            if not rt:
                log.error("creds: no refreshToken present; cannot refresh")
                return False

            resp = await self._try_refresh(rt)
            if resp is None:
                # recovery: the operator may have placed a fresh token in the mount
                seed = _extract_oauth(self._read(self.seed_path)) if self.seed_path else None
                seed_rt = seed.get("refreshToken") if seed else None
                if seed_rt and seed_rt != rt:
                    log.info("creds: retrying refresh with a freshly re-seeded token")
                    resp = await self._try_refresh(seed_rt)
                if resp is None:
                    self._note_refresh_failure()
                    return False

            self._fail_count = 0
            self._next_retry_at = 0  # success → reset backoff
            self._write(self._merge(oauth, resp))
            self._last_refresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._last_error = None
            self._save_state()
            ttl = (int(self._current().get("expiresAt", 0)) - _now_ms()) // 1000
            log.info("creds: refreshed OK — access token valid ~%ss", max(ttl, 0))
        # the token rotated — push it to running sessions' Warden (outside the lock)
        await self._notify_change()
        return True

    async def _notify_change(self) -> None:
        if self.on_change is None:
            return
        try:
            await self.on_change()
        except Exception as e:  # noqa: BLE001
            log.error("creds: on_change (propagate to running sessions) failed: %s", e)

    def _note_refresh_failure(self) -> None:
        self._fail_count += 1
        delay = min(3600, 600 * (2 ** min(self._fail_count - 1, 3)))  # 10, 20, 40, 60min (cap)
        self._next_retry_at = _now_ms() + delay * 1000
        self._save_state()  # survive a restart so we don't re-hammer mid-backoff
        log.warning("creds: backing off refresh ~%ss after failure #%s (e.g. rate limit)",
                    delay, self._fail_count)

    async def _try_refresh(self, rt: str) -> dict | None:
        try:
            return await asyncio.to_thread(self._refresh_call, rt)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            self._last_error = f"refresh HTTP {e.code}"
            log.error("creds: refresh HTTP %s: %s", e.code, detail)
        except Exception as e:
            self._last_error = f"refresh error: {e}"
            log.error("creds: refresh error: %s", e)
        return None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(LOOP_INTERVAL_S)
            try:
                await self.ensure_fresh()
            except Exception as e:
                log.error("creds loop error: %s", e)

    def _ensure_loop(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def start(self) -> bool:
        verify = os.environ.get("TERRA_CREDS_VERIFY") == "1"
        ok = await self.ensure_fresh(force=verify)
        if self._has_durable():
            self._ensure_loop()
        return ok

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
