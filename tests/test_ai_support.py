from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from ai_support import DAILY_LIMIT, DailyQuota, cloud_answer, load_ai_config, local_answer, read_topics, save_ai_config
from catalog import DEFAULT_AI_MODEL, VPNError, decode_catalog, decode_catalog_details, encrypt, validate_ai_model


class AISupportTests(unittest.TestCase):
    def test_vxp_preserves_key_under_encryption_and_legacy_payload_still_loads(self):
        rows = [{"name": "Example", "url": "https://example.test/subscribe"}]
        password = "local-catalog-password"
        key = "sk-test-example-secret"
        model = "deepseek-v4-pro"
        encrypted = encrypt(rows, password, {"mode": "rule"}, ai_key=key, ai_model=model)
        self.assertNotIn(key.encode(), encrypted)
        vendors, settings, restored, selected = decode_catalog_details(encrypted, password)
        self.assertEqual((vendors[0].name, settings, restored, selected), ("Example", {"mode": "rule"}, key, model))
        self.assertEqual(decode_catalog(encrypted, password), (vendors, settings))
        self.assertEqual(decode_catalog_details(encrypt(rows, password, ai_key=key), password)[2:], (key, DEFAULT_AI_MODEL))
        self.assertEqual(decode_catalog_details(encrypt(rows, password), password)[2:], ("", DEFAULT_AI_MODEL))
        with self.assertRaises(VPNError):
            validate_ai_model("bad model\nname")

    def test_local_key_storage_is_encrypted_and_bound_to_source(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            key = "sk-test-local-secret"
            save_ai_config(path, "source-a", key, "deepseek-v4-pro")
            self.assertNotIn(key.encode(), (path / "ai-key.json.dpapi").read_bytes())
            self.assertEqual(load_ai_config(path, "source-a"), (key, "deepseek-v4-pro"))
            self.assertEqual(load_ai_config(path, "source-b"), ("", DEFAULT_AI_MODEL))
            save_ai_config(path, "source-a", "")
            self.assertEqual(load_ai_config(path, "source-a"), ("", DEFAULT_AI_MODEL))

    def test_quota_is_persistent_and_rolls_over_on_next_local_day(self):
        with TemporaryDirectory() as tmp:
            day = Mock(return_value=date(2026, 11, 20))
            quota = DailyQuota(Path(tmp), day)
            self.assertEqual(quota.remaining(), DAILY_LIMIT)
            for _ in range(100):
                quota.reserve()
            reopened = DailyQuota(Path(tmp), day)
            with self.assertRaises(VPNError):
                reopened.reserve()
            day.return_value = date(2026, 11, 21)
            self.assertEqual(reopened.remaining(), DAILY_LIMIT)

    def test_exact_help_answer_is_local_and_cloud_context_is_small_and_redacted(self):
        topics = read_topics()
        self.assertIn("系统代理", local_answer("没有网怎么办？", topics))
        for question in ("如何使用xpn-x", "如何使用 VPN-X？", "VPN-X 怎么用"):
            answer = local_answer(question, topics)
            self.assertIn("点击「一键启动」即可", answer)
            self.assertIn("不用填写任何配置", answer)
        self.assertIsNone(local_answer("TUN 有关的问题需要进一步分析", topics))
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        response = Mock(status_code=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [json.dumps({"choices": [{"message": {"content": "连接正常"}}]}).encode()]
        session.post.return_value = response
        key = "sk-test-remote-secret"
        secret = "private-password"
        with patch("ai_support.requests.Session", return_value=session):
            result = cloud_answer(key, f"TUN 有问题 {secret}", [("user", "too old"),
                                  ("assistant", "word" * 200), ("user", "https://private.example/token")],
                                  "代理运行中", topics, model="deepseek-v4-pro", secrets=(secret,))
        self.assertEqual(result, "连接正常")
        kwargs = session.post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {key}")
        data = kwargs["json"]
        self.assertEqual(data["model"], "deepseek-v4-pro")
        self.assertLessEqual(data["max_tokens"], 384)
        self.assertIn("相关指南", data["messages"][0]["content"])
        self.assertNotIn(secret, str(data))
        self.assertNotIn("private.example", str(data))
        self.assertNotIn("too old", str(data))
