"""NapCat (OneBot 11 reverse WebSocket) platform adapter for Hermes Agent.

Installed by ``hermes-napcat install`` to:
    gateway/platforms/napcat.py

Configuration in ~/.hermes/config.yaml:

    platforms:
      napcat:
        enabled: true
        extra:
          http_api: "http://127.0.0.1:18801"
          access_token: ""
          self_id: "123456789"
          ws_port: 18800
          dm_policy: "allowlist"     # allowlist | open | disabled
          allow_from: []             # QQ numbers allowed for DMs
          group_policy: "open"       # open | allowlist | disabled
          group_allow_from: []       # falls back to allow_from
          friend_policy: "open"      # open | allowlist | disabled (friend-add handling)
          admins: []                 # QQ numbers that can use admin-only tools
          media_max_mb: 5
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime
from typing import Any, Optional

import aiohttp
import aiohttp.web

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
)
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource

from .api import (
    call_onebot_api,
    get_file,
    get_login_info,
    get_msg,
    image_segment,
    record_segment,
    reply_segment,
    send_group_msg,
    send_private_msg,
    text_segment,
    upload_group_file,
    upload_private_file,
    upload_file_stream,
    video_segment,
)

logger = logging.getLogger(__name__)

_QQ_TEXT_LIMIT = 4500
_AUDIO_EXTS = {".mp3", ".opus", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".silk", ".amr"}
_VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".flv", ".wmv"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".ico", ".svg"}

# ── Markdown → QQ plain-text ──────────────────────────────────────────────────

def _strip_markdown(text: str) -> str:
    """Convert Markdown to clean QQ-friendly plain text.

    QQ does not render Markdown; raw syntax like **bold** or ## heading
    appears as literal characters.  This function converts the most common
    constructs to readable Unicode equivalents.
    """
    lines = text.splitlines()
    out: list[str] = []
    in_code = False
    code_lang = ""
    code_lines: list[str] = []

    for line in lines:
        # ── fenced code blocks ────────────────────────────────────────────
        fence = re.match(r"^(`{3,}|~{3,})(.*)", line.strip())
        if fence:
            if not in_code:
                in_code = True
                code_lang = fence.group(2).strip()
                code_lines = []
            else:
                in_code = False
                block = "\n".join(code_lines)
                label = f"[{code_lang}]" if code_lang else "[代码]"
                out.append(f"┌─{label}─")
                for cl in code_lines:
                    out.append("│ " + cl)
                out.append("└──────")
                code_lines = []
            continue
        if in_code:
            code_lines.append(line)
            continue

        # ── headings ──────────────────────────────────────────────────────
        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            level, title = len(h.group(1)), h.group(2).strip()
            title = _inline(title)
            if level <= 2:
                out.append(f"【{title}】")
            else:
                out.append(f"▌ {title}")
            continue

        # ── horizontal rules ──────────────────────────────────────────────
        if re.match(r"^\s*[-*_]{3,}\s*$", line):
            out.append("────────────────")
            continue

        # ── blockquotes ───────────────────────────────────────────────────
        bq = re.match(r"^>\s?(.*)", line)
        if bq:
            out.append("「" + _inline(bq.group(1)) + "」")
            continue

        # ── unordered lists ───────────────────────────────────────────────
        ul = re.match(r"^(\s*)[-*+]\s+(.*)", line)
        if ul:
            indent = len(ul.group(1)) // 2
            out.append("  " * indent + "• " + _inline(ul.group(2)))
            continue

        # ── ordered lists ─────────────────────────────────────────────────
        ol = re.match(r"^(\s*)\d+[.)]\s+(.*)", line)
        if ol:
            indent = len(ol.group(1)) // 2
            num = re.match(r"^\s*(\d+)", line).group(1)
            out.append("  " * indent + num + ". " + _inline(ol.group(2)))
            continue

        # ── table rows ────────────────────────────────────────────────────
        if re.match(r"^\s*\|", line):
            # Skip separator rows (|---|---|)
            if re.match(r"^\s*\|[\s\-:|]+\|\s*$", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("  ".join(_inline(c) for c in cells if c))
            continue

        # ── normal line ───────────────────────────────────────────────────
        out.append(_inline(line))

    return "\n".join(out).strip()


def _inline(text: str) -> str:
    """Strip inline Markdown from a single line."""
    # inline code: `code`
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    # bold+italic: ***text*** or ___text___
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text)
    # bold: **text** or __text__
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text)
    # italic: *text* or _text_  (only word-boundary _ to avoid false positives)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    # strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    # links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", text)
    # images: ![alt](url)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"[\1]", text)
    # bare reference-style links: [text][ref]
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    return text


def _file_ext(url: str) -> str:
    path = url.split("?")[0]
    dot = path.rfind(".")
    return path[dot:].lower() if dot != -1 else ""


def _classify_media(url: str) -> str:
    ext = _file_ext(url)
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _IMAGE_EXTS:
        return "image"
    return "file"


def _extract_text(segments: list[dict]) -> str:
    parts = []
    for s in segments:
        if s["type"] == "text":
            parts.append(s["data"].get("text", ""))
        elif s["type"] == "at":
            parts.append(f"@{s['data'].get('qq', '')}")
    return "".join(parts).strip()


def _extract_images(segments: list[dict]) -> list[str]:
    return [
        s["data"].get("url") or s["data"].get("file", "")
        for s in segments if s["type"] == "image"
        if s["data"].get("url") or s["data"].get("file")
    ]


def _extract_record(segments: list[dict]) -> str | None:
    for s in segments:
        if s["type"] == "record":
            return s["data"].get("url") or s["data"].get("file")
    return None


def _extract_files(segments: list[dict]) -> list[dict]:
    files = []
    for s in segments:
        if s["type"] == "file":
            d = s.get("data", {})
            files.append(
                {
                    "name": d.get("file", "") or d.get("name", "") or "未知文件",
                    "file_id": d.get("file_id", ""),
                    "size": d.get("file_size", ""),
                }
            )
    return files


# NapCat docker: /app/.config/QQ is bind-mounted to /opt/napcat/.config
_CONTAINER_MOUNT = "/app/.config/QQ"
_HOST_MOUNT = "/opt/napcat/.config"


async def _resolve_private_file(
    api_base: str,
    file_id: str,
    access_token: str | None,
) -> str | None:
    """Map a OneBot file_id to a host-readable path (NapCat docker layout)."""
    if not file_id:
        return None
    try:
        data = await get_file(api_base, file_id, access_token)
    except Exception as exc:
        logger.debug("NapCat: get_file failed: %s", exc)
        return None
    cpath = data.get("file") or data.get("url") or ""
    if not cpath:
        return None
    if cpath.startswith(_CONTAINER_MOUNT):
        hpath = _HOST_MOUNT + cpath[len(_CONTAINER_MOUNT):]
        if os.path.exists(hpath):
            return hpath
    return None


def _extract_reply_id(segments: list[dict]) -> int | None:
    for s in segments:
        if s["type"] == "reply":
            try:
                return int(s["data"]["id"])
            except (KeyError, ValueError):
                pass
    return None


def _extract_reply_text(segments: list[dict]) -> str | None:
    """Return the quoted text embedded in a reply segment, if any.

    NapCat patch: the reply segment may carry a `text` field with the quoted
    message's content, which avoids the (often failing) get_msg lookup.
    """
    for s in segments:
        if s["type"] == "reply":
            t = (s.get("data") or {}).get("text")
            if t:
                return str(t)
    return None


def _has_bot_mention(segments: list[dict], self_id: str) -> bool:
    return any(
        s["type"] == "at" and str(s["data"].get("qq")) == self_id
        for s in segments
    )


def _strip_bot_mention(segments: list[dict], self_id: str) -> list[dict]:
    return [
        s for s in segments
        if not (s["type"] == "at" and str(s["data"].get("qq")) == self_id)
    ]


def _chunk_text(text: str, limit: int = _QQ_TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split = text.rfind("\n", 0, limit)
        if split <= 0:
            split = text.rfind(" ", 0, limit)
        if split <= 0:
            split = limit
        chunks.append(text[:split])
        text = text[split:].lstrip("\n")
    return chunks


def _resolve_voice_path(url: str) -> str | None:
    """Map NapCat in-container paths to host paths (docker bind mounts).

    NapCat record segments report the file as an in-container absolute path
    (e.g. /app/.config/QQ/...) which the Hermes process (host) cannot HTTP-GET
    nor open directly. The docker deployment bind-mounts /opt/napcat/.config
    to /app/.config/QQ, so we rewrite the prefix and read the file locally.
    """
    if not url:
        return None
    p = url[7:] if url.startswith("file://") else url
    mappings = (
        ("/app/.config/QQ", "/opt/napcat/.config"),  # QQ data bind mount
    )
    for prefix, host_prefix in mappings:
        if p.startswith(prefix):
            host = host_prefix + p[len(prefix):]
            if os.path.exists(host):
                return host
    if os.path.exists(p):
        return p
    return None


async def _download_and_convert_wav(url: str, max_bytes: int, http_api: str = "") -> str | None:
    try:
        local = _resolve_voice_path(url)
        logger.debug("NapCat: voice url=%s local=%s max=%d", url, local, max_bytes)
        if local:
            with open(local, "rb") as f:
                data = f.read()
        else:
            data = None
            # In-container path not yet visible on the host: ask NapCat to
            # convert the record via get_record, then read it immediately —
            # QQ Ptt files under /app/.config/QQ are temporary and may be
            # cleaned up seconds after the event arrives.
            if url.startswith("/app/") and http_api:
                basename = url.rsplit("/", 1)[-1]
                try:
                    from .api import call_onebot_api
                    resp = await call_onebot_api(http_api, "get_record", {"file": basename, "out_format": "wav"})
                    wav_path = (resp.get("data") or {}).get("file") or ""
                    wav_local = _resolve_voice_path(wav_path)
                    if wav_local:
                        # Copy to a stable temp path: NapCat may clean Ptt files anytime
                        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
                        os.close(fd)
                        with open(wav_local, "rb") as f:
                            data = f.read()
                        with open(tmp_path, "wb") as f:
                            f.write(data)
                        logger.info("NapCat: voice get_record -> %d bytes (copied)", len(data))
                        if len(data) > max_bytes:
                            os.unlink(tmp_path)
                            return None
                        return tmp_path
                except Exception as exc:
                    logger.info("NapCat: voice get_record failed: %s", exc)
            if data is None:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        data = await resp.read()
        logger.debug("NapCat: voice data=%d bytes", len(data))
        if len(data) > max_bytes:
            return None
        fd, in_path = tempfile.mkstemp(suffix=".silk")
        os.close(fd)
        out_path = in_path.replace(".silk", ".wav")
        with open(in_path, "wb") as f:
            f.write(data)
        # 1) Try ffmpeg first (handles standard formats: mp3/ogg/wav...)
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", in_path, "-ar", "16000", "-ac", "1", "-f", "wav", out_path],
            capture_output=True, timeout=15,
        )
        logger.debug("NapCat: voice ffmpeg rc=%d", result.returncode)
        if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 44:
            # 2) QQ voice is SILK v3 with a 0x02-prefixed header — ffmpeg has no
            #    SILK decoder, so fall back to pilk (silk_to_wav).
            try:
                import pilk
                pilk.silk_to_wav(in_path, out_path, rate=16000)
                logger.debug("NapCat: voice pilk ok size=%d", os.path.getsize(out_path))
            except Exception as exc:
                logger.debug("NapCat: voice pilk failed: %s", exc)
                os.unlink(in_path)
                return None
        os.unlink(in_path)
        if not os.path.exists(out_path) or os.path.getsize(out_path) <= 44:
            return None
        return out_path
    except Exception as exc:
        logger.debug("Voice download/convert failed: %s", exc)
        return None


def check_napcat_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


def _coerce_qid_set(raw) -> set[str]:
    """Turn a QQ-id allowlist into a set of str, accepting a real list, a comma
    string ("123,456"), or a JSON-string list ('["123","456"]')."""
    import json as _json
    if raw is None:
        return set()
    if isinstance(raw, str):
        txt = raw.strip()
        if txt.startswith("[") and txt.endswith("]"):
            try:
                return {str(x).strip() for x in _json.loads(txt)}
            except Exception:
                pass
        if txt:
            return {x.strip() for x in txt.split(",") if x.strip()}
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return {str(raw).strip()} if str(raw).strip() else set()


class NapCatAdapter(BasePlatformAdapter):
    """Hermes platform adapter for QQ via NapCat (OneBot 11 reverse WebSocket).

    NapCat dials **out** to the WS server we start here; we reply via
    NapCat's HTTP API.
    """

    MAX_MESSAGE_LENGTH = _QQ_TEXT_LIMIT

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform("napcat"))
        extra: dict[str, Any] = getattr(config, "extra", {}) or {}

        self._http_api: str = extra.get("http_api", "").rstrip("/")
        self._access_token: str = extra.get("access_token", "") or ""
        raw_self_id = str(extra.get("self_id", ""))
        # Treat placeholder values as empty so HTTP probe fills in real QQ
        self._self_id: str = "" if raw_self_id in ("YOUR_QQ_NUMBER", "YOURQQ_NUMBER") else raw_self_id
        self._ws_port: int = int(extra.get("ws_port", 18800))
        self._dm_policy: str = extra.get("dm_policy", "allowlist")
        self._allow_from: list[str] = [str(x) for x in extra.get("allow_from", [])]
        self._group_policy: str = extra.get("group_policy", "open")
        self._group_allow_from: list[str] = [str(x) for x in extra.get("group_allow_from", [])]
        # Friend-add policy.  open     = auto-accept everyone (default).
        #                   allowlist = only accept QQ numbers in allow_from/admins.
        #                   disabled  = never auto-handle (log only, no accept/reject).
        self._friend_policy: str = str(extra.get("friend_policy", "open")).lower()
        self._media_max_mb: int = int(extra.get("media_max_mb", 5))
        self._admins: list[str] = [str(x) for x in extra.get("admins", [])]
        # Robust friend allowlist: extra config may hold admins/allow_from as either
        # a real list, a comma string ("123,456"), or a JSON-string list
        # ('["123","456"]').  Parse all three so the allowlist check works regardless.
        self._friend_allow: set[str] = (
            _coerce_qid_set(extra.get("admins")) | _coerce_qid_set(extra.get("allow_from"))
        )
        # Whether to echo a "reply" segment pointing at the incoming message.
        # off (default) = send fresh top-level messages; first = reply only
        # when replying to a quoted message; all = always reply.
        self._reply_to_mode: str = str(extra.get("reply_to_mode", "off")).lower()

        self._runner: aiohttp.web.AppRunner | None = None
        self._active_ws: set[aiohttp.web.WebSocketResponse] = set()

        # Wire up qq_tool so the agent can call QQ APIs directly
        try:
            from . import qq_tool as _qq_tool
            _qq_tool._init(self._http_api, self._access_token, self._admins)
        except ImportError:
            pass

    # ── Connection ─────────────────────────────────────────────────────────

    async def connect(self, is_reconnect: bool = False) -> bool:
        if not self._http_api:
            logger.error("NapCat: http_api is not configured")
            return False

        app = aiohttp.web.Application()
        app.router.add_get("/", self._ws_handler)
        self._runner = aiohttp.web.AppRunner(app)
        await self._runner.setup()
        site = aiohttp.web.TCPSite(self._runner, "0.0.0.0", self._ws_port)
        await site.start()
        self._is_connected = True
        logger.info("NapCat: reverse WS listening on ws://0.0.0.0:%d", self._ws_port)

        try:
            info = await get_login_info(self._http_api, self._access_token or None)
            if not self._self_id:
                self._self_id = str(info.get("user_id", ""))
            logger.info(
                "NapCat: bot is %s (QQ:%s)",
                info.get("nickname", "?"), info.get("user_id", "?"),
            )
        except Exception as exc:
            logger.warning("NapCat: HTTP probe failed (WS still running): %s", exc)

        return True

    async def disconnect(self) -> None:
        self._is_connected = False
        for ws in list(self._active_ws):
            await ws.close()
        self._active_ws.clear()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("NapCat: disconnected")

    # ── Inbound WS handler ─────────────────────────────────────────────────

    async def _ws_handler(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        self._active_ws.add(ws)
        logger.info("NapCat WS connected from %s", request.remote)
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    asyncio.create_task(self._handle_raw(msg.data))
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            self._active_ws.discard(ws)
            logger.info("NapCat WS disconnected")
        return ws

    async def _handle_raw(self, raw: str) -> None:
        try:
            data: dict = json.loads(raw)
        except json.JSONDecodeError:
            return
        if data.get("post_type") == "request":
            await self._handle_request(data)
            return
        if data.get("post_type") != "message":
            return
        try:
            await self._process_message(data)
        except Exception:
            logger.exception("NapCat: error processing message")

    async def _process_message(self, event: dict) -> None:
        is_group = event.get("message_type") == "group"
        sender_id = str(event.get("user_id", ""))
        sender = event.get("sender", {})
        sender_name: str = sender.get("card") or sender.get("nickname") or sender_id
        group_id = str(event.get("group_id", "")) if is_group else ""
        chat_id = f"group:{group_id}" if is_group else sender_id
        segments: list[dict] = event.get("message", [])

        # Group: require @bot mention
        if is_group:
            if self._self_id and not _has_bot_mention(segments, self._self_id):
                return
            if self._self_id:
                segments = _strip_bot_mention(segments, self._self_id)

        # Authorization
        if is_group:
            if self._group_policy == "disabled":
                return
            if self._group_policy == "allowlist":
                effective = self._group_allow_from or self._allow_from
                if effective and sender_id not in effective:
                    return
        else:
            if self._dm_policy == "disabled":
                return
            if self._dm_policy == "allowlist":
                if self._allow_from and sender_id not in self._allow_from:
                    return

        text = _extract_text(segments)
        image_urls = _extract_images(segments)
        record_url = _extract_record(segments)

        # In group chats prefix every message with the sender's name so the
        # AI can tell participants apart when the group shares one session.
        # Skip the prefix for slash commands so the gateway can detect them
        # correctly — is_command() checks text.startswith("/").
        if is_group and text:
            if text.lstrip().startswith("/"):
                text = text.lstrip()  # preserve slash command, sender is in channel_prompt
            else:
                text = f"[{sender_name}]: {text}"

        # Fetch quoted message text for reply context
        reply_id = _extract_reply_id(event.get("message", []))
        reply_text: str | None = None
        if reply_id:
            # NapCat patch: reply segment may carry the quoted text directly
            # (NapCat's get_msg lookup fails for private replies, so prefer
            # the inline text when present).
            seg_text = _extract_reply_text(event.get("message", []))
            if seg_text:
                reply_text = f"[引用消息]: {seg_text}"
                text = f"[引用消息: {seg_text}]\n{text}"
            else:
                try:
                    quoted = await get_msg(self._http_api, reply_id, self._access_token or None)
                    q_sender = quoted.get("sender", {})
                    q_name = (
                        q_sender.get("card")
                        or q_sender.get("nickname")
                        or str(q_sender.get("user_id", ""))
                    )
                    q_text = _extract_text(quoted.get("message", []))
                    if q_text:
                        reply_text = f"[{q_name}]: {q_text}"
                        text = f"[引用 {q_name} 的消息: {q_text}]\n{text}"
                except Exception:
                    pass

        # Determine MessageType and media
        media_urls: list[str] = []
        media_types: list[str] = []
        msg_type = MessageType.TEXT

        if image_urls:
            msg_type = MessageType.PHOTO
            max_bytes = self._media_max_mb * 1024 * 1024
            for idx, url in enumerate(image_urls):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url) as resp:
                            resp.raise_for_status()
                            img_data = await resp.read()
                    if len(img_data) <= max_bytes:
                        if idx == 0:  # cache first image for vision tool
                            cached = cache_image_from_bytes(img_data)
                            media_urls.append(cached)
                            media_types.append("image/jpeg")
                    else:
                        # Over-limit: keep the remote URL so the agent can still use it
                        text = (text + f"\n[图片 {len(img_data)//1024}KB 超过 {self._media_max_mb}MB 上限, 远程URL: {url}]").strip()
                except Exception as exc:
                    logger.debug("NapCat: image download failed: %s", exc)
                    text = (text + f"\n[图片下载失败, 远程URL: {url}]").strip()

        elif record_url:
            msg_type = MessageType.VOICE
            max_bytes = self._media_max_mb * 1024 * 1024
            wav = await _download_and_convert_wav(record_url, max_bytes, self._http_api)
            if wav:
                media_urls.append(wav)
                media_types.append("audio/wav")
                logger.debug("NapCat: voice -> %s", wav)
            else:
                # Over-limit or conversion failure: keep the remote URL
                text = (text + f"\n[语音超过 {self._media_max_mb}MB 上限或转换失败, 远程URL: {record_url}]").strip()

        # File messages: resolve file_id to a host path so the agent can
        # read the file on disk.
        for f in _extract_files(segments):
            hpath = await _resolve_private_file(
                self._http_api, f["file_id"], self._access_token or None
            )
            if hpath:
                text = (text + f"\n[收到文件: {f['name']} 大小{f['size']}字节, 本地路径: {hpath}]").strip()
            else:
                text = (text + f"\n[收到文件: {f['name']} 大小{f['size']}字节, 但下载失败(file_id={f['file_id']})]").strip()

        if not text and not media_urls:
            return

        source = SessionSource(
            platform=Platform("napcat"),
            chat_id=chat_id,
            chat_name=sender_name if not is_group else group_id,
            chat_type="group" if is_group else "dm",
            user_id=sender_id,
            user_name=sender_name,
        )

        is_admin = sender_id in self._admins
        role_label = "管理员" if is_admin else "用户"
        permission_prompt = (
            f"[{role_label}] QQ:{sender_id}。"
            "你现在运行在本机 Hermes 环境，拥有完整本地工具访问权限。"
            "可直接调用：terminal（执行 shell 命令）、read_file（读取本机文件）、"
            "write_file、web_search、browser、vision_analyze 等所有工具。"
            "读取文件、查看日志、执行查询等只读操作直接执行，无需确认。"
            "仅对真正不可逆的操作（删除文件、踢人、禁言、修改配置等）需先说明再执行。"
        )

        message_event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            raw_message=event,
            message_id=str(event.get("message_id", "")),
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=str(reply_id) if reply_id else None,
            reply_to_text=reply_text,
            timestamp=datetime.fromtimestamp(event["time"]) if event.get("time") else datetime.now(),
            channel_prompt=permission_prompt,
        )

        # Set per-message context so admin-gated tools know who is asking
        try:
            from . import qq_tool as _qq_tool
            _qq_tool._set_context(sender_id, is_admin=is_admin)
        except ImportError:
            pass

        await self.handle_message(message_event)

    async def _handle_request(self, data: dict) -> None:
        """Handle non-message post_types, mainly friend requests.

        OneBot11 request events carry post_type='request'. Friend requests
        have request_type='friend'. We auto-accept incoming friend requests so
        the bot can be added normally. Group join/invite requests we only log.
        """
        request_type = data.get("request_type")
        if request_type == "friend":
            flag = data.get("flag", "")
            user_id = str(data.get("user_id", ""))
            if self._friend_policy == "open":
                approve = True
            elif self._friend_policy == "allowlist":
                approve = user_id in self._friend_allow
            else:  # disabled
                logger.info("NapCat: friend request from %s — friend_policy=disabled, ignoring", user_id)
                return
            logger.info(
                "NapCat: friend request from %s (flag=%s) — %s",
                user_id, flag, "accepting" if approve else "rejecting (not in allowlist)",
            )
            try:
                from .api import call_onebot_api
                resp = await call_onebot_api(
                    self._http_api, "set_friend_add_request",
                    {"flag": flag, "approve": approve},
                    self._access_token or None,
                )
                logger.info("NapCat: friend request handled, api=%s", resp)
            except Exception:
                logger.exception("NapCat: failed to handle friend request")
        else:
            logger.info("NapCat: ignoring %s request event (only friend requests are auto-handled)", request_type)

    # ── Outbound ───────────────────────────────────────────────────────────

    def _parse_chat_id(self, chat_id: str) -> tuple[bool, int]:
        if chat_id.startswith("group:"):
            return True, int(chat_id[6:])
        return False, int(chat_id)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        try:
            is_group, num_id = self._parse_chat_id(chat_id)
            chunks = _chunk_text(_strip_markdown(content))
            last_id: str | None = None
            # reply_to_mode governs whether we attach a "reply" segment:
            #   off (default)  -> send fresh top-level message
            #   first          -> reply only when replying to a quoted msg
            #   all            -> always reply to the triggering message
            use_reply = self._reply_to_mode == "all" or (
                self._reply_to_mode == "first" and reply_to
            )
            for i, chunk in enumerate(chunks):
                segs: list[dict] = []
                if i == 0 and use_reply and reply_to:
                    try:
                        segs.append(reply_segment(int(reply_to)))
                    except (ValueError, TypeError):
                        pass
                segs.append(text_segment(chunk))
                if is_group:
                    r = await send_group_msg(self._http_api, num_id, segs, self._access_token or None)
                else:
                    r = await send_private_msg(self._http_api, num_id, segs, self._access_token or None)
                last_id = str(r.get("message_id", ""))
            return SendResult(success=True, message_id=last_id)
        except Exception as exc:
            logger.error("NapCat send error: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        try:
            is_group, num_id = self._parse_chat_id(chat_id)
            segs: list[dict] = [image_segment(image_url)]
            if caption:
                segs.append(text_segment(caption))
            if is_group:
                r = await send_group_msg(self._http_api, num_id, segs, self._access_token or None)
            else:
                r = await send_private_msg(self._http_api, num_id, segs, self._access_token or None)
            return SendResult(success=True, message_id=str(r.get("message_id", "")))
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        metadata: dict | None = None,
    ) -> SendResult:
        try:
            is_group, num_id = self._parse_chat_id(chat_id)
            segs = [record_segment(audio_path)]
            if is_group:
                r = await send_group_msg(self._http_api, num_id, segs, self._access_token or None)
            else:
                r = await send_private_msg(self._http_api, num_id, segs, self._access_token or None)
            return SendResult(success=True, message_id=str(r.get("message_id", "")))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        metadata: dict | None = None,
    ) -> SendResult:
        try:
            is_group, num_id = self._parse_chat_id(chat_id)
            segs = [video_segment(video_path)]
            if is_group:
                r = await send_group_msg(self._http_api, num_id, segs, self._access_token or None)
            else:
                r = await send_private_msg(self._http_api, num_id, segs, self._access_token or None)
            return SendResult(success=True, message_id=str(r.get("message_id", "")))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        filename: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        try:
            is_group, num_id = self._parse_chat_id(chat_id)
            name = filename or os.path.basename(file_path)
            # NapCat only accepts URLs or container-side paths — stream host
            # files into the container temp dir first (fixes 识别URL失败).
            if file_path and not file_path.startswith(("http://", "https://", "file://")) \
                    and os.path.isfile(file_path):
                file_path = await upload_file_stream(
                    self._http_api, file_path, name, self._access_token or None,
                )
            if is_group:
                await upload_group_file(self._http_api, num_id, file_path, name, self._access_token or None)
            else:
                await upload_private_file(self._http_api, num_id, file_path, name, self._access_token or None)
            return SendResult(success=True)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def get_chat_info(self, chat_id: str) -> dict:
        try:
            is_group, num_id = self._parse_chat_id(chat_id)
            if is_group:
                resp = await call_onebot_api(
                    self._http_api, "get_group_info",
                    {"group_id": num_id, "no_cache": True},
                    self._access_token or None,
                )
                g = resp["data"]
                return {"name": g.get("group_name", str(num_id)), "type": "group", "chat_id": chat_id}
            else:
                resp = await call_onebot_api(
                    self._http_api, "get_stranger_info",
                    {"user_id": num_id, "no_cache": True},
                    self._access_token or None,
                )
                u = resp["data"]
                return {"name": u.get("nickname", str(num_id)), "type": "dm", "chat_id": chat_id}
        except Exception as exc:
            return {"name": chat_id, "type": "unknown", "error": str(exc), "chat_id": chat_id}

    async def format_message(self, content: str) -> str:
        return _strip_markdown(content)

    async def send_typing(self, chat_id: str, metadata: dict | None = None) -> None:
        # QQ OneBot: NapCat exposes set_input_status(event_type, user_id).
        #   event_type 1 = "对方正在输入" (typing bubble shown)
        #   event_type 0 = "对方正在说话" (speaking status, verified live 2026-08-08)
        # Typing only applies to private chats — groups have no typing bubble.
        # Marker: _napcat_typing_indicator
        try:
            if chat_id.startswith("group:"):
                return
            await call_onebot_api(
                self._http_api,
                "set_input_status",
                {"user_id": int(chat_id), "event_type": 1},
                self._access_token or None,
                timeout=3,
            )
        except Exception:
            logger.debug("NapCat: send_typing failed (non-fatal)", exc_info=True)

    async def stop_typing(self, chat_id: str) -> None:
        # event_type 0 = "对方正在说话" (speaking status, NOT a clear).
        # QQ's input-status event_type has no dedicated "clear" value; the
        # bubble times out on its own. Sending 0 here would wrongly show the
        # peer as "speaking", so this is left as a no-op for QQ.
        try:
            if chat_id.startswith("group:"):
                return
        except Exception:
            logger.debug("NapCat: stop_typing failed (non-fatal)", exc_info=True)


# ── Standalone delivery (cron out-of-process push) ──────────────────────────
#
# Registered via the plugin's root __init__.py register() → ctx.register_platform
# as `standalone_sender_fn`.  Hermes' cron delivery preflight
# (`_is_known_delivery_platform`) accepts a plugin platform when its
# PlatformEntry declares `cron_deliver_env_var` — without it,
# `deliver=napcat:xxx` cron jobs are rejected as an unknown platform even
# though the gateway adapter works fine.
#
# Marker: _napcat_plugin_registration


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Out-of-process push delivery for cron jobs running detached from the gateway.

    Without this hook, ``deliver=napcat`` cron jobs fail with "No live adapter
    for platform 'napcat'" when cron runs as its own process.  Sends via the
    OneBot HTTP API directly (same path as NapCatAdapter.send) so no gateway
    WebSocket session is required.

    ``thread_id`` is accepted for signature parity but ignored — QQ has no
    thread primitive.  ``media_files`` are not deliverable from this path
    (requires a publicly reachable URL); a text hint is appended instead.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    http_api = str(extra.get("http_api", "") or "").rstrip("/")
    access_token = str(extra.get("access_token", "") or "") or None
    if not http_api:
        return {"error": "NapCat standalone send: missing http_api in platform config"}
    if not chat_id:
        return {"error": "NapCat standalone send: missing chat_id"}

    is_group = chat_id.startswith("group:")
    num_id = int(chat_id[6:]) if is_group else int(chat_id)

    parts = _chunk_text(_strip_markdown(message or ""))
    if media_files:
        parts.append(f"[{len(media_files)} attachment(s) generated; not deliverable from cron]")

    last_id: str | None = None
    try:
        for chunk in parts:
            segs: list[dict] = [text_segment(chunk)]
            if is_group:
                r = await send_group_msg(http_api, num_id, segs, access_token)
            else:
                r = await send_private_msg(http_api, num_id, segs, access_token)
            last_id = str(r.get("message_id", ""))
        return {"success": True, "message_id": last_id}
    except Exception as exc:
        logger.error("NapCat standalone send error: %s", exc)
        return {"error": str(exc)}


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="napcat",
        label="NapCat (QQ)",
        adapter_factory=lambda cfg: NapCatAdapter(cfg),
        check_fn=check_napcat_requirements,
        validate_config=None,
        required_env=[],
        install_hint=(
            "NapCat 容器: docker run -d --name napcat --net host "
            "-e NAPCAT_UID=1000 -e NAPCAT_GID=1000 "
            "mlikiowa/napcat-docker 详见 README"
        ),
        # Declare cron delivery support: lets `deliver=napcat:xxx` jobs pass
        # preflight without hardcoding "napcat" into core's whitelist.
        cron_deliver_env_var="NAPCAT_HOME_CHANNEL",
        # Out-of-process cron delivery via OneBot HTTP (no gateway WS needed).
        standalone_sender_fn=_standalone_send,
    )
