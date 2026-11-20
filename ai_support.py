"""Local guide and bounded DeepSeek requests for VPN-X AI."""
from datetime import date
import json
from pathlib import Path
import re
import sys
import time

import requests

from catalog import DEFAULT_AI_MODEL, VPNError, atomic, protect, validate_ai_key, validate_ai_model

DAILY_LIMIT = 100
QUESTION_LIMIT = 600
REPLY_LIMIT = 3000
ENDPOINT = "https://api.deepseek.com/chat/completions"
GUIDE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "vpnx-guide.md"


def key_file(data_dir):
    return data_dir / "ai-key.json.dpapi"


def load_ai_config(data_dir, source_id):
    path = key_file(data_dir)
    try:
        with path.open("rb") as stream:
            stored = json.loads(protect(stream.read(4096), decode=True))
        if stored["source"] == source_id:
            return validate_ai_key(stored["key"]), validate_ai_model(stored.get("model", DEFAULT_AI_MODEL))
    except FileNotFoundError:
        pass
    except Exception:
        raise VPNError("无法读取本机 AI 凭据，请重新同步加密目录") from None
    return "", DEFAULT_AI_MODEL


def save_ai_config(data_dir, source_id, key, model=DEFAULT_AI_MODEL):
    model = validate_ai_model(model)
    if key or model != DEFAULT_AI_MODEL:
        record = json.dumps({"source": source_id, "key": validate_ai_key(key), "model": model})
        atomic(key_file(data_dir), protect(record.encode()).encode("ascii"))
    else:
        key_file(data_dir).unlink(missing_ok=True)


class DailyQuota:
    def __init__(self, data_dir, today=None):
        self.path = data_dir / "ai-usage.json"
        self.today = today or date.today

    def _read(self):
        day = self.today().isoformat()
        try:
            with self.path.open("rb") as stream:
                record = json.loads(stream.read(512))
            used = record["used"]
            if type(used) is not int or not 0 <= used <= DAILY_LIMIT or not isinstance(record["day"], str):
                raise ValueError
            return day, used if record["day"] >= day else 0
        except FileNotFoundError:
            return day, 0
        except (ValueError, OSError, KeyError, TypeError):
            raise VPNError("无法读取 AI 次数记录，请检查用户数据目录") from None

    def remaining(self):
        return DAILY_LIMIT - self._read()[1]

    def reserve(self):
        day, used = self._read()
        if used >= DAILY_LIMIT:
            raise VPNError("今天的 100 次提问已用完，明天本机日期刷新后可继续使用")
        # Reserve before sending: even a timed-out cloud request can be billed.
        try:
            atomic(self.path, json.dumps({"day": day, "used": used + 1}).encode())
        except OSError:
            raise VPNError("无法保存 AI 次数记录") from None
        return DAILY_LIMIT - used - 1


def guide_text():
    return GUIDE.read_text(encoding="utf-8")


def read_topics():
    topics = []
    for part in guide_text().split("\n## ")[1:]:
        title, tags, *body = part.splitlines()
        topics.append((title, tuple(tags.removeprefix("关键词：").casefold().split("、")), "\n".join(body).strip()))
    return topics


def normalize(text):
    return re.sub(r"[\W_]+", "", text.casefold())


def relevant(question, topics=None):
    topics = read_topics() if topics is None else topics
    scores = [(sum(len(t) for t in tags if t and t in question.casefold()), title, answer)
              for title, tags, answer in topics]
    return [(title, answer) for score, title, answer in sorted(scores, reverse=True)[:2] if score]


def local_answer(question, topics):
    text = normalize(question)
    for title, tags, body in topics:
        if text == normalize(title) or any(text == normalize(tag) for tag in tags if tag):
            return body
    return None


def redact(text, secrets=()):
    for secret in sorted((s for s in set(secrets) if s), key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    return re.sub(r"(?i)https?://[^\s，；]+|\bsk-[a-z0-9_-]{8,}", "[已隐藏]", text)


def cloud_answer(key, question, history, state, topics, *, model=DEFAULT_AI_MODEL, secrets=()):
    # Limit history, selected guide text and output independently. Never include
    # raw logs, catalog contents, provider/node names or persistent credentials.
    secrets = (*secrets, key)
    system = ("你是 VPN-X AI，以简体中文简明回答 VPN-X 使用问题。优先使用下方本地指南和本次状态。"
              "无法确认时说明原因并给出最多三个排查步骤；不要索取密钥、订阅链接或完整日志。"
              "只给建议，不声称修改了用户的代理。"
              f"\n当前状态：{state[:500]}")
    snippets = "\n".join(f"{title}：{body}" for title, body in relevant(question, topics))[:1000]
    if snippets:
        system += f"\n相关指南：{snippets}"
    messages = [{"role": "system", "content": system}]
    for role, value in tuple(history)[-2:]:
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": redact(value, secrets)[:420]})
    messages.append({"role": "user", "content": redact(question, secrets)[:QUESTION_LIMIT]})
    payload = {"model": validate_ai_model(model), "messages": messages, "thinking": {"type": "disabled"},
               "max_tokens": 384, "stream": False}
    started = time.monotonic()
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.post(ENDPOINT, json=payload, headers={"Authorization": f"Bearer {key}"},
                              timeout=(5, 18), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    detail = {400: "请求参数不受支持，请联系管理员", 404: "模型不可用，请联系管理员",
                              401: "AI 服务认证失败，请联系管理员", 402: "账户余额不足",
                              429: "请求过多，请稍后再试"}.get(response.status_code, f"HTTP {response.status_code}")
                    raise VPNError(f"DeepSeek 请求失败：{detail}")
                raw = bytearray()
                for chunk in response.iter_content(4096):
                    raw.extend(chunk)
                    if len(raw) > 32768 or time.monotonic() - started > 30:
                        raise VPNError("DeepSeek 响应超出大小或时间限制")
        answer = json.loads(raw)["choices"][0]["message"]["content"]
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError
        return redact(answer.strip(), secrets)[:REPLY_LIMIT]
    except requests.RequestException:
        raise VPNError("无法连接 DeepSeek。可先查看本地指南排查网络。") from None
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise VPNError("DeepSeek 返回了无法读取的回复") from None
