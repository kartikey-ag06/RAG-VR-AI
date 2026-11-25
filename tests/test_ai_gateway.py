"""Unit tests for core.ai.AIGateway (Phase 3 step 2) - no app, no network: a fake HTTP client records calls."""
import itertools
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app as backend
from core.ai import AIGateway, SseReader, clean_messages
from core.ai.gateway import ANSWER_CACHE_SIZE, MAX_MESSAGES, MAX_PROMPT_CHARS

_N = itertools.count()


class Resp:
    def __init__(self, status=200, payload=None, lines=None):
        self.status_code, self._p, self._lines, self.encoding, self.closed = status, payload, lines or [], None, False
        self.text = json.dumps(payload or {})

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def close(self):
        self.closed = True


class FakeHttp:
    def __init__(self, models=("llama-3.3-70b-versatile", "whisper-large-v3-turbo", "openai/gpt-oss-20b")):
        self.models, self.posts, self.gets, self.reply, self.delay = list(models), [], 0, "hello", 0.0

    def get(self, url, **kw):
        self.gets += 1
        return Resp(payload={"data": [{"id": m} for m in self.models]})

    def post(self, url, json=None, **kw):
        self.posts.append({"url": url, "json": json, **kw})
        time.sleep(self.delay)
        if kw.get("stream"):
            return Resp(lines=[f"data: {__import__('json').dumps({'choices': [{'delta': {'content': c}}]})}" for c in "abc"]
                        + ["data: [DONE]"])
        return Resp(payload={"choices": [{"message": {"content": self.reply}}]})


def gw(tmp_path, env=None, http=None, key_text=None, model_text=None):
    d = tmp_path / f"g{next(_N)}"                     # own folder per gateway: files never leak between cases
    d.mkdir(parents=True)
    kf, mf = d / "key.txt", d / "model.txt"
    if key_text is not None:
        kf.write_text(key_text, "utf-8")
    if model_text is not None:
        mf.write_text(model_text, "utf-8")
    return AIGateway(http or FakeHttp(), env if env is not None else {}, key_file=kf, model_file=mf)


# ---------------------------------------------------------------- key + provider
def test_key_order_env_then_file(tmp_path):
    assert gw(tmp_path).get_key() is None and gw(tmp_path).provider() is None
    assert gw(tmp_path, key_text="gsk_file\nignored").get_key() == "gsk_file"
    assert gw(tmp_path, key_text="not-a-key").get_key() is None                 # must start with gsk_ / xai-
    assert gw(tmp_path, env={"GROQ_API_KEY": " gsk_env "}, key_text="gsk_file").get_key() == "gsk_env"
    assert gw(tmp_path, env={"GROQ_API_KEY": "gsk_a", "XAI_API_KEY": "xai-b"}).get_key() == "xai-b"
    assert gw(tmp_path, env={"GROQ_API_KEY": "gsk_a"}).provider()["name"] == "groq"
    assert gw(tmp_path, env={"XAI_API_KEY": "xai-b"}).provider()["base"] == "https://api.x.ai/v1"


def test_key_file_can_be_a_function(tmp_path):
    f = {"p": tmp_path / "none.txt"}
    g = AIGateway(FakeHttp(), {}, key_file=lambda: f["p"], model_file=tmp_path / "m.txt")
    assert g.get_key() is None
    (tmp_path / "k.txt").write_text("gsk_late", "utf-8")
    f["p"] = tmp_path / "k.txt"
    assert g.get_key() == "gsk_late"


# ---------------------------------------------------------------- models
def test_model_choice(tmp_path):
    env = {"GROQ_API_KEY": "gsk_x"}
    g = gw(tmp_path, env=env)
    assert g.chat_model() == "llama-3.3-70b-versatile" and g.stt_model() == "whisper-large-v3-turbo"
    assert g.fast_model() == "openai/gpt-oss-20b"
    g.chat_model(); g.list_models()
    assert g.http.gets == 1                                                   # model list cached for 10 minutes
    assert gw(tmp_path, env=env, model_text="my-model\nx").chat_model() == "my-model"
    assert gw(tmp_path, env={"XAI_API_KEY": "xai-1"}).chat_model() == "grok-4.7"
    assert gw(tmp_path).chat_model() == ""                                    # no key
    other = gw(tmp_path, env=env, http=FakeHttp(models=["mixtral-x", "whisper-large-v3"]))
    assert other.chat_model() == "mixtral-x" and other.stt_model() == "whisper-large-v3"


def test_model_list_failure_falls_back(tmp_path):
    class Down(FakeHttp):
        def get(self, url, **kw):
            raise ConnectionError("offline")
    g = gw(tmp_path, env={"GROQ_API_KEY": "gsk_x"}, http=Down())
    assert g.chat_model() == "llama-3.3-70b-versatile" and g.stt_model() == "whisper-large-v3-turbo"
    with pytest.raises(RuntimeError, match="no AI key"):
        gw(tmp_path).list_models()


