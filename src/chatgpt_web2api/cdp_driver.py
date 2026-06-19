"""CDP Driver — browser automation via Chrome DevTools Protocol.

Connects to an existing Chrome instance via CDP websocket.
Provides typed primitives for:
  - Auth token management
  - JS evaluation
  - Page navigation
  - Message input via CDP Input.insertText
  - Response retrieval via conversation API
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional

try:
    import websockets
except ImportError:
    raise ImportError("pip install websockets")

logger = logging.getLogger(__name__)


@dataclass
class StreamChunk:
    """A single streaming chunk."""
    delta: str
    finish_reason: Optional[str] = None


class CDPDriver:
    """Chrome DevTools Protocol driver for ChatGPT automation."""

    def __init__(self, cdp_port: int = 9222) -> None:
        self.port = cdp_port
        self._ws = None
        self._msg_id = 0
        self._access_token = ""
        self._user_name = ""
        self._current_conv_id: Optional[str] = None
        self._current_model: Optional[str] = None

    # ── Connection ────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to Chrome's CDP and authenticate."""
        ws_url = await self._find_page_ws()
        self._ws = await websockets.connect(ws_url, max_size=100 * 1024 * 1024)
        logger.info("CDP connected to Chrome")
        await self._refresh_token()

    async def _find_page_ws(self) -> str:
        """Find a suitable page's websocket URL."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/json/list"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            targets = json.loads(resp.read())

        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("No browser pages found — is Chrome running with chatgpt.com?")

        # Prefer chatgpt.com page
        chatgpt = [t for t in pages if "chatgpt.com" in t.get("url", "") or "chatgpt.com" in t.get("title", "")]
        target = chatgpt[0] if chatgpt else pages[0]
        logger.info("Using page: %s", target.get("title", "")[:60])
        return target["webSocketDebuggerUrl"]

    async def _refresh_token(self) -> None:
        """Get a fresh access token from /api/auth/session."""
        raw = await self._js(
            "(async () => {"
            "  const r = await fetch('/api/auth/session', {credentials:'include'});"
            "  const d = await r.json();"
            "  return {token: d.accessToken || '', user: d.user?.name || ''};"
            "})()"
        )
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str) and raw:
            data = json.loads(raw)
        else:
            data = {}
        self._access_token = data.get("token", "")
        self._user_name = data.get("user", "")
        if not self._access_token:
            raise RuntimeError("No access token — not logged into ChatGPT")
        logger.info("Auth: %d chars, user: %s", len(self._access_token), self._user_name)

    # ── CDP primitives ────────────────────────────────────────

    async def _cdp(self, method: str, params: dict = None, timeout: float = 15) -> dict:
        self._msg_id += 1
        mid = self._msg_id
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(
                self._ws.recv(), timeout=max(1, deadline - time.monotonic())
            )
            resp = json.loads(raw)
            if resp.get("id") == mid:
                return resp
        raise TimeoutError(f"CDP timeout: {method}")

    async def _js(self, expr: str, timeout: float = 15) -> str:
        resp = await self._cdp("Runtime.evaluate", {
            "expression": expr,
            "awaitPromise": True,
            "returnByValue": True,
            "timeout": int(timeout * 1000),
        }, timeout=timeout)
        return resp.get("result", {}).get("result", {}).get("value", "")

    async def _js_with_data(self, expr_template: str, data: dict, timeout: float = 15) -> str:
        """Evaluate JS with safely injected data variables.

        Injects data as ``const __D = <json>;`` prefix, eliminating
        all string-concatenation injection vectors.  The template can
        reference ``__D.keyName`` for any key in *data*.
        """
        expr = f"(() => {{ const __D = {json.dumps(data)}; return {expr_template}; }})()"
        return await self._js(expr, timeout=timeout)

    # ── Model Selection ───────────────────────────────────────

    async def select_model(self, slug: str) -> bool:
        """Select a model in the ChatGPT model picker.

        Clicks the model picker button, waits for the dropdown,
        finds the item matching *slug*, and clicks it.

        Returns True if the model was selected, False if it failed
        (e.g. model not found, picker not available).  Failures are
        non-fatal — the request proceeds with whatever model is active.
        """
        if slug in ("auto", None, ""):
            return True  # auto is the default, no action needed

        aliases = self._model_aliases(slug)

        # Track the current model
        self._current_model = slug

        # Click the model picker button
        picker_clicked = await self._js_with_data(
            "(function() {"
            "  var btn = document.querySelector('#model-selector-btn') "
            "    || document.querySelector('button.__composer-pill') "
            "    || document.querySelector('button[aria-label*=\"Model\"]') "
            "    || document.querySelector('[data-testid*=\"model\"]') "
            "    || document.querySelector('button[class*=\"model\"]') "
            "    || Array.from(document.querySelectorAll('button')).find(function(b) {"
            "      var t = (b.innerText || b.textContent || '').trim();"
            "      return __D.aliases.some(function(a) { return t.indexOf(a) !== -1; });"
            "    });"
            "  if (!btn) return 'no picker';"
            "  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(t) {"
            "    btn.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window}));"
            "  });"
            "  return 'clicked';"
            "})()",
            {"aliases": aliases},
        )
        if picker_clicked != "clicked":
            logger.warning("Model picker not found: %s — proceeding with active model", picker_clicked)
            return False

        # Wait for dropdown to appear
        await asyncio.sleep(0.8)

        # Find and click the target model item
        # The dropdown renders model items as buttons or list items with the slug
        result = await self._js_with_data(
            "(function() {"
            "  function norm(s) {"
            "    return (s || '').toLowerCase()"
            "      .replace(/gpt\\s*[- ]?\\s*/g, 'gpt-')"
            "      .replace(/\\s+/g, '-')"
            "      .replace(/\\./g, '-')"
            "      .replace(/--+/g, '-')"
            "      .replace(/^-|-$/g, '');"
            "  }"
            "  var slug = norm(__D.slug);"
            "  var aliases = (__D.aliases || []).map(function(a) { return String(a).toLowerCase(); });"
            "  var normAliases = aliases.map(norm);"
            "  var display = slug.replace(/^gpt-/, 'gpt ');"
            "  var items = Array.from(document.querySelectorAll('[role=\"menuitemradio\"], [role=\"menuitem\"], [role=\"option\"], button'));"
            "  for (var i = 0; i < items.length; i++) {"
            "    var el = items[i];"
            "    var text = (el.innerText || el.textContent || '').trim().toLowerCase();"
            "    var dataSlug = (el.getAttribute('data-slug') || '').toLowerCase();"
            "    var textNorm = norm(text);"
            "    var dataNorm = norm(dataSlug);"
            "    var matched = dataNorm === slug || textNorm === slug || textNorm.indexOf(slug) !== -1 || text.indexOf(display) !== -1;"
            "    matched = matched || aliases.some(function(a) { return text === a || text.indexOf(a) !== -1 || dataSlug === a; });"
            "    matched = matched || normAliases.some(function(a) { return textNorm === a || textNorm.indexOf(a) !== -1 || dataNorm === a; });"
            "    if (matched && text) {"
            "      ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(t) {"
            "        el.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window}));"
            "      });"
            "      return 'selected';"
            "    }"
            "  }"
            "  return 'not-found';"
            "})()",
            {"slug": slug.lower(), "aliases": aliases},
        )

        if result in ("selected", "selected-fallback"):
            logger.info("Model selected: %s (%s)", slug, result)
            await asyncio.sleep(0.5)  # Let UI settle
            return True

        logger.warning("Model '%s' not found in picker: %s — proceeding with active model", slug, result)
        return False

    # ── Navigation ────────────────────────────────────────────

    def _model_aliases(self, slug: str) -> list[str]:
        """Return picker labels and aliases for current ChatGPT Web model tiers."""
        raw = str(slug or "").strip()
        compact = raw.lower().replace(" ", "").replace("-", "").replace("_", "")
        aliases = {raw, raw.lower()}
        tier_aliases = {
            "fast": ["\u6781\u901f"],
            "\u6781\u901f": ["\u6781\u901f", "fast"],
            "balanced": ["\u5747\u8861"],
            "\u5747\u8861": ["\u5747\u8861", "balanced"],
            "advanced": ["\u9ad8\u7ea7"],
            "\u9ad8\u7ea7": ["\u9ad8\u7ea7", "advanced"],
            "ultra": ["\u8d85\u9ad8"],
            "\u8d85\u9ad8": ["\u8d85\u9ad8", "ultra"],
            "prostandard": ["Pro \u6807\u51c6", "pro standard", "pro-standard"],
            "pro\u6807\u51c6": ["Pro \u6807\u51c6", "pro standard", "pro-standard"],
            "proextended": ["Pro \u6269\u5c55", "pro extended", "pro-extended", "gpt-5.5 pro"],
            "proext": ["Pro \u6269\u5c55", "pro extended", "pro-extended", "pro-ext"],
            "pro\u6269\u5c55": ["Pro \u6269\u5c55", "pro extended", "pro-extended", "gpt-5.5 pro"],
            "gpt5.5pro": ["Pro \u6269\u5c55", "GPT-5.5"],
            "gpt55pro": ["Pro \u6269\u5c55", "GPT-5.5"],
            "gpt55": ["GPT-5.5", "gpt-5-5"],
        }
        for value in tier_aliases.get(compact, []):
            aliases.add(value)
            aliases.add(value.lower())
        return [a for a in aliases if a]

    async def navigate_new_chat(self, gizmo_id: str = None) -> None:
        """Navigate to a fresh chat. Optionally scope to a project gizmo."""
        url = f"https://chatgpt.com/g/{gizmo_id}/project" if gizmo_id else "https://chatgpt.com/"
        logger.info("Navigate: %s", url)
        await self._cdp("Page.navigate", {"url": url})
        await asyncio.sleep(2)

        # Wait for textarea
        for _ in range(30):
            result = await self._js(
                "(function() {"
                "  return JSON.stringify({"
                "    ready: !!document.querySelector('#prompt-textarea'),"
                "    url: location.href"
                "  });"
                "})()"
            )
            try:
                state = json.loads(result)
                if state.get("ready"):
                    logger.info("Page ready: %s", state.get("url"))
                    break
            except (json.JSONDecodeError, TypeError):
                pass
            await asyncio.sleep(0.5)

        # Settle time for sentinel init
        await asyncio.sleep(2)
        self._current_conv_id = None

    async def navigate_conversation(self, conversation_id: str) -> None:
        """Navigate to an existing conversation for multi-turn."""
        url = f"https://chatgpt.com/c/{conversation_id}"
        logger.info("Navigate to conversation: %s", url)
        await self._cdp("Page.navigate", {"url": url})
        await asyncio.sleep(3)

        # Wait for textarea
        for _ in range(30):
            result = await self._js(
                "(function() {"
                "  return JSON.stringify({"
                "    ready: !!document.querySelector('#prompt-textarea'),"
                "    url: location.href"
                "  });"
                "})()"
            )
            try:
                state = json.loads(result)
                if state.get("ready"):
                    logger.info("Conversation ready: %s", state.get("url"))
                    break
            except (json.JSONDecodeError, TypeError):
                pass
            await asyncio.sleep(0.5)

        await asyncio.sleep(1)
        self._current_conv_id = conversation_id

    # ── Message Input ─────────────────────────────────────────

    async def type_message(self, text: str) -> None:
        """Type text into the ChatGPT prompt textarea."""
        # Focus
        focus_result = await self._js(
            "(function() {"
            "  var el = document.querySelector('#prompt-textarea');"
            "  if (!el) return 'no textarea';"
            "  el.focus();"
            "  return 'focused';"
            "})()"
        )
        if focus_result != 'focused':
            raise RuntimeError("No textarea found")

        # Clear existing text by selecting all first
        await self._cdp("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2})
        await self._cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2})
        await asyncio.sleep(0.1)

        # Insert text via CDP
        await self._cdp("Input.insertText", {"text": text})
        await asyncio.sleep(0.5)

        # Verify
        content = await self._js(
            "document.querySelector('#prompt-textarea')?.textContent || ''"
        )
        if not content:
            raise RuntimeError("Failed to insert text into textarea")
        logger.info("Typed: %s", text[:80])

    async def click_send(self) -> None:
        """Click the send button via JS MouseEvent sequence."""
        # Wait for button to be enabled
        for _ in range(10):
            has_btn = await self._js(
                "(function() {"
                "  var btn = document.querySelector('button[data-testid=\"send-button\"]');"
                "  return btn && !btn.disabled ? 'yes' : 'no';"
                "})()"
            )
            if has_btn == "yes":
                break
            await asyncio.sleep(0.3)

        result = await self._js(
            "(function() {"
            "  var btn = document.querySelector('button[data-testid=\"send-button\"]');"
            "  if (!btn) return 'no send button';"
            "  if (btn.disabled) return 'button disabled';"
            "  var evts = ['pointerdown','mousedown','pointerup','mouseup','click'];"
            "  for (var i = 0; i < evts.length; i++) {"
            "    btn.dispatchEvent(new MouseEvent(evts[i], {bubbles:true, cancelable:true, view:window}));"
            "  }"
            "  return 'sent';"
            "})()"
        )
        if result != "sent":
            raise RuntimeError(f"Send failed: {result}")
        logger.info("Message sent")

    # ── Response Retrieval ────────────────────────────────────

    async def send_and_stream(self, text: str, timeout: float = 120) -> AsyncIterator[StreamChunk]:
        """Send a message and yield streaming response chunks.

        This is the main high-level operation:
        1. Count existing assistant messages
        2. Type message
        3. Click send
        4. Wait for new assistant message to appear
        5. Poll DOM for streaming text
        6. Fetch final text from conversation API
        """
        # Count existing assistants BEFORE sending
        initial_raw = await self._js(
            "document.querySelectorAll('[data-message-author-role=\"assistant\"]').length"
        )
        initial_count = int(initial_raw) if initial_raw else 0

        # Type and send
        await self.type_message(text)
        await self.click_send()

        # Wait for a new assistant message (up to 60s)
        deadline = time.monotonic() + min(timeout, 60)
        while time.monotonic() < deadline:
            raw = await self._js(
                "document.querySelectorAll('[data-message-author-role=\"assistant\"]').length"
            )
            if int(raw or 0) > initial_count:
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Timed out waiting for assistant response")

        logger.info("Assistant message appeared, waiting for completion...")

        # Poll until generation is done (Stop button gone)
        last_dom_text = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = await self._js(
                "(function() {"
                "  var msgs = document.querySelectorAll('[data-message-author-role=\"assistant\"]');"
                "  if (!msgs.length) return JSON.stringify({text:'', done:false});"
                "  var last = msgs[msgs.length - 1];"
                "  var md = last.querySelector('.markdown');"
                "  var text = md ? (md.textContent || '') : '';"
                "  var stopBtn = document.querySelector('button[aria-label=\"Stop\"]');"
                "  return JSON.stringify({text: text, done: !stopBtn && !!md});"
                "})()",
            )
            try:
                data = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                await asyncio.sleep(0.5)
                continue

            current = data.get("text", "")
            done = data.get("done", False)

            if len(current) > len(last_dom_text):
                delta = current[len(last_dom_text):]
                last_dom_text = current
                yield StreamChunk(delta=delta)

            if done:
                break

            await asyncio.sleep(0.5)

        # Wait for URL to become /c/{id}
        conv_id = ""
        for _ in range(30):
            url = await self._js("window.location.href")
            if "/c/" in url:
                conv_id = url.split("/c/")[1].split("/")[0].split("?")[0]
                break
            await asyncio.sleep(0.5)

        if conv_id:
            logger.info("Conversation: %s", conv_id)
            self._current_conv_id = conv_id
            # Fetch final text from API (more reliable than DOM for thinking models)
            for _ in range(60):
                api_text = await self._fetch_text(conv_id)
                if api_text and len(api_text) > len(last_dom_text):
                    yield StreamChunk(delta=api_text[len(last_dom_text):])
                    last_dom_text = api_text
                    break
                if api_text:
                    break
                await asyncio.sleep(0.5)

        yield StreamChunk(delta="", finish_reason="stop")

    async def _fetch_text(self, conversation_id: str) -> str:
        """Fetch the latest assistant text from the conversation API."""
        return await self._js_with_data(
            "(async function() {"
            "  try {"
            "    var r = await fetch('/backend-api/conversation/' + __D.conv_id + '?offset=0&limit=5', {"
            "      headers: {'Authorization': 'Bearer ' + __D.token}"
            "    });"
            "    if (!r.ok) return '';"
            "    var conv = await r.json();"
            "    var mapping = conv.mapping || {};"
            "    var current = conv.current_node || '';"
            "    if (current && mapping[current]) {"
            "      var node = mapping[current];"
            "      if (node.message && node.message.author && node.message.author.role === 'assistant') {"
            "        if (node.message.content.content_type === 'text') {"
            "          var parts = node.message.content.parts || [];"
            "          if (parts.length > 0 && parts[0]) return parts[0];"
            "        }"
            "      }"
            "    }"
            "    return '';"
            "  } catch(e) { return ''; }"
            "})()",
            {"conv_id": conversation_id, "token": self._access_token},
            timeout=15,
        ) or ""

    # ── API helpers ───────────────────────────────────────────

    async def get_models(self) -> list[dict]:
        raw = await self._js_with_data(
            "(async () => {"
            "  try {"
            "    var r = await fetch('/backend-api/models?iim=false&is_gizmo=false', {"
            "      headers: {'Authorization': 'Bearer ' + __D.token}"
            "    });"
            "    return await r.text();"
            "  } catch(e) { return ''; }"
            "})()",
            {"token": self._access_token},
        )
        try:
            data = json.loads(raw) if isinstance(raw, str) and raw else raw
        except json.JSONDecodeError:
            return []

        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        if not isinstance(data, dict):
            return []

        for key in ("models", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [m for m in value if isinstance(m, dict)]

        if isinstance(data.get("categories"), list):
            models: list[dict] = []
            for category in data["categories"]:
                if not isinstance(category, dict):
                    continue
                for model in category.get("models", []) or []:
                    if isinstance(model, dict):
                        models.append(model)
            return models

        return []

    async def get_projects(self) -> list[dict]:
        raw = await self._js_with_data(
            "(async () => {"
            "  var r = await fetch('/backend-api/gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=5&limit=50', {"
            "    headers: {'Authorization': 'Bearer ' + __D.token}"
            "  });"
            "  var data = await r.json();"
            "  return JSON.stringify((data.items || []).map(function(i) {"
            "    var g = (i.gizmo || {}).gizmo || {};"
            "    return {id: g.id, name: (g.display || {}).name || '', memory_scope: g.memory_scope || '', short_url: g.short_url || ''};"
            "  }));"
            "})()",
            {"token": self._access_token},
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    # ── Conversation Management ──────────────────────────────

    async def get_conversations(
        self,
        offset: int = 0,
        limit: int = 28,
        order: str = "updated",
    ) -> list[dict]:
        """List recent conversations."""
        raw = await self._js_with_data(
            "(async () => {"
            "  var r = await fetch('/backend-api/conversations?offset=' + __D.offset + '&limit=' + __D.limit + '&order=' + __D.order, {"
            "    headers: {'Authorization': 'Bearer ' + __D.token}"
            "  });"
            "  var data = await r.json();"
            "  return JSON.stringify((data.items || []).map(function(c) {"
            "    return {id: c.id, title: c.title || 'Untitled', "
            "      update_time: c.update_time, create_time: c.create_time,"
            "      is_archived: !!c.is_archived, gizmo_id: c.gizmo_id || null};"
            "  }));"
            "})()",
            {"token": self._access_token, "offset": str(offset), "limit": str(limit), "order": order},
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    async def get_conversation(self, conversation_id: str) -> dict:
        """Get full conversation detail with message mapping."""
        raw = await self._js_with_data(
            "(async () => {"
            "  var r = await fetch('/backend-api/conversation/' + __D.conv_id, {"
            "    headers: {'Authorization': 'Bearer ' + __D.token}"
            "  });"
            "  return await r.text();"
            "})()",
            {"conv_id": conversation_id, "token": self._access_token},
            timeout=30,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    async def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation. Returns True on success."""
        result = await self._js_with_data(
            "(async () => {"
            "  try {"
            "    var r = await fetch('/backend-api/conversation/' + __D.conv_id, {"
            "      method: 'PATCH',"
            "      headers: {'Authorization': 'Bearer ' + __D.token, 'Content-Type': 'application/json'},"
            "      body: JSON.stringify({is_visible: false})"
            "    });"
            "    return r.ok ? 'true' : 'false';"
            "  } catch(e) { return 'error:' + e.message; }"
            "})()",
            {"conv_id": conversation_id, "token": self._access_token},
        )
        if result == "true":
            logger.info("Deleted conversation: %s", conversation_id)
            if self._current_conv_id == conversation_id:
                self._current_conv_id = None
            return True
        logger.warning("Failed to delete conversation %s: %s", conversation_id, result)
        return False

    async def rename_conversation(
        self, conversation_id: str, title: str
    ) -> bool:
        """Rename a conversation. Returns True on success."""
        result = await self._js_with_data(
            "(async () => {"
            "  try {"
            "    var r = await fetch('/backend-api/conversation/' + __D.conv_id, {"
            "      method: 'PATCH',"
            "      headers: {'Authorization': 'Bearer ' + __D.token, 'Content-Type': 'application/json'},"
            "      body: JSON.stringify({title: __D.title})"
            "    });"
            "    return r.ok ? 'true' : 'false';"
            "  } catch(e) { return 'error:' + e.message; }"
            "})()",
            {"conv_id": conversation_id, "token": self._access_token, "title": title},
        )
        if result == "true":
            logger.info("Renamed conversation %s to: %s", conversation_id, title)
            return True
        logger.warning("Failed to rename conversation: %s", result)
        return False

    # ── Project Management ────────────────────────────────────

    async def create_project(
        self,
        name: str,
        instructions: str = "",
        memory_scope: str = "project_v2",
    ) -> dict:
        """Create a new ChatGPT project (gizmo).

        Args:
            name: Project display name
            instructions: Custom instructions for the project
            memory_scope: 'project_v2' (dedicated) or 'global' (shared)

        Returns:
            Created project dict with id, name, etc.
        """
        raw = await self._js_with_data(
            "(async () => {"
            "  try {"
            "    var body = {"
            "      gizmo: {"
            "        display: {name: __D.name, description: ''},"
            "        memory_scope: __D.memory_scope,"
            "        memory_enabled: true,"
            "        instructions: __D.instructions,"
            "        gizmo_type: 'snorlax',"
            "        tools: [],"
            "        files: []"
            "      }"
            "    };"
            "    var r = await fetch('/backend-api/gizmos', {"
            "      method: 'POST',"
            "      headers: {'Authorization': 'Bearer ' + __D.token, 'Content-Type': 'application/json'},"
            "      body: JSON.stringify(body)"
            "    });"
            "    if (!r.ok) return JSON.stringify({error: 'HTTP ' + r.status, body: await r.text()});"
            "    var data = await r.json();"
            "    var g = (data.gizmo || data);"
            "    return JSON.stringify({"
            "      id: g.id,"
            "      name: (g.display || {}).name || '',"
            "      memory_scope: g.memory_scope || '',"
            "      instructions: g.instructions || ''"
            "    });"
            "  } catch(e) { return JSON.stringify({error: e.message}); }"
            "})()",
            {
                "token": self._access_token,
                "name": name,
                "instructions": instructions,
                "memory_scope": memory_scope,
            },
            timeout=20,
        )
        try:
            result = json.loads(raw)
            if "error" in result:
                logger.error("Create project failed: %s", result["error"])
                return result
            logger.info("Created project: %s (%s)", result.get("name"), result.get("id"))
            return result
        except json.JSONDecodeError:
            return {"error": "Invalid response"}

    async def update_project_instructions(
        self,
        project_id: str,
        instructions: str,
    ) -> bool:
        """Update a project's custom instructions. Returns True on success."""
        result = await self._js_with_data(
            "(async () => {"
            "  try {"
            "    var body = {"
            "      gizmo: {"
            "        display: {},"
            "        instructions: __D.instructions"
            "      }"
            "    };"
            "    var r = await fetch('/backend-api/gizmos/' + __D.project_id, {"
            "      method: 'PATCH',"
            "      headers: {'Authorization': 'Bearer ' + __D.token, 'Content-Type': 'application/json'},"
            "      body: JSON.stringify(body)"
            "    });"
            "    return r.ok ? 'true' : 'false';"
            "  } catch(e) { return 'error:' + e.message; }"
            "})()",
            {"token": self._access_token, "project_id": project_id, "instructions": instructions},
            timeout=15,
        )
        if result == "true":
            logger.info("Updated instructions for project: %s", project_id)
            return True
        logger.warning("Failed to update project instructions: %s", result)
        return False

    async def get_project_detail(self, project_id: str) -> dict:
        """Get full project/gizmo detail."""
        raw = await self._js_with_data(
            "(async () => {"
            "  var r = await fetch('/backend-api/gizmos/' + __D.project_id, {"
            "    headers: {'Authorization': 'Bearer ' + __D.token}"
            "  });"
            "  return await r.text();"
            "})()",
            {"token": self._access_token, "project_id": project_id},
            timeout=15,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    # ── Archive Conversation ────────────────────────────────

    async def archive_conversation(
        self, conversation_id: str, archive: bool = True
    ) -> bool:
        """Archive or unarchive a conversation. Returns True on success."""
        result = await self._js_with_data(
            "(async () => {"
            "  try {"
            "    var r = await fetch('/backend-api/conversation/' + __D.conv_id, {"
            "      method: 'PATCH',"
            "      headers: {'Authorization': 'Bearer ' + __D.token, 'Content-Type': 'application/json'},"
            "      body: JSON.stringify({is_archived: __D.archive})"
            "    });"
            "    return r.ok ? 'true' : 'false';"
            "  } catch(e) { return 'error:' + e.message; }"
            "})()",
            {"conv_id": conversation_id, "token": self._access_token, "archive": archive},
        )
        if result == "true":
            logger.info("%s conversation: %s", 'Archived' if archive else 'Unarchived', conversation_id)
            return True
        logger.warning("Failed to archive conversation: %s", result)
        return False

    # ── Memory Management ─────────────────────────────────────

    async def get_memories(self) -> list[dict]:
        """List all ChatGPT memories."""
        raw = await self._js_with_data(
            "(async () => {"
            "  try {"
            "    var r = await fetch('/backend-api/memories', {"
            "      headers: {'Authorization': 'Bearer ' + __D.token}"
            "    });"
            "    if (!r.ok) return JSON.stringify({error: 'HTTP ' + r.status});"
            "    var data = await r.json();"
            "    return JSON.stringify(data);"
            "  } catch(e) { return JSON.stringify({error: e.message}); }"
            "})()",
            {"token": self._access_token},
            timeout=15,
        )
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "error" in data:
                logger.error("Get memories failed: %s", data["error"])
                return []
            if isinstance(data, list):
                return data
            for key in ("memories", "items", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return []
        except json.JSONDecodeError:
            return []

    async def create_memory(self, content: str) -> dict:
        """Create a memory by sending a chat message asking ChatGPT to remember.

        The POST /backend-api/memories endpoint returns 405 — ChatGPT only
        creates memories through conversation. This method sends a message
        asking ChatGPT to remember the content, which triggers the memory
        system automatically.
        """
        memory_prompt = (
            f"Please remember this for all future conversations: {content}"
        )

        # Navigate to a fresh chat for memory creation
        await self.navigate_new_chat()

        # Send and collect the response
        full_response = ""
        async for chunk in self.send_and_stream(memory_prompt, timeout=60):
            if chunk.delta:
                full_response += chunk.delta

        conv_id = self._current_conv_id or ""

        logger.info("Memory creation request sent via chat (conv: %s)", conv_id)

        return {
            "content": content,
            "method": "chat",
            "conversation_id": conv_id,
            "response": full_response[:200],
            "note": (
                "Memory creation happens via chat — ChatGPT may paraphrase "
                "or decline. Use list_memories to verify."
            ),
        }

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a ChatGPT memory by ID. Returns True on success."""
        result = await self._js_with_data(
            "(async () => {"
            "  try {"
            "    var r = await fetch('/backend-api/memories/' + __D.memory_id, {"
            "      method: 'DELETE',"
            "      headers: {'Authorization': 'Bearer ' + __D.token}"
            "    });"
            "    return r.ok ? 'true' : 'false';"
            "  } catch(e) { return 'error:' + e.message; }"
            "})()",
            {"memory_id": memory_id, "token": self._access_token},
            timeout=15,
        )
        if result == "true":
            logger.info("Deleted memory: %s", memory_id)
            return True
        logger.warning("Failed to delete memory %s: %s", memory_id, result)
        return False

    # ── Custom GPT Navigation ─────────────────────────────────

    async def navigate_gpt(self, gizmo_id: str) -> None:
        """Navigate to a Custom GPT for interaction."""
        url = f"https://chatgpt.com/g/{gizmo_id}"
        logger.info("Navigate to GPT: %s", url)
        await self._cdp("Page.navigate", {"url": url})
        await asyncio.sleep(3)
        for _ in range(30):
            result = await self._js(
                "(function() {"
                "  return JSON.stringify({"
                "    ready: !!document.querySelector('#prompt-textarea'),"
                "    url: location.href"
                "  });"
                "})()",
            )
            try:
                state = json.loads(result)
                if state.get("ready"):
                    logger.info("GPT page ready: %s", state.get("url"))
                    break
            except (json.JSONDecodeError, TypeError):
                pass
            await asyncio.sleep(0.5)
        await asyncio.sleep(2)
        self._current_conv_id = None

    async def list_gpts(self) -> list[dict]:
        """List Custom GPTs (non-project gizmos).

        Projects (gizmo_type='snorlax') are excluded — use get_projects()
        for those.  Only marketplace or user-created non-project GPTs
        are returned.
        """
        raw = await self._js_with_data(
            "(async () => {"
            "  var r = await fetch('/backend-api/gizmos/snorlax/sidebar?owned_only=false&conversations_per_gizmo=0&limit=100', {"
            "    headers: {'Authorization': 'Bearer ' + __D.token}"
            "  });"
            "  var data = await r.json();"
            "  return JSON.stringify((data.items || []).map(function(i) {"
            "    var g = (i.gizmo || {}).gizmo || {};"
            "    if (g.gizmo_type === 'snorlax' && g.memory_scope) return null;"
            "    return {id: g.id, name: (g.display || {}).name || '', "
            "      description: (g.display || {}).description || '',"
            "      gizmo_type: g.gizmo_type || ''};"
            "  }).filter(Boolean));"
            "})()",
            {"token": self._access_token},
            timeout=20,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    # ── Project Files ─────────────────────────────────────────

    async def get_project_files(self, project_id: str) -> list[dict]:
        """List files attached to a ChatGPT project."""
        raw = await self._js_with_data(
            "(async () => {"
            "  try {"
            "    var r = await fetch('/backend-api/gizmos/' + __D.project_id, {"
            "      headers: {'Authorization': 'Bearer ' + __D.token}"
            "    });"
            "    if (!r.ok) return '[]';"
            "    var data = await r.json();"
            "    var gizmo = data.gizmo || data;"
            "    var files = gizmo.files || [];"
            "    return JSON.stringify(files.map(function(f) {"
            "      return {id: f.id || '', name: f.file_name || f.name || '', "
            "        size: f.size || 0, mime_type: f.mime_type || ''};"
            "    }));"
            "  } catch(e) { return '[]'; }"
            "})()",
            {"token": self._access_token, "project_id": project_id},
            timeout=15,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    # ── Token Management ──────────────────────────────────────

    async def ensure_token(self) -> str:
        """Ensure a valid access token, refreshing if needed. Returns the token."""
        if not self._access_token:
            await self._refresh_token()
        return self._access_token

    # ── Lifecycle ─────────────────────────────────────────────

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("CDP driver closed")

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"
