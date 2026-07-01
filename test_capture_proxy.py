import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capture_proxy import (
    apply_claude_3p_gateway_config,
    build_admin_state,
    describe_image_with_vision_provider,
    restore_claude_3p_backup,
    rewrite_images_to_text,
    rewrite_model,
    save_runtime_config,
    read_json_file,
)


def image_block(data: str, media_type: str = "image/png") -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


def config_for(cache_path: Path, **overrides: object) -> dict:
    config = {
        "vision_enabled": True,
        "vision_model": "fake-vision-model",
        "image_cache_path": str(cache_path),
        "max_image_bytes": 8000000,
    }
    config.update(overrides)
    return config


class VisionRewriteTests(unittest.TestCase):
    def test_rewrites_multiple_images_in_order_and_caches_descriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "image_descriptions.json"
            calls: list[str] = []

            def fake_describe(media_type: str, base64_data: str, config: dict) -> tuple[str, int]:
                calls.append(base64_data)
                return f"description for {base64_data}", 7

            payload = {
                "model": "claude-sonnet-4-6",
                "tools": [{"name": "keep_me"}],
                "tool_choice": {"type": "auto"},
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "compare these"},
                            image_block("aaa"),
                            image_block("bbb"),
                        ],
                    }
                ],
            }

            rewritten, stats = rewrite_images_to_text(payload, config_for(cache_path), fake_describe)

            self.assertEqual(stats["image_count"], 2)
            self.assertEqual(stats["cache_hits"], 0)
            self.assertEqual(stats["cache_misses"], 2)
            self.assertEqual(stats["vision_elapsed_ms"], 14)
            self.assertEqual(calls, ["aaa", "bbb"])
            self.assertEqual(rewritten["tools"], payload["tools"])
            self.assertEqual(rewritten["tool_choice"], payload["tool_choice"])
            self.assertIn("[Image 1 Description]\ndescription for aaa", rewritten["messages"][0]["content"][1]["text"])
            self.assertIn("[Image 2 Description]\ndescription for bbb", rewritten["messages"][0]["content"][2]["text"])

            rewritten_again, stats_again = rewrite_images_to_text(payload, config_for(cache_path), fake_describe)

            self.assertEqual(stats_again["image_count"], 2)
            self.assertEqual(stats_again["cache_hits"], 2)
            self.assertEqual(stats_again["cache_misses"], 0)
            self.assertEqual(calls, ["aaa", "bbb"])
            self.assertIn("description for aaa", rewritten_again["messages"][0]["content"][1]["text"])

    def test_vision_failure_inserts_unavailable_text_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "image_descriptions.json"

            def failing_describe(media_type: str, base64_data: str, config: dict) -> tuple[str, int]:
                raise TimeoutError("vision timed out")

            payload = {
                "messages": [
                    {
                        "role": "user",
                        "content": [image_block("aaa")],
                    }
                ]
            }

            rewritten, stats = rewrite_images_to_text(payload, config_for(cache_path), failing_describe)

            self.assertEqual(stats["image_count"], 1)
            self.assertEqual(stats["cache_misses"], 1)
            self.assertEqual(stats["errors"], ["vision timed out"])
            self.assertIn(
                "[Image Description unavailable: vision timed out]",
                rewritten["messages"][0]["content"][0]["text"],
            )

    def test_preserves_tool_blocks_around_rewritten_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "image_descriptions.json"
            tool_use = {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}}
            tool_result = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}

            def fake_describe(media_type: str, base64_data: str, config: dict) -> tuple[str, int]:
                return "diagram details", 3

            payload = {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [tool_use, image_block("aaa"), tool_result],
                    }
                ]
            }

            rewritten, _stats = rewrite_images_to_text(payload, config_for(cache_path), fake_describe)

            content = rewritten["messages"][0]["content"]
            self.assertEqual(content[0], tool_use)
            self.assertEqual(content[2], tool_result)
            self.assertEqual(content[1]["type"], "text")
            self.assertIn("diagram details", content[1]["text"])

    def test_model_rewrite_still_maps_claude_ids_after_vision_rewrite(self) -> None:
        payload = {"model": "claude-sonnet-4-6", "messages": []}

        rewritten = rewrite_model(payload, {"claude-sonnet-*": "deepseek-v4-flash"})

        self.assertEqual(rewritten["model"], "deepseek-v4-flash")

    def test_siliconflow_provider_uses_configured_key_url_and_model_alias(self) -> None:
        captured: dict = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"choices": [{"message": {"content": "识别结果"}}]}

        def fake_post(url: str, **kwargs: object) -> FakeResponse:
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

        config = {
            "vision_provider": "siliconflow",
            "vision_model": "qwen3-vl-32b",
            "vision_base_urls": {"siliconflow": "https://api.siliconflow.cn/v1"},
            "vision_api_key_envs": {"siliconflow": "GUIJILIUDONG_API_KEY"},
            "vision_model_aliases": {"qwen3-vl-32b": "Qwen/Qwen3-VL-32B-Instruct"},
            "vision_timeout_seconds": 45,
        }

        with mock.patch.dict("os.environ", {"GUIJILIUDONG_API_KEY": "test-token"}, clear=False):
            with mock.patch("capture_proxy.httpx.post", side_effect=fake_post):
                description, _elapsed_ms = describe_image_with_vision_provider("image/png", "aaa", config)

        self.assertEqual(description, "识别结果")
        self.assertEqual(captured["url"], "https://api.siliconflow.cn/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(captured["json"]["model"], "Qwen/Qwen3-VL-32B-Instruct")
        image_url = captured["json"]["messages"][0]["content"][1]["image_url"]["url"]
        self.assertEqual(image_url, "data:image/png;base64,aaa")

    def test_save_runtime_config_omits_internal_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config = {
                "_config_path": str(config_path),
                "vision_provider": "siliconflow",
                "vision_model": "qwen3-vl-8b",
            }

            save_runtime_config(config)

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertNotIn("_config_path", saved)
            self.assertEqual(saved["vision_model"], "qwen3-vl-8b")

    def test_read_json_file_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "bom.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ok": True}).encode("utf-8"))

            self.assertEqual(read_json_file(path), {"ok": True})

    def test_claude_3p_apply_and_restore_uses_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_dir = Path(tmp_dir) / "configLibrary"
            provider_id = "provider-1"
            provider_path = config_dir / f"{provider_id}.json"
            meta_path = config_dir / "_meta.json"
            provider_path.parent.mkdir(parents=True)
            provider_path.write_text(
                json.dumps(
                    {
                        "inferenceGatewayBaseUrl": "https://old.example/anthropic",
                        "inferenceGatewayApiKey": "old-key",
                    }
                ),
                encoding="utf-8",
            )
            meta_path.write_text(json.dumps({"appliedId": "old"}), encoding="utf-8")
            config = {
                "claude_3p_config_dir": str(config_dir),
                "claude_3p_provider_id": provider_id,
                "claude_3p_provider_name": "CC Switch",
            }

            apply_claude_3p_gateway_config(config, "http://127.0.0.1:9980/anthropic", "")

            provider = json.loads(provider_path.read_text(encoding="utf-8"))
            self.assertEqual(provider["inferenceGatewayBaseUrl"], "http://127.0.0.1:9980/anthropic")
            self.assertEqual(provider["inferenceGatewayApiKey"], "old-key")
            backups = build_admin_state({"dump_dir": tmp_dir, "log_file": str(Path(tmp_dir) / "proxy.log"), **config})["claude_3p"]["backups"]
            self.assertEqual(len(backups), 1)

            apply_claude_3p_gateway_config(config, "http://127.0.0.1:9980/anthropic", "")
            backups_after_noop = build_admin_state({"dump_dir": tmp_dir, "log_file": str(Path(tmp_dir) / "proxy.log"), **config})["claude_3p"]["backups"]
            self.assertEqual(len(backups_after_noop), 1)

            restore_claude_3p_backup(config, backups[0]["name"])

            restored = json.loads(provider_path.read_text(encoding="utf-8"))
            self.assertEqual(restored["inferenceGatewayBaseUrl"], "https://old.example/anthropic")
            self.assertEqual(restored["inferenceGatewayApiKey"], "old-key")


if __name__ == "__main__":
    unittest.main()