def test_payload(tmp_path):
    g = gw(tmp_path, env={"GROQ_API_KEY": "gsk_x"})
    p = g.chat_payload([{"role": "user", "content": "q"}], 50, stream=True, model="openai/gpt-oss-120b")
    assert p == {"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": "q"}], "temperature": 0.2,
                 "max_tokens": 50, "reasoning_effort": "low", "stream": True}
    assert "reasoning_effort" not in g.chat_payload([], model="llama-3.3-70b-versatile")


# ---------------------------------------------------------------- calls
def test_complete_and_errors(tmp_path):
    g = gw(tmp_path, env={"GROQ_API_KEY": "gsk_x"})
    assert g.complete([{"role": "user", "content": "q"}], 30) == "hello"
    call = g.http.posts[-1]
    assert call["timeout"] == 120 and call["headers"] == {"Authorization": "Bearer gsk_x"} and "stream" not in call
    g.complete([{"role": "user", "content": "q"}], fast=True)
    assert g.http.posts[-1]["json"]["model"] == "openai/gpt-oss-20b"
    with pytest.raises(RuntimeError, match="no API key"):
        gw(tmp_path).complete([])

    class Busy(FakeHttp):
        def post(self, url, **kw):
            return Resp(status=429)
    with pytest.raises(RuntimeError, match="busy"):
        gw(tmp_path, env={"GROQ_API_KEY": "gsk_x"}, http=Busy()).complete([])


def test_stream_and_sse_reader(tmp_path):
    g = gw(tmp_path, env={"GROQ_API_KEY": "gsk_x"})
    assert "".join(g.stream([{"role": "user", "content": "q"}])) == "abc"
    assert g.http.posts[-1]["stream"] is True
    r = Resp(lines=["", ": keep-alive", "data: {bad json", 'data: {"choices":[{"delta":{"content":"x"}}]}'])
    reader = SseReader(r)
    assert list(reader) == ["x"] and reader.complete and r.encoding == "utf-8"      # ended without [DONE] = complete
    r2 = Resp(lines=['data: {"choices":[{"delta":{"content":"a"}}]}', 'data: {"choices":[{"delta":{"content":"b"}}]}'])
    reader2 = SseReader(r2)
    it = iter(reader2)
    assert next(it) == "a" and reader2.complete is False                            # stopped early = not complete


# ---------------------------------------------------------------- cache + single flight
def test_answer_cache_lru(tmp_path):
    g = gw(tmp_path, env={"GROQ_API_KEY": "gsk_x"})
    k = g.cache_key([{"role": "user", "content": "q"}])
    assert k == g.cache_key([{"role": "user", "content": "q"}]) and k != g.cache_key([{"role": "user", "content": "r"}])
    g.remember("e", "AI error: x"); g.remember("empty", "")
    assert g.cached("e") is None and g.cached("empty") is None
    for i in range(ANSWER_CACHE_SIZE + 5):
        g.remember(str(i), f"a{i}")
    assert len(g.answers) == ANSWER_CACHE_SIZE and g.cached("0") is None and g.cached(str(ANSWER_CACHE_SIZE + 4))


def test_single_flight_one_leader(tmp_path):
    g = gw(tmp_path)
    roles, release = [], threading.Event()

    def worker():
        with g.single_flight("k", wait=5) as leader:
            roles.append(leader)
            if leader:
                release.wait(2)
    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts:
        t.start()
    time.sleep(0.2)
    release.set()
    for t in ts:
        t.join(5)
    assert roles.count(True) == 1 and roles.count(False) == 4 and g._inflight == {}


def test_grok_endpoint_shares_one_provider_call(tmp_path, monkeypatch):
    """Five identical questions at the same time -> the provider is called once (was untested before Phase 3)."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    fake = FakeHttp()
    fake.delay = 0.4
    monkeypatch.setattr(backend.HTTP, "post", fake.post)
    client, out = TestClient(backend.app), []
    body = {"messages": [{"role": "user", "content": "same question at the same time"}]}
    ts = [threading.Thread(target=lambda: out.append(client.post("/api/grok", json=body).json())) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert len(fake.posts) == 1 and len(out) == 5
    assert all(o["choices"][0]["message"]["content"] == "hello" for o in out)
    assert sum(1 for o in out if o.get("shared")) == 4


# ---------------------------------------------------------------- message validation
def test_clean_messages():
    ok, err = clean_messages({"messages": [{"role": "user", "content": "hi", "extra": 1}]})
    assert ok == [{"role": "user", "content": "hi"}] and err == ""
    assert clean_messages({})[0] is None and clean_messages(None)[0] is None
    assert clean_messages({"messages": [{"role": "user", "content": "x"}] * (MAX_MESSAGES + 1)})[0] is None
    assert "too long" in clean_messages({"messages": [{"role": "user", "content": "x" * (MAX_PROMPT_CHARS + 1)}]})[1]
    assert clean_messages({"messages": [{"role": "user", "content": 5}]})[0] is None


def test_app_aliases_point_at_the_gateway():
    assert backend.llm_complete == backend.AI.complete and backend.provider == backend.AI.provider
    assert backend._ANSWERS is backend.AI.answers and backend._MODELS is backend.AI.models_cache
