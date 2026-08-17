"""OneBot 11 HTTP API async client."""
from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def call_onebot_api(
    base_url: str,
    action: str,
    params: dict[str, Any] | None = None,
    access_token: str | None = None,
    timeout: float = 10,
    strict_content_type: bool = True,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{action}"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout)
    ) as session:
        async with session.post(url, json=params or {}, headers=headers) as resp:
            resp.raise_for_status()
            if strict_content_type:
                data: dict[str, Any] = await resp.json()
            else:
                data = await resp.json(content_type=None)
            if data.get("retcode", 0) != 0:
                raise RuntimeError(
                    f"OneBot API error {action}: retcode={data.get('retcode')} status={data.get('status')}"
                )
            return data


async def get_login_info(base_url: str, access_token: str | None = None) -> dict[str, Any]:
    resp = await call_onebot_api(base_url, "get_login_info", access_token=access_token)
    return resp["data"]


async def send_private_msg(
    base_url: str,
    user_id: int,
    message: list[dict],
    access_token: str | None = None,
) -> dict[str, Any]:
    resp = await call_onebot_api(
        base_url, "send_private_msg",
        {"user_id": user_id, "message": message},
        access_token=access_token,
    )
    return resp["data"]


async def send_group_msg(
    base_url: str,
    group_id: int,
    message: list[dict],
    access_token: str | None = None,
) -> dict[str, Any]:
    resp = await call_onebot_api(
        base_url, "send_group_msg",
        {"group_id": group_id, "message": message},
        access_token=access_token,
    )
    return resp["data"]


async def get_msg(
    base_url: str,
    message_id: int,
    access_token: str | None = None,
) -> dict[str, Any]:
    resp = await call_onebot_api(
        base_url, "get_msg",
        {"message_id": message_id},
        access_token=access_token,
    )
    return resp["data"]


async def upload_group_file(
    base_url: str,
    group_id: int,
    file: str,
    name: str,
    access_token: str | None = None,
) -> None:
    await call_onebot_api(
        base_url, "upload_group_file",
        {"group_id": group_id, "file": file, "name": name},
        access_token=access_token,
        timeout=60,
    )


async def upload_private_file(
    base_url: str,
    user_id: int,
    file: str,
    name: str,
    access_token: str | None = None,
) -> None:
    await call_onebot_api(
        base_url, "upload_private_file",
        {"user_id": user_id, "file": file, "name": name},
        access_token=access_token,
        timeout=60,
    )


async def upload_file_stream(
    base_url: str,
    file_path: str,
    name: str,
    access_token: str | None = None,
    chunk_size: int = 10 * 1024 * 1024,
    retention_ms: int = 30 * 60 * 1000,
) -> str:
    """Stream a host file into NapCat's container temp dir via upload_file_stream.

    NapCat's upload_private_file / upload_group_file only accept URLs or
    container-side paths — host paths fail with 识别URL失败. This helper
    uploads the file in base64 chunks and returns the container-side path.
    """
    import base64 as _b64
    import uuid as _uuid
    from pathlib import Path

    data = Path(file_path).read_bytes()
    total = max(1, (len(data) + chunk_size - 1) // chunk_size)
    stream_id = str(_uuid.uuid4())

    # 1) create stream
    await call_onebot_api(
        base_url, "upload_file_stream",
        {"stream_id": stream_id, "total_chunks": total,
         "file_size": len(data), "filename": name,
         "file_retention": retention_ms},
        access_token=access_token,
        timeout=30,
        strict_content_type=False,
    )
    # 2) upload chunks
    for i in range(total):
        chunk = data[i * chunk_size:(i + 1) * chunk_size]
        await call_onebot_api(
            base_url, "upload_file_stream",
            {"stream_id": stream_id,
             "chunk_data": _b64.b64encode(chunk).decode(),
             "chunk_index": i},
            access_token=access_token,
            timeout=120,
            strict_content_type=False,
        )
    # 3) complete → container-side path
    resp = await call_onebot_api(
        base_url, "upload_file_stream",
        {"stream_id": stream_id, "is_complete": True},
        access_token=access_token,
        timeout=60,
        strict_content_type=False,
    )
    file_path_container = resp.get("data", {}).get("file_path")
    if not file_path_container:
        raise RuntimeError(f"upload_file_stream complete: no file_path in {resp}")
    return file_path_container


# ---------- segment builders ----------

def text_segment(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}

def image_segment(file_url: str) -> dict:
    return {"type": "image", "data": {"file": file_url}}

def at_segment(qq: int | str) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}

def reply_segment(message_id: int | str) -> dict:
    return {"type": "reply", "data": {"id": str(message_id)}}

def record_segment(file_url: str) -> dict:
    return {"type": "record", "data": {"file": file_url}}

def video_segment(file_url: str) -> dict:
    return {"type": "video", "data": {"file": file_url}}
