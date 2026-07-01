#!/usr/bin/env python3
"""
Claude Desktop vision rewrite proxy.

This proxy captures Anthropic Messages requests, rewrites image blocks into text
descriptions, writes request dumps for inspection, and forwards the rewritten
body to the configured upstream.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx


DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",
    "listen_port": 8787,
    "upstream_base_url": "https://api.deepseek.com",
    "strip_path_prefix": "",
    "upstream_path_prefix": "",
    "model_map": {},
    "served_models": [
        "claude-sonnet-4-6",
        "claude-opus-4-8",
        "claude-haiku-4-5",
    ],
    "dump_dir": "~/.claude/vision_proxy_http/dumps",
    "log_file": "~/.claude/vision_proxy_http/proxy.log",
    "copy_timestamped_dumps": True,
    "vision_enabled": True,
    "vision_provider": "modelscope",
    "vision_base_url": "https://api-inference.modelscope.cn/v1",
    "vision_base_urls": {
        "modelscope": "https://api-inference.modelscope.cn/v1",
        "siliconflow": "https://api.siliconflow.cn/v1",
    },
    "vision_api_key_envs": {
        "modelscope": "MODELSCOPE_API_KEY",
        "siliconflow": "GUIJILIUDONG_API_KEY",
    },
    "vision_model": "Qwen/Qwen3-VL-235B-A22B-Instruct",
    "vision_model_aliases": {
        "paddleocr": "PaddlePaddle/PaddleOCR-VL-1.5",
        "qwen3-vl-8b": "Qwen/Qwen3-VL-8B-Instruct",
        "qwen3-vl-32b": "Qwen/Qwen3-VL-32B-Instruct",
    },
    "vision_timeout_seconds": 45,
    "max_image_bytes": 8000000,
    "image_cache_path": "~/.claude/vision_proxy_http/cache/image_descriptions.json",
    "claude_3p_config_dir": "~/AppData/Local/Claude-3p/configLibrary",
    "claude_3p_provider_id": "00000000-0000-4000-8000-000000157210",
    "claude_3p_provider_name": "Vision Proxy",
    "proxy_public_url": "http://127.0.0.1:9980/anthropic",
}

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

SENSITIVE_HEADER_WORDS = ("authorization", "api-key", "x-api-key", "token", "key")
VISION_PROMPT = (
    "Describe this image in detail. If it contains code, UI, error messages, "
    "architecture diagrams, terminal output, or other technical content, describe "
    "all visible text, structure, and visual relationships as precisely as possible. "
    "Answer in Chinese."
)


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def load_config(path: str | None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path:
        config_path = expand_path(path)
        with config_path.open("r", encoding="utf-8") as f:
            file_config = json.load(f)
        config.update(file_config)
        config["_config_path"] = str(config_path)

    env_upstream = os.getenv("VISION_PROXY_UPSTREAM_BASE_URL")
    if env_upstream:
        config["upstream_base_url"] = env_upstream

    return config


def write_json_no_bom(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def save_runtime_config(config: dict[str, Any]) -> None:
    config_path_value = config.get("_config_path")
    if not isinstance(config_path_value, str) or not config_path_value:
        raise RuntimeError("config path is not available")
    config_path = expand_path(config_path_value)
    data = {key: value for key, value in config.items() if not key.startswith("_")}
    write_json_no_bom(config_path, data)


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if any(word in lower for word in SENSITIVE_HEADER_WORDS):
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted


def hash_image(media_type: str, base64_data: str, namespace: str = "") -> str:
    prefix = f"{namespace}:" if namespace else ""
    digest = hashlib.sha256(f"{prefix}{media_type}:{base64_data}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def load_image_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("failed to load image cache path=%s", cache_path)
        return {}
    return data if isinstance(data, dict) else {}


def save_image_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_name(f"{cache_path.name}.tmp")
    tmp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(cache_path)


def short_error(exc: BaseException | str) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else "unknown error"
    return message[:200]


def unavailable_image_description(reason: str) -> str:
    return f"[Image Description unavailable: {reason}]"


def extract_choice_text(response_payload: Any) -> str:
    choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ValueError("vision response missing choices")
    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message = first_choice.get("message") if isinstance(first_choice.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    raise ValueError("vision response missing message content")


def get_vision_provider(config: dict[str, Any]) -> str:
    return str(config.get("vision_provider", DEFAULT_CONFIG["vision_provider"])).strip().lower()


def get_vision_model(config: dict[str, Any]) -> str:
    model = str(config.get("vision_model", DEFAULT_CONFIG["vision_model"])).strip()
    aliases = config.get("vision_model_aliases", DEFAULT_CONFIG["vision_model_aliases"])
    if isinstance(aliases, dict):
        mapped = aliases.get(model.lower())
        if isinstance(mapped, str) and mapped:
            return mapped
    return model


def get_vision_base_url(config: dict[str, Any], provider: str) -> str:
    base_urls = config.get("vision_base_urls", DEFAULT_CONFIG["vision_base_urls"])
    if isinstance(base_urls, dict):
        provider_base_url = base_urls.get(provider)
        if isinstance(provider_base_url, str) and provider_base_url:
            return provider_base_url.rstrip("/")
    return str(config.get("vision_base_url", DEFAULT_CONFIG["vision_base_url"])).rstrip("/")


def get_vision_api_key_env(config: dict[str, Any], provider: str) -> str:
    api_key_envs = config.get("vision_api_key_envs", DEFAULT_CONFIG["vision_api_key_envs"])
    if isinstance(api_key_envs, dict):
        env_name = api_key_envs.get(provider)
        if isinstance(env_name, str) and env_name:
            return env_name
    return "MODELSCOPE_API_KEY"


def get_cache_info(config: dict[str, Any]) -> dict[str, Any]:
    cache_path = expand_path(str(config.get("image_cache_path", DEFAULT_CONFIG["image_cache_path"])))
    cache = load_image_cache(cache_path)
    size_bytes = cache_path.stat().st_size if cache_path.exists() else 0
    return {
        "path": str(cache_path),
        "exists": cache_path.exists(),
        "entries": len(cache),
        "size_bytes": size_bytes,
    }


def clear_image_cache(config: dict[str, Any]) -> dict[str, Any]:
    cache_path = expand_path(str(config.get("image_cache_path", DEFAULT_CONFIG["image_cache_path"])))
    if cache_path.exists():
        cache_path.unlink()
    return get_cache_info(config)


def tail_text(path: Path, max_lines: int = 80) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except Exception as exc:
        return [f"failed to read {path}: {short_error(exc)}"]


def read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def claude_3p_paths(config: dict[str, Any]) -> dict[str, Path]:
    config_dir = expand_path(str(config.get("claude_3p_config_dir", DEFAULT_CONFIG["claude_3p_config_dir"])))
    provider_id = str(config.get("claude_3p_provider_id", DEFAULT_CONFIG["claude_3p_provider_id"]))
    return {
        "config_dir": config_dir,
        "meta": config_dir / "_meta.json",
        "provider": config_dir / f"{provider_id}.json",
        "backup_root": config_dir / "vision-proxy-backups",
    }


def build_claude_3p_inference_models(config: dict[str, Any]) -> list[dict[str, str]]:
    model_ids = config.get("served_models", DEFAULT_CONFIG["served_models"])
    if not isinstance(model_ids, list):
        model_ids = DEFAULT_CONFIG["served_models"]
    return [{"name": str(model_id)} for model_id in model_ids if str(model_id).strip()]


def make_claude_3p_backup(config: dict[str, Any]) -> Path:
    paths = claude_3p_paths(config)
    stamp = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    backup_dir = paths["backup_root"] / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    for key in ("meta", "provider"):
        src = paths[key]
        if src.exists():
            shutil.copy2(src, backup_dir / src.name)
    return backup_dir


def list_claude_3p_backups(config: dict[str, Any]) -> list[dict[str, Any]]:
    backup_root = claude_3p_paths(config)["backup_root"]
    if not backup_root.exists():
        return []
    backups: list[dict[str, Any]] = []
    for item in sorted(backup_root.iterdir(), reverse=True):
        if item.is_dir():
            backups.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "created_at": dt.datetime.fromtimestamp(item.stat().st_mtime).isoformat(timespec="seconds"),
                }
            )
    return backups


def get_claude_3p_status(config: dict[str, Any]) -> dict[str, Any]:
    paths = claude_3p_paths(config)
    provider: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    errors: list[str] = []
    try:
        if paths["provider"].exists():
            provider = read_json_file(paths["provider"])
    except Exception as exc:
        errors.append(f"provider read failed: {short_error(exc)}")
    try:
        if paths["meta"].exists():
            meta = read_json_file(paths["meta"])
    except Exception as exc:
        errors.append(f"meta read failed: {short_error(exc)}")

    api_key = provider.get("inferenceGatewayApiKey") if isinstance(provider, dict) else None
    return {
        "config_dir": str(paths["config_dir"]),
        "provider_path": str(paths["provider"]),
        "meta_path": str(paths["meta"]),
        "provider_exists": paths["provider"].exists(),
        "meta_exists": paths["meta"].exists(),
        "applied_id": meta.get("appliedId") if isinstance(meta, dict) else None,
        "provider_id": str(config.get("claude_3p_provider_id", DEFAULT_CONFIG["claude_3p_provider_id"])),
        "provider_name": str(config.get("claude_3p_provider_name", DEFAULT_CONFIG["claude_3p_provider_name"])),
        "base_url": provider.get("inferenceGatewayBaseUrl") if isinstance(provider, dict) else None,
        "auth_scheme": provider.get("inferenceGatewayAuthScheme") if isinstance(provider, dict) else None,
        "api_key_set": bool(api_key),
        "errors": errors,
        "backups": list_claude_3p_backups(config)[:12],
    }


def apply_claude_3p_gateway_config(config: dict[str, Any], base_url: str, api_key: str) -> dict[str, Any]:
    paths = claude_3p_paths(config)
    provider_id = str(config.get("claude_3p_provider_id", DEFAULT_CONFIG["claude_3p_provider_id"]))
    provider_name = str(config.get("claude_3p_provider_name", DEFAULT_CONFIG["claude_3p_provider_name"]))
    base_url = base_url.strip() or str(config.get("proxy_public_url", DEFAULT_CONFIG["proxy_public_url"]))

    provider: dict[str, Any] = {}
    if paths["provider"].exists():
        provider = read_json_file(paths["provider"])
        if not isinstance(provider, dict):
            provider = {}

    next_provider = dict(provider)
    next_provider.update(
        {
            "coworkEgressAllowedHosts": ["*"],
            "disableDeploymentModeChooser": True,
            "inferenceGatewayAuthScheme": "bearer",
            "inferenceGatewayBaseUrl": base_url,
            "inferenceModels": build_claude_3p_inference_models(config),
            "inferenceProvider": "gateway",
        }
    )
    if api_key.strip():
        next_provider["inferenceGatewayApiKey"] = api_key.strip()
    elif "inferenceGatewayApiKey" not in next_provider:
        next_provider["inferenceGatewayApiKey"] = ""

    meta = {
        "appliedId": provider_id,
        "entries": [{"id": provider_id, "name": provider_name}],
    }
    current_meta: Any = {}
    if paths["meta"].exists():
        current_meta = read_json_file(paths["meta"])

    if next_provider != provider or meta != current_meta:
        make_claude_3p_backup(config)
        write_json_no_bom(paths["provider"], next_provider)
        write_json_no_bom(paths["meta"], meta)
    return get_claude_3p_status(config)


def restore_claude_3p_backup(config: dict[str, Any], backup_name: str) -> dict[str, Any]:
    paths = claude_3p_paths(config)
    backup_root = paths["backup_root"].resolve()
    backup_dir = (backup_root / backup_name).resolve()
    if backup_root not in backup_dir.parents or not backup_dir.is_dir():
        raise ValueError("invalid backup")

    make_claude_3p_backup(config)
    for filename in ("_meta.json", paths["provider"].name):
        src = backup_dir / filename
        if src.exists():
            target = paths["meta"] if filename == "_meta.json" else paths["provider"]
            data = read_json_file(src)
            write_json_no_bom(target, data)
    return get_claude_3p_status(config)


def describe_image_with_vision_provider(media_type: str, base64_data: str, config: dict[str, Any]) -> tuple[str, int]:
    provider = get_vision_provider(config)
    env_name = get_vision_api_key_env(config, provider)
    api_key = os.getenv(env_name)
    if not api_key:
        raise RuntimeError(f"{env_name} is not set")

    timeout_seconds = float(config.get("vision_timeout_seconds", 45))
    base_url = get_vision_base_url(config, provider)
    model = get_vision_model(config)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{base64_data}"},
                    },
                ],
            }
        ],
    }

    started = time.perf_counter()
    response = httpx.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=httpx.Timeout(timeout_seconds, connect=min(15.0, timeout_seconds)),
        follow_redirects=False,
        trust_env=False,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response.raise_for_status()
    description = extract_choice_text(response.json())
    if not description:
        raise ValueError("vision response content is empty")
    return description, elapsed_ms


describe_image_with_qwen = describe_image_with_vision_provider


def rewrite_images_to_text(
    payload: Any,
    config: dict[str, Any],
    describe_func: Any = describe_image_with_vision_provider,
) -> tuple[Any, dict[str, Any]]:
    stats: dict[str, Any] = {
        "image_count": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "vision_elapsed_ms": 0,
        "errors": [],
    }
    if not config.get("vision_enabled", True) or not isinstance(payload, dict):
        return payload, stats

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload, stats

    cache_path = expand_path(str(config.get("image_cache_path", DEFAULT_CONFIG["image_cache_path"])))
    cache = load_image_cache(cache_path)
    cache_dirty = False
    max_image_bytes = int(config.get("max_image_bytes", DEFAULT_CONFIG["max_image_bytes"]))
    provider = get_vision_provider(config)
    model = get_vision_model(config)
    cache_namespace = f"{provider}:{model}"

    rewritten_messages: list[Any] = []
    image_index = 0
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            rewritten_messages.append(message)
            continue

        rewritten_content: list[Any] = []
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "image":
                rewritten_content.append(block)
                continue

            image_index += 1
            stats["image_count"] += 1
            source = block.get("source") if isinstance(block.get("source"), dict) else {}
            source_type = source.get("type")
            media_type = source.get("media_type")
            base64_data = source.get("data")

            if source_type != "base64" or not isinstance(media_type, str) or not isinstance(base64_data, str):
                description = unavailable_image_description("unsupported image source")
                stats["errors"].append("unsupported image source")
            elif len(base64_data) > max_image_bytes:
                description = unavailable_image_description("image too large")
                stats["errors"].append("image too large")
            else:
                cache_key = hash_image(media_type, base64_data, cache_namespace)
                cache_entry = cache.get(cache_key) if isinstance(cache.get(cache_key), dict) else None
                cached_description = cache_entry.get("description") if cache_entry else None
                if isinstance(cached_description, str) and cached_description:
                    stats["cache_hits"] += 1
                    description = cached_description
                else:
                    stats["cache_misses"] += 1
                    vision_started = time.perf_counter()
                    try:
                        description, elapsed_ms = describe_func(media_type, base64_data, config)
                        stats["vision_elapsed_ms"] += elapsed_ms
                        cache[cache_key] = {
                            "provider": provider,
                            "media_type": media_type,
                            "base64_len": len(base64_data),
                            "description": description,
                            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                            "model": model,
                        }
                        cache_dirty = True
                    except Exception as exc:
                        stats["vision_elapsed_ms"] += int((time.perf_counter() - vision_started) * 1000)
                        reason = short_error(exc)
                        description = unavailable_image_description(reason)
                        stats["errors"].append(reason)

            rewritten_content.append(
                {
                    "type": "text",
                    "text": f"[Image {image_index} Description]\n{description}",
                }
            )

        rewritten_message = dict(message)
        rewritten_message["content"] = rewritten_content
        rewritten_messages.append(rewritten_message)

    if stats["image_count"] == 0:
        return payload, stats

    if cache_dirty:
        try:
            save_image_cache(cache_path, cache)
        except Exception as exc:
            reason = f"failed to save image cache: {short_error(exc)}"
            logging.exception(reason)
            stats["errors"].append(reason)

    rewritten_payload = dict(payload)
    rewritten_payload["messages"] = rewritten_messages
    return rewritten_payload, stats


def find_image_blocks(payload: Any) -> dict[str, Any]:
    stats = {
        "image_block_count": 0,
        "unsupported_image_text_count": 0,
        "image_media_types": [],
        "image_data_lengths": [],
        "content_block_types": {},
    }

    messages = payload.get("messages", []) if isinstance(payload, dict) else []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", ""))
            stats["content_block_types"][block_type] = (
                stats["content_block_types"].get(block_type, 0) + 1
            )

            if block_type == "image":
                stats["image_block_count"] += 1
                source = block.get("source") if isinstance(block.get("source"), dict) else {}
                media_type = source.get("media_type")
                data = source.get("data")
                if isinstance(media_type, str):
                    stats["image_media_types"].append(media_type)
                if isinstance(data, str):
                    stats["image_data_lengths"].append(len(data))

            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and "[Unsupported Image]" in text:
                    stats["unsupported_image_text_count"] += 1

    return stats


def rewrite_model(payload: Any, model_map: dict[str, str]) -> Any:
    if not model_map or not isinstance(payload, dict):
        return payload
    model = payload.get("model")
    if not isinstance(model, str):
        return payload

    replacement = model_map.get(model)
    if replacement is None:
        for pattern, mapped_model in model_map.items():
            if pattern == "*":
                replacement = mapped_model
                break
            if pattern.endswith("*") and model.startswith(pattern[:-1]):
                replacement = mapped_model
                break

    if replacement is not None:
        payload = dict(payload)
        payload["model"] = replacement
    return payload


def build_models_response(model_ids: list[str]) -> dict[str, Any]:
    data = [
        {
            "id": model_id,
            "type": "model",
            "display_name": model_id,
            "created_at": "2026-01-01T00:00:00Z",
        }
        for model_id in model_ids
    ]
    return {
        "data": data,
        "has_more": False,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
    }


def build_target_url(
    upstream_base_url: str,
    incoming_path: str,
    strip_path_prefix: str = "",
    upstream_path_prefix: str = "",
) -> str:
    parsed = urlsplit(incoming_path)
    path = parsed.path or "/"
    if strip_path_prefix:
        prefix = "/" + strip_path_prefix.strip("/")
        if path == prefix:
            path = "/"
        elif path.startswith(prefix + "/"):
            path = path[len(prefix) :]
    if upstream_path_prefix:
        upstream_prefix = "/" + upstream_path_prefix.strip("/")
        path = upstream_prefix.rstrip("/") + (path if path.startswith("/") else f"/{path}")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{upstream_base_url.rstrip('/')}{path}{query}"


def dump_request(
    *,
    dump_dir: Path,
    copy_timestamped_dumps: bool,
    request_id: str,
    method: str,
    path: str,
    target_url: str,
    headers: dict[str, str],
    body: bytes,
    forwarded_body: bytes,
) -> dict[str, Any]:
    dump_dir.mkdir(parents=True, exist_ok=True)
    created_at = dt.datetime.now().isoformat(timespec="seconds")

    raw_path = dump_dir / "latest-request.raw"
    raw_path.write_bytes(body)

    try:
        parsed_body = json.loads(body.decode("utf-8"))
        body_is_json = True
    except Exception as exc:
        parsed_body = {
            "_decode_error": str(exc),
            "_raw_preview": body[:2048].decode("utf-8", errors="replace"),
        }
        body_is_json = False

    try:
        parsed_forwarded_body = json.loads(forwarded_body.decode("utf-8"))
    except Exception:
        parsed_forwarded_body = None

    stats = find_image_blocks(parsed_body)
    original_model = parsed_body.get("model") if isinstance(parsed_body, dict) else None
    forwarded_model = (
        parsed_forwarded_body.get("model")
        if isinstance(parsed_forwarded_body, dict)
        else None
    )
    envelope = {
        "request_id": request_id,
        "created_at": created_at,
        "method": method,
        "path": path,
        "target_url": target_url,
        "headers": redact_headers(headers),
        "body": parsed_body,
        "forwarded_body": parsed_forwarded_body,
    }
    summary = {
        "request_id": request_id,
        "created_at": created_at,
        "method": method,
        "path": path,
        "target_url": target_url,
        "body_bytes": len(body),
        "forwarded_body_bytes": len(forwarded_body),
        "body_is_json": body_is_json,
        "original_model": original_model,
        "forwarded_model": forwarded_model,
        **stats,
    }

    latest_request = dump_dir / "latest-request.json"
    latest_summary = dump_dir / "latest-summary.json"
    latest_request.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    latest_summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    parsed_path = urlsplit(path).path
    if parsed_path.endswith("/messages") and "count_tokens" not in parsed_path:
        (dump_dir / "latest-message-request.json").write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (dump_dir / "latest-message-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if parsed_forwarded_body is not None:
            (dump_dir / "latest-rewritten-message-request.json").write_text(
                json.dumps(parsed_forwarded_body, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    if copy_timestamped_dumps:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_id = request_id.replace("-", "")
        archive_path = dump_dir / f"{stamp}-{safe_id}.json"
        archive_path.write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return summary


def load_latest_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return read_json_file(path)
    except Exception as exc:
        return {"error": short_error(exc)}


def build_admin_state(config: dict[str, Any]) -> dict[str, Any]:
    provider = get_vision_provider(config)
    model = get_vision_model(config)
    env_name = get_vision_api_key_env(config, provider)
    dump_dir = expand_path(str(config.get("dump_dir", DEFAULT_CONFIG["dump_dir"])))
    log_file = expand_path(str(config.get("log_file", DEFAULT_CONFIG["log_file"])))
    aliases = config.get("vision_model_aliases", DEFAULT_CONFIG["vision_model_aliases"])
    base_urls = config.get("vision_base_urls", DEFAULT_CONFIG["vision_base_urls"])
    return {
        "service": {
            "listen": f"http://{config.get('listen_host')}:{config.get('listen_port')}",
            "admin_url": f"http://{config.get('listen_host')}:{config.get('listen_port')}/admin",
            "upstream_base_url": config.get("upstream_base_url"),
            "config_path": config.get("_config_path"),
        },
        "vision": {
            "enabled": bool(config.get("vision_enabled", True)),
            "provider": provider,
            "provider_options": sorted(base_urls.keys()) if isinstance(base_urls, dict) else [provider],
            "model": str(config.get("vision_model", "")),
            "resolved_model": model,
            "model_aliases": aliases if isinstance(aliases, dict) else {},
            "base_url": get_vision_base_url(config, provider),
            "api_key_env": env_name,
            "api_key_set": bool(os.getenv(env_name)),
            "timeout_seconds": config.get("vision_timeout_seconds"),
            "max_image_bytes": config.get("max_image_bytes"),
        },
        "cache": get_cache_info(config),
        "claude_3p": get_claude_3p_status(config),
        "latest": {
            "message_summary": load_latest_json(dump_dir / "latest-message-summary.json"),
            "rewritten_exists": (dump_dir / "latest-rewritten-message-request.json").exists(),
            "dump_dir": str(dump_dir),
            "log_file": str(log_file),
            "log_tail": tail_text(log_file, 80),
        },
    }


ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Vision Proxy Admin</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #121722;
      --muted: #637083;
      --accent: #1769e0;
      --accent-2: #0f8b6e;
      --danger: #bc2f2f;
      --code: #111827;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 "Segoe UI", Arial, sans-serif;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      padding: 16px 24px;
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 650; }
    main { padding: 20px 24px 32px; max-width: 1360px; margin: 0 auto; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    h2 { margin: 0 0 14px; font-size: 15px; font-weight: 650; }
    label { display: block; color: var(--muted); font-size: 12px; margin-bottom: 5px; }
    input, select {
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 7px 9px;
      font: inherit;
    }
    input[type="checkbox"] { width: auto; min-height: 0; }
    .fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .field-full { grid-column: 1 / -1; }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .actions { margin-top: 14px; display: flex; gap: 8px; flex-wrap: wrap; }
    button {
      min-height: 34px;
      border: 1px solid #b8c8e3;
      border-radius: 6px;
      background: #fff;
      color: var(--accent);
      padding: 7px 11px;
      font: inherit;
      cursor: pointer;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    button.danger { border-color: #e0b9b9; color: var(--danger); }
    .metric-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .metric { border: 1px solid var(--line); border-radius: 6px; padding: 9px; min-height: 62px; }
    .metric strong { display: block; font-size: 18px; }
    .muted { color: var(--muted); }
    .status-ok { color: var(--accent-2); font-weight: 650; }
    .status-bad { color: var(--danger); font-weight: 650; }
    code, pre {
      font-family: Consolas, "SFMono-Regular", monospace;
      color: var(--code);
    }
    pre {
      background: #f1f3f7;
      border: 1px solid var(--line);
      border-radius: 6px;
      margin: 0;
      padding: 10px;
      max-height: 360px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .span-2 { grid-column: 1 / -1; }
    #toast {
      min-height: 20px;
      margin-top: 8px;
      color: var(--muted);
    }
    @media (max-width: 900px) {
      .grid, .fields { grid-template-columns: 1fr; }
      .span-2, .field-full { grid-column: auto; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Vision Proxy Admin</h1>
    <div id="toast"></div>
  </header>
  <main class="grid">
    <section>
      <h2>Vision Model</h2>
      <div class="fields">
        <div>
          <label for="visionProvider">Provider</label>
          <select id="visionProvider"></select>
        </div>
        <div>
          <label for="visionModel">Model</label>
          <select id="visionModel"></select>
        </div>
        <div>
          <label for="visionTimeout">Timeout seconds</label>
          <input id="visionTimeout" type="number" min="5" max="180" step="1">
        </div>
        <div>
          <label for="maxImageBytes">Max base64 length</label>
          <input id="maxImageBytes" type="number" min="100000" step="100000">
        </div>
        <div class="field-full row">
          <input id="visionEnabled" type="checkbox">
          <label for="visionEnabled" style="margin:0">Enable image rewrite</label>
        </div>
      </div>
      <div class="actions">
        <button class="primary" onclick="saveVision()">Save Vision Settings</button>
        <button class="danger" onclick="clearCache()">Clear Image Cache</button>
        <button onclick="refresh()">Refresh</button>
      </div>
    </section>

    <section>
      <h2>Status</h2>
      <div class="metric-grid">
        <div class="metric"><span class="muted">Vision key</span><strong id="keyStatus"></strong></div>
        <div class="metric"><span class="muted">Cache entries</span><strong id="cacheEntries"></strong></div>
        <div class="metric"><span class="muted">Last images</span><strong id="lastImages"></strong></div>
      </div>
      <p class="muted">Resolved model: <code id="resolvedModel"></code></p>
      <p class="muted">Vision base URL: <code id="visionBaseUrl"></code></p>
      <p class="muted">Proxy route: <code id="proxyRoute"></code></p>
    </section>

    <section>
      <h2>Claude Desktop 3p</h2>
      <div class="fields">
        <div class="field-full">
          <label for="gatewayUrl">Gateway URL</label>
          <input id="gatewayUrl" placeholder="http://127.0.0.1:9980/anthropic">
        </div>
        <div>
          <label for="gatewayApiKey">API key</label>
          <input id="gatewayApiKey" type="password" placeholder="Leave blank to keep current key">
        </div>
        <div>
          <label for="restoreBackup">Restore backup</label>
          <select id="restoreBackup"></select>
        </div>
      </div>
      <div class="actions">
        <button class="primary" onclick="applyClaude3p()">Apply 3p Gateway</button>
        <button onclick="restoreClaude3p()">Restore Selected Backup</button>
      </div>
      <p class="muted">Current URL: <code id="current3pUrl"></code></p>
      <p class="muted">Active provider: <code id="active3p"></code></p>
    </section>

    <section>
      <h2>Latest Request</h2>
      <pre id="latestSummary"></pre>
    </section>

    <section class="span-2">
      <h2>Log Tail</h2>
      <pre id="logTail"></pre>
    </section>
  </main>
  <script>
    let state = null;
    const modelOptions = [
      ["paddleocr", "PaddleOCR VL 1.5"],
      ["qwen3-vl-8b", "Qwen3 VL 8B"],
      ["qwen3-vl-32b", "Qwen3 VL 32B"],
      ["Qwen/Qwen3-VL-235B-A22B-Instruct", "ModelScope Qwen3 VL 235B"]
    ];
    function setToast(text, bad) {
      const el = document.getElementById("toast");
      el.textContent = text || "";
      el.className = bad ? "status-bad" : "muted";
    }
    async function api(path, options) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    }
    function fillSelect(select, rows, value) {
      select.innerHTML = "";
      rows.forEach(([val, label]) => {
        const opt = document.createElement("option");
        opt.value = val;
        opt.textContent = label;
        if (val === value) opt.selected = true;
        select.appendChild(opt);
      });
    }
    async function refresh() {
      try {
        state = await api("/admin/api/status");
        render();
        setToast("Loaded");
      } catch (err) {
        setToast(String(err.message || err), true);
      }
    }
    function render() {
    const v = state.vision;
      const recommendedRoute = state.service.admin_url.replace(/\/admin$/, "/anthropic");
      fillSelect(document.getElementById("visionProvider"), v.provider_options.map(x => [x, x]), v.provider);
      fillSelect(document.getElementById("visionModel"), modelOptions, v.model);
      document.getElementById("visionTimeout").value = v.timeout_seconds || 45;
      document.getElementById("maxImageBytes").value = v.max_image_bytes || 8000000;
      document.getElementById("visionEnabled").checked = !!v.enabled;
      document.getElementById("keyStatus").textContent = v.api_key_set ? "set" : "missing";
      document.getElementById("keyStatus").className = v.api_key_set ? "status-ok" : "status-bad";
      document.getElementById("cacheEntries").textContent = state.cache.entries;
      const summary = state.latest.message_summary || {};
      document.getElementById("lastImages").textContent = summary.image_block_count ?? "-";
      document.getElementById("resolvedModel").textContent = v.resolved_model;
      document.getElementById("visionBaseUrl").textContent = v.base_url;
      document.getElementById("proxyRoute").textContent = recommendedRoute;
      const c = state.claude_3p;
      document.getElementById("gatewayUrl").value = recommendedRoute;
      document.getElementById("current3pUrl").textContent = c.base_url || "";
      document.getElementById("active3p").textContent = `${c.applied_id || "-"} / key ${c.api_key_set ? "set" : "missing"}`;
      const backupRows = c.backups.length
        ? [["", "Select backup"], ...c.backups.map(x => [x.name, x.name])]
        : [["", "No backups"]];
      fillSelect(document.getElementById("restoreBackup"), backupRows, "");
      document.getElementById("latestSummary").textContent = JSON.stringify(summary, null, 2);
      document.getElementById("logTail").textContent = (state.latest.log_tail || []).join("\n");
    }
    async function saveVision() {
      const payload = {
        vision_provider: document.getElementById("visionProvider").value,
        vision_model: document.getElementById("visionModel").value,
        vision_enabled: document.getElementById("visionEnabled").checked,
        vision_timeout_seconds: Number(document.getElementById("visionTimeout").value),
        max_image_bytes: Number(document.getElementById("maxImageBytes").value)
      };
      try {
        await api("/admin/api/vision", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        await refresh();
        setToast("Vision settings saved");
      } catch (err) {
        setToast(String(err.message || err), true);
      }
    }
    async function clearCache() {
      try {
        await api("/admin/api/cache/clear", {method: "POST"});
        await refresh();
        setToast("Image cache cleared");
      } catch (err) {
        setToast(String(err.message || err), true);
      }
    }
    async function applyClaude3p() {
      const payload = {
        base_url: document.getElementById("gatewayUrl").value,
        api_key: document.getElementById("gatewayApiKey").value
      };
      try {
        await api("/admin/api/claude3p/apply", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        document.getElementById("gatewayApiKey").value = "";
        await refresh();
        setToast("Claude 3p settings applied. Restart Claude Desktop to use them.");
      } catch (err) {
        setToast(String(err.message || err), true);
      }
    }
    async function restoreClaude3p() {
      const backup = document.getElementById("restoreBackup").value;
      if (!backup) { setToast("No backup selected", true); return; }
      try {
        await api("/admin/api/claude3p/restore", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({backup})
        });
        await refresh();
        setToast("Claude 3p backup restored. Restart Claude Desktop to use it.");
      } catch (err) {
        setToast(String(err.message || err), true);
      }
    }
    refresh();
  </script>
</body>
</html>"""


class CaptureProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClaudeDesktopCaptureProxy/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("%s - %s", self.client_address[0], format % args)

    @property
    def config(self) -> dict[str, Any]:
        return self.server.config  # type: ignore[attr-defined]

    @property
    def client(self) -> httpx.Client:
        return self.server.client  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/admin", "/admin/"):
            self.send_html(200, ADMIN_HTML)
            return
        if path == "/admin/api/status":
            self.send_json(200, build_admin_state(self.config))
            return
        if path == "/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "claude-desktop-capture-proxy",
                    "upstream_base_url": self.config["upstream_base_url"],
                },
            )
            return
        if self.is_models_path(path):
            self.send_json(
                200,
                build_models_response(list(self.config.get("served_models", []))),
            )
            return
        if self.is_gateway_root_path(path):
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "vision-proxy",
                    "upstream_base_url": self.config["upstream_base_url"],
                },
            )
            return
        self.send_json(404, {"error": "not_found", "hint": "Use POST Anthropic API paths or GET /health."})

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health" or self.is_gateway_root_path(path):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def is_gateway_root_path(self, path: str) -> bool:
        prefix = str(self.config.get("strip_path_prefix", "")).strip("/")
        if not prefix:
            return path in ("", "/")
        return path in (f"/{prefix}", f"/{prefix}/")

    def is_models_path(self, path: str) -> bool:
        return path in (
            "/v1/models",
            "/models",
            "/anthropic/v1/models",
            "/anthropic/models",
        )

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/admin/api/"):
            self.handle_admin_post(path)
            return

        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self.send_json(411, {"error": "length_required"})
            return

        try:
            body = self.rfile.read(int(content_length))
        except Exception as exc:
            self.send_json(400, {"error": "failed_to_read_body", "detail": str(exc)})
            return

        incoming_headers = {key: value for key, value in self.headers.items()}
        target_url = build_target_url(
            self.config["upstream_base_url"],
            self.path,
            str(self.config.get("strip_path_prefix", "")),
            str(self.config.get("upstream_path_prefix", "")),
        )

        forwarded_body = body
        vision_stats: dict[str, Any] = {
            "image_count": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "vision_elapsed_ms": 0,
            "errors": [],
        }
        try:
            parsed_body_for_forward = json.loads(body.decode("utf-8"))
            vision_rewritten_body, vision_stats = rewrite_images_to_text(
                parsed_body_for_forward,
                self.config,
            )
            rewritten_body = rewrite_model(
                vision_rewritten_body,
                dict(self.config.get("model_map", {})),
            )
            if rewritten_body is not parsed_body_for_forward:
                forwarded_body = json.dumps(rewritten_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except Exception as exc:
            logging.exception("failed to rewrite request_id=%s error=%s", request_id, short_error(exc))
            forwarded_body = body

        try:
            summary = dump_request(
                dump_dir=expand_path(self.config["dump_dir"]),
                copy_timestamped_dumps=bool(self.config.get("copy_timestamped_dumps", True)),
                request_id=request_id,
                method="POST",
                path=self.path,
                target_url=target_url,
                headers=incoming_headers,
                body=body,
                forwarded_body=forwarded_body,
            )
            logging.info(
                (
                    "captured request_id=%s path=%s body_bytes=%s image_blocks=%s "
                    "unsupported_text=%s vision_images=%s cache_hits=%s cache_misses=%s "
                    "vision_elapsed_ms=%s errors=%s"
                ),
                request_id,
                self.path,
                len(body),
                summary["image_block_count"],
                summary["unsupported_image_text_count"],
                vision_stats["image_count"],
                vision_stats["cache_hits"],
                vision_stats["cache_misses"],
                vision_stats["vision_elapsed_ms"],
                vision_stats["errors"],
            )
        except Exception:
            logging.exception("failed to dump request_id=%s", request_id)

        upstream_headers = {
            key: value
            for key, value in incoming_headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }
        upstream_headers["x-capture-proxy-request-id"] = request_id

        try:
            upstream_started = time.perf_counter()
            with self.client.stream(
                "POST",
                target_url,
                content=forwarded_body,
                headers=upstream_headers,
            ) as response:
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    lower = key.lower()
                    if lower in HOP_BY_HOP_HEADERS or lower == "content-length":
                        continue
                    self.send_header(key, value)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("x-capture-proxy-request-id", request_id)
                self.end_headers()

                for chunk in response.iter_raw():
                    if not chunk:
                        continue
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            upstream_elapsed_ms = int((time.perf_counter() - upstream_started) * 1000)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logging.info(
                (
                    "proxied request_id=%s status=%s upstream_elapsed_ms=%s "
                    "total_elapsed_ms=%s vision_images=%s cache_hits=%s cache_misses=%s "
                    "vision_elapsed_ms=%s errors=%s"
                ),
                request_id,
                response.status_code,
                upstream_elapsed_ms,
                elapsed_ms,
                vision_stats["image_count"],
                vision_stats["cache_hits"],
                vision_stats["cache_misses"],
                vision_stats["vision_elapsed_ms"],
                vision_stats["errors"],
            )
        except (BrokenPipeError, ConnectionResetError):
            logging.warning("client disconnected request_id=%s", request_id)
        except Exception as exc:
            logging.exception("upstream failed request_id=%s", request_id)
            self.send_json(
                502,
                {
                    "error": "upstream_failed",
                    "request_id": request_id,
                    "detail": str(exc),
                    "target_url": target_url,
                },
            )

    def read_admin_payload(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b""
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            if not body:
                return {}
            payload = json.loads(body.decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        parsed = parse_qs(body.decode("utf-8", errors="replace"))
        return {key: values[-1] for key, values in parsed.items() if values}

    def handle_admin_post(self, path: str) -> None:
        try:
            payload = self.read_admin_payload()
            if path == "/admin/api/vision":
                self.update_vision_config(payload)
                save_runtime_config(self.config)
                self.send_json(200, build_admin_state(self.config))
                return
            if path == "/admin/api/cache/clear":
                cache = clear_image_cache(self.config)
                self.send_json(200, {"ok": True, "cache": cache})
                return
            if path == "/admin/api/claude3p/apply":
                base_url = str(payload.get("base_url", ""))
                api_key = str(payload.get("api_key", ""))
                status = apply_claude_3p_gateway_config(self.config, base_url, api_key)
                self.send_json(200, {"ok": True, "claude_3p": status})
                return
            if path == "/admin/api/claude3p/restore":
                backup = str(payload.get("backup", ""))
                status = restore_claude_3p_backup(self.config, backup)
                self.send_json(200, {"ok": True, "claude_3p": status})
                return
            self.send_json(404, {"error": "admin endpoint not found"})
        except Exception as exc:
            logging.exception("admin request failed path=%s", path)
            self.send_json(400, {"error": short_error(exc)})

    def update_vision_config(self, payload: dict[str, Any]) -> None:
        if "vision_provider" in payload:
            provider = str(payload["vision_provider"]).strip().lower()
            base_urls = self.config.get("vision_base_urls", DEFAULT_CONFIG["vision_base_urls"])
            if isinstance(base_urls, dict) and provider not in base_urls:
                raise ValueError("unknown vision provider")
            self.config["vision_provider"] = provider

        if "vision_model" in payload:
            model = str(payload["vision_model"]).strip()
            if not model:
                raise ValueError("vision_model is required")
            self.config["vision_model"] = model

        if "vision_enabled" in payload:
            value = payload["vision_enabled"]
            self.config["vision_enabled"] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")

        if "vision_timeout_seconds" in payload:
            timeout = int(payload["vision_timeout_seconds"])
            if timeout < 5 or timeout > 180:
                raise ValueError("vision_timeout_seconds must be between 5 and 180")
            self.config["vision_timeout_seconds"] = timeout

        if "max_image_bytes" in payload:
            max_image_bytes = int(payload["max_image_bytes"])
            if max_image_bytes < 100000:
                raise ValueError("max_image_bytes is too small")
            self.config["max_image_bytes"] = max_image_bytes

    def send_json(self, status_code: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, status_code: int, payload: str) -> None:
        data = payload.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CaptureProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler], config: dict[str, Any]):
        super().__init__(server_address, handler)
        timeout = httpx.Timeout(connect=30.0, read=None, write=60.0, pool=30.0)
        self.config = config
        self.client = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)

    def server_close(self) -> None:
        self.client.close()
        super().server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture and proxy Claude Desktop Anthropic requests.")
    parser.add_argument("--config", help="Path to JSON config file.")
    parser.add_argument("--host", help="Override listen host.")
    parser.add_argument("--port", type=int, help="Override listen port.")
    parser.add_argument("--upstream-base-url", help="Override upstream base URL.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    if args.host:
        config["listen_host"] = args.host
    if args.port:
        config["listen_port"] = args.port
    if args.upstream_base_url:
        config["upstream_base_url"] = args.upstream_base_url

    setup_logging(expand_path(config["log_file"]))
    host = str(config["listen_host"])
    port = int(config["listen_port"])
    upstream = str(config["upstream_base_url"])

    logging.info("starting capture proxy on http://%s:%s upstream=%s", host, port, upstream)
    logging.info("dump_dir=%s", expand_path(config["dump_dir"]))

    server = CaptureProxyServer((host, port), CaptureProxyHandler, config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("stopping capture proxy")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
