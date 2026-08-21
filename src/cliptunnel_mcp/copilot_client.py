"""GitHub Copilot API client — stdlib only.

Handles token exchange (gho_ -> Copilot session token) and chat completions
with function calling support.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class CopilotToken:
    """A Copilot session token with expiry."""
    token: str
    expires_at: int  # unix timestamp

    def is_expired(self) -> bool:
        """Return True if the token has expired (with 60s safety margin)."""
        return time.time() >= (self.expires_at - 60)


class CopilotClient:
    """Client for the GitHub Copilot API.

    Reads the gho_ OAuth token from a file, exchanges it for a short-lived
    session token, and makes chat completion requests.
    """

    TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
    CHAT_URL = "https://api.individual.githubcopilot.com/chat/completions"
    RESPONSES_URL = "https://api.individual.githubcopilot.com/responses"
    DEFAULT_MODEL = "mai-code-1.1-flash"
    # Models that only support /responses endpoint
    RESPONSES_ONLY_MODELS = {"mai-code-1.1-flash", "mai-code-1-flash-picker"}

    # ── GitHub OAuth Device Flow ───────────────────────────────────────────
    DEVICE_CODE_URL = "https://github.com/login/device/code"
    TOKEN_POLL_URL = "https://github.com/login/oauth/access_token"
    DEVICE_CLIENT_ID = "Iv1.b507a08c87ecfe98"
    DEVICE_SCOPE = "read:user"

    def __init__(self, token_file: str = ".copilot_agent_token") -> None:
        self._token_file = token_file
        self._copilot_token: CopilotToken | None = None
        self._ssl_ctx = ssl.create_default_context()

    def _read_oauth_token(self) -> str:
        """Read the gho_ token from file."""
        try:
            with open(self._token_file, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Copilot token file not found: {self._token_file}"
            )

    def _exchange_token(self) -> CopilotToken:
        """Exchange the gho_ OAuth token for a Copilot session token."""
        oauth_token = self._read_oauth_token()
        req = urllib.request.Request(self.TOKEN_URL)
        req.add_header("Authorization", f"token {oauth_token}")
        req.add_header("User-Agent", "copilot-agent/1.0")
        req.add_header("Accept", "application/json")
        req.add_header("Editor-Version", "vscode/1.99.0")
        req.add_header("Editor-Plugin-Version", "copilot/1.0.0")
        req.add_header("Copilot-Integration-Id", "vscode-chat")
        r = urllib.request.urlopen(req, timeout=10, context=self._ssl_ctx)
        data = json.loads(r.read().decode())
        return CopilotToken(
            token=data["token"],
            expires_at=data.get("expires_at", int(time.time()) + 1800),
        )

    def _ensure_token(self) -> str:
        """Return a valid Copilot token, exchanging if necessary."""
        if self._copilot_token is None or self._copilot_token.is_expired():
            self._copilot_token = self._exchange_token()
        return self._copilot_token.token

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        """Send a chat completion request and return the response dict.

        The response follows the OpenAI chat completion format:
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "...",
                    "tool_calls": [...]  # optional
                },
                "finish_reason": "stop" | "tool_calls"
            }],
            "usage": {...}
        }
        """
        token = self._ensure_token()
        body: dict = {
            "model": model or self.DEFAULT_MODEL,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
        if max_tokens:
            body["max_tokens"] = max_tokens

        data = json.dumps(body).encode()
        req = urllib.request.Request(self.CHAT_URL, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Copilot-Integration-Id", "vscode-chat")
        req.add_header("Editor-Version", "vscode/1.99.0")
        req.add_header("Editor-Plugin-Version", "copilot/1.0.0")
        req.add_header("User-Agent", "copilot-agent/1.0")

        try:
            r = urllib.request.urlopen(req, timeout=120, context=self._ssl_ctx)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            raise RuntimeError(f"Copilot /chat/completions error {e.code}: {err_body[:500]}") from e
        return json.loads(r.read().decode())

    def responses(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        """Send a responses API request and return a normalized dict.

        The /responses endpoint uses a different format than /chat/completions:
        - Input: list of messages (converted to responses format)
        - Tools: [{type:"function", name, description, parameters}] (no nested "function" key)
        - Output: {output: [...], output_text: "..."}

        Returns a normalized dict in the same shape as chat() so the agent
        loop can handle both uniformly:
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "...",
                    "tool_calls": [...]  # optional
                },
                "finish_reason": "stop" | "tool_calls"
            }]
        }
        """
        token = self._ensure_token()
        mdl = model or self.DEFAULT_MODEL

        # Convert chat messages to responses input format.
        # System messages go into the "instructions" field, not the input array.
        instructions = None
        input_items = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                instructions = content
            elif role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": content,
                })
            elif role == "assistant" and msg.get("tool_calls"):
                if content:
                    input_items.append({"role": "assistant", "content": content})
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    input_items.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", "{}"),
                    })
            else:
                input_items.append({"role": role, "content": content})
        body: dict = {
            "model": mdl,
            "input": input_items,
        }
        if instructions:
            body["instructions"] = instructions

        # Convert tools from chat format to responses format
        if tools:
            resp_tools = []
            for t in tools:
                func = t.get("function", t)
                resp_tools.append({
                    "type": "function",
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "parameters": func.get("parameters", {}),
                })
            body["tools"] = resp_tools

        if max_tokens:
            body["max_output_tokens"] = max_tokens
        data = json.dumps(body).encode()
        req = urllib.request.Request(self.RESPONSES_URL, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Copilot-Integration-Id", "vscode-chat")
        req.add_header("Editor-Version", "vscode/1.99.0")
        req.add_header("Editor-Plugin-Version", "copilot/1.0.0")
        req.add_header("User-Agent", "copilot-agent/1.0")

        try:
            r = urllib.request.urlopen(req, timeout=120, context=self._ssl_ctx)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            raise RuntimeError(f"Copilot /responses error {e.code}: {err_body[:500]}") from e
        raw = json.loads(r.read().decode())

        # Normalize responses format -> chat completions format
        output_items = raw.get("output", [])
        tool_calls = []
        content = raw.get("output_text") or ""

        for item in output_items:
            if item.get("type") == "function_call":
                tool_calls.append({
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                })
            elif item.get("type") == "message" and not content:
                # Extract text from message content blocks
                msg_content = item.get("content", [])
                if isinstance(msg_content, list):
                    parts = [c.get("text", "") for c in msg_content if c.get("type") == "output_text"]
                    content = "".join(parts)
                elif isinstance(msg_content, str):
                    content = msg_content

        finish_reason = "tool_calls" if tool_calls else "stop"
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": content,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                },
                "finish_reason": finish_reason,
            }],
            "usage": raw.get("usage", {}),
            "_raw_responses": raw,
        }

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        """Send a request using the appropriate endpoint for the model.

        Models in RESPONSES_ONLY_MODELS use /responses; others use /chat/completions.
        Returns a normalized chat-completions-format dict.
        """
        mdl = model or self.DEFAULT_MODEL
        if mdl in self.RESPONSES_ONLY_MODELS:
            return self.responses(messages, tools, model, temperature, max_tokens)
        return self.chat(messages, tools, model, temperature, max_tokens)

    # ── GitHub OAuth Device Flow ───────────────────────────────────────────

    def start_device_flow(self) -> dict:
        """Start GitHub OAuth device flow.

        Returns dict with device_code, user_code, verification_uri, expires_in, interval.
        """
        data = (
            f"client_id={self.DEVICE_CLIENT_ID}&scope={self.DEVICE_SCOPE}"
        ).encode()
        req = urllib.request.Request(self.DEVICE_CODE_URL, data=data, method="POST")
        req.add_header("Accept", "application/json")
        r = urllib.request.urlopen(req, timeout=15, context=self._ssl_ctx)
        return json.loads(r.read().decode())

    def poll_device_flow(self, device_code: str) -> dict:
        """Poll for device flow completion.

        Returns {"access_token": "gho_...", "token_type": "bearer", "scope": "read:user"}
        or {"error": "authorization_pending", "interval": N}.
        """
        data = (
            f"client_id={self.DEVICE_CLIENT_ID}"
            f"&device_code={device_code}"
            f"&grant_type=urn:ietf:params:oauth:grant-type:device_code"
        ).encode()
        req = urllib.request.Request(self.TOKEN_POLL_URL, data=data, method="POST")
        req.add_header("Accept", "application/json")
        r = urllib.request.urlopen(req, timeout=15, context=self._ssl_ctx)
        return json.loads(r.read().decode())

    def save_oauth_token(self, token: str, path: str | None = None) -> None:
        """Save the gho_ token to file."""
        target = path or self._token_file
        with open(target, "w") as f:
            f.write(token.strip())