import base64
import re
from datetime import UTC, datetime
from email.utils import parseaddr
from html import unescape
from urllib.parse import urlencode

import httpx

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def body_text(payload):
    """Walk nested MIME; attachments and unrelated MIME types are ignored."""
    chunks = []
    if payload.get("mimeType") in ("text/plain", "text/html"):
        data = payload.get("body", {}).get("data")
        if data:
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
            chunks.append(unescape(re.sub(r"<[^>]+>", "\n", raw)))
    for part in payload.get("parts", []):
        chunks.append(body_text(part))
    return "\n".join(chunks)


def parse_steam_code(message, login, earliest, current=None):
    current = current or datetime.now(UTC)
    timestamp = datetime.fromtimestamp(int(message.get("internalDate", 0)) / 1000, UTC)
    if timestamp < earliest or timestamp > current:
        return None
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    if parseaddr(headers.get("from", ""))[1].lower() != "noreply@steampowered.com":
        return None
    # Do not distribute account recovery, password reset, or email-change codes.
    subject = headers.get("subject", "").lower()
    if not any(
        marker in subject for marker in ("access from new", "new sign in", "нового комп", "нового пристро")
    ):
        return None
    text = body_text(payload)
    if not re.search(r"(?<![\w])" + re.escape(login) + r"(?![\w])", text, re.IGNORECASE):
        return None
    codes = set(re.findall(r"(?m)^\s*([A-Z0-9]{5})\s*$", text))
    return next(iter(codes)) if len(codes) == 1 else None


class Gmail:
    def __init__(self, cfg, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client

    @property
    def redirect(self):
        return self.cfg.public_base_url + "/oauth/gmail/callback"

    def authorize_url(self, state):
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": self.cfg.google_client_id,
                "redirect_uri": self.redirect,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )

    async def token(self, **grant):
        response = await self.client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": self.cfg.google_client_id,
                "client_secret": self.cfg.google_client_secret.get_secret_value(),
                **grant,
            },
        )
        response.raise_for_status()
        return response.json()

    async def exchange(self, code):
        tokens = await self.token(code=code, redirect_uri=self.redirect, grant_type="authorization_code")
        if not tokens.get("refresh_token"):
            raise ValueError("missing_refresh_token")
        profile = await self.get("profile", tokens["access_token"])
        return {"email": profile["emailAddress"], "refresh_token": tokens["refresh_token"]}

    async def get(self, path, token, **params):
        response = await self.client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/" + path,
            headers={"Authorization": "Bearer " + token},
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def latest_code(self, credentials, login, earliest):
        token = (await self.token(grant_type="refresh_token", refresh_token=credentials["refresh_token"]))[
            "access_token"
        ]
        result = await self.get(
            "messages",
            token,
            q=f"from:noreply@steampowered.com after:{int(earliest.timestamp())}",
            maxResults=20,
        )
        messages = []
        for item in result.get("messages", []):
            messages.append(await self.get("messages/" + item["id"], token, format="full"))
        for message in sorted(messages, key=lambda m: int(m["internalDate"]), reverse=True):
            code = parse_steam_code(message, login, earliest)
            if code:
                return code, message["id"]
        return None
