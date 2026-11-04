"""Translation proxy between RSTGameTranslation and Ollama.

Listens on 127.0.0.1:11434, forwards to the real Ollama on 127.0.0.1:11438.

Why this exists: RST sends /api/generate requests with raw=true and a complex
JSON-instruction prompt. Index-Translate-2B is a dedicated translation model:
with raw mode it echoes the input; even with the chat template, the long
mixed-instruction prompt confuses it on noisy multi-block input. The model's
native prompt format (see its model card) translates reliably.

For /api/generate this proxy:
  1. extracts the INPUT JSON from RST's prompt,
  2. translates the text with the model's native prompt (one batched call,
     || between model inputs; RST's ##|||## framing preserved in responses),
  3. returns the result wrapped in the JSON shape RST expects.
Everything else is passed through unchanged.
"""
import json
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
import hashlib
import os
import collections
import contextlib
import http.client
import socket
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("RST_OLLAMA_UPSTREAM", "http://127.0.0.1:11438")
LISTEN_PORT = 11434
LOG_PATH = os.environ.get("RST_PROXY_LOG", str(Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "RSTGameTranslation" / "proxy.log"))
SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
logf = open(LOG_PATH, "a", encoding="utf-8")

import threading
import time as _time

_TRANSLATE_LOCK = threading.Lock()   # one GPU translation at a time
_RESULT_CACHE = {}                    # request-level cache
class LruCache(collections.OrderedDict):
    def __init__(self, capacity):
        super().__init__()
        self.capacity = capacity

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.capacity:
            self.popitem(last=False)


_PIECE_CACHE = LruCache(1000)
_CACHE_TTL = 30.0                     # seconds
_CACHE_HIT_LOG = {"n": 0}
PROMPT_REVISION = "ja-game-strict-v2"
_REQUEST_LOCAL = threading.local()
_ACTIVE_REQUESTS = {}
_CANCELLED_REQUESTS = collections.OrderedDict()
_REQUEST_LOCK = threading.Lock()


class TranslationCancelled(RuntimeError):
    pass


class RequestContext:
    def __init__(self, request_id):
        self.request_id = request_id
        self.cancelled = threading.Event()
        self.deadline = _time.monotonic() + 45
        self.lock = threading.RLock()
        self.sockets = set()

    def check(self):
        if self.cancelled.is_set():
            raise TranslationCancelled("translation superseded or cancelled")
        if _time.monotonic() >= self.deadline:
            self.cancel()
            raise OllamaGenerationError("translation deadline exceeded")

    def attach(self, sock):
        with self.lock:
            self.check()
            self.sockets.add(sock)

    def detach(self, sock):
        with self.lock:
            self.sockets.discard(sock)

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            for sock in tuple(self.sockets):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def cancel_request(request_id):
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        return False
    with _REQUEST_LOCK:
        _CANCELLED_REQUESTS[request_id] = _time.monotonic()
        while len(_CANCELLED_REQUESTS) > 1000:
            _CANCELLED_REQUESTS.popitem(last=False)
        context = _ACTIVE_REQUESTS.get(request_id)
    if context:
        context.cancel()
    return context is not None


def check_cancelled():
    context = getattr(_REQUEST_LOCAL, "context", None)
    if context:
        context.check()


@contextlib.contextmanager
def translation_gate():
    started = _time.monotonic()
    while not _TRANSLATE_LOCK.acquire(timeout=.025):
        check_cancelled()
    try:
        check_cancelled()
        waited = _time.monotonic() - started
        if waited > .05:
            log("queue: waited %.0fms" % (waited * 1000))
        yield
    finally:
        _TRANSLATE_LOCK.release()

def log(msg):
    logf.write(msg + "\n")
    logf.flush()

SEP = "##|||##"
MODEL_SEP = "||"
INPUT_SPLIT = "=== INPUT JSON ==="
LANG_MAP = {
    "chinese (simplified)": "简体中文",
    "chinese (traditional)": "繁体中文",
    "chinese": "中文",
    "simplified chinese": "简体中文",
    "traditional chinese": "繁体中文",
    "zh-cn": "简体中文",
    "zh-tw": "繁体中文",
    "zh": "中文",
    "ja": "日语",
    "en": "英语",
    "english": "英语",
    "japanese": "日语",
    "korean": "韩语",
    "vietnamese": "越南语",
    "russian": "俄语",
    "portuguese": "葡萄牙语",
}

THINK_RE = re.compile(r"^(?:```[\s\S]*?```\s*)+")

def norm_lang(name):
    if not name:
        return "简体中文"
    return LANG_MAP.get(name.strip().lower(), name.strip())

class OllamaGenerationError(RuntimeError):
    pass


def call_ollama_generate(payload):
    check_cancelled()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(UPSTREAM + "/api/generate", data=data,
                                 headers={"Content-Type": "application/json"})
    context = getattr(_REQUEST_LOCAL, "context", None)
    try:
        if context:
            # Keep the socket itself so cancellation can interrupt a blocking read
            # and close the upstream HTTP request, instead of only abandoning the UI task.
            url = urllib.parse.urlsplit(UPSTREAM)
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=max(.1, context.deadline - _time.monotonic()))
            upstream_socket = None
            try:
                connection.connect()
                upstream_socket = connection.sock
                context.attach(upstream_socket)
                connection.request("POST", "/api/generate", body=data, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                body = response.read()
                context.check()
                result = json.loads(body)
                if response.status >= 400:
                    raise OllamaGenerationError("Ollama HTTP %s: %s" % (response.status, result.get("error", body.decode("utf-8", "replace"))))
            except Exception:
                context.check()
                raise
            finally:
                if upstream_socket:
                    context.detach(upstream_socket)
                connection.close()
        else:
            with urllib.request.urlopen(req, timeout=45) as r:
                result = json.load(r)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("error", body)
        except (ValueError, AttributeError):
            detail = body
        raise OllamaGenerationError("Ollama HTTP %s: %s" % (exc.code, detail)) from exc
    if result.get("error"):
        raise OllamaGenerationError(str(result["error"]))
    if result.get("done_reason") == "length":
        raise OllamaGenerationError("translation output length limit reached")
    return result

def translate_native(text, target_lang, model, *, piece_count=1, correction=False):
    """Translate plain text, using || only for model-side batch boundaries."""
    prompt = ("请将以下文本翻译为" + target_lang +
              "，直接输出翻译结果，不要进行任何解释。\n\n" + text)
    if piece_count > 1:
        prompt = ("请将以下文本翻译为" + target_lang +
                  "，只输出译文。以下共" + str(piece_count) +
                  "段，以||分隔。请保持段数和顺序，原样保留||，不要合并或遗漏。\n\n" + text)
    source = getattr(_REQUEST_LOCAL, "source", "auto")
    japanese = source == "ja" or (source == "auto" and has_kana(text))
    if japanese:
        head = ("请将以下日语游戏文本翻译为" + target_lang +
                "，只输出中文译文，不要用日语改写，不要解释。保留数字、变量和格式标记。")
        if target_lang not in ("简体中文", "中文", "繁体中文"):
            head = "请将以下日语游戏文本翻译为" + target_lang + "，只输出译文，不要解释。保留数字、变量和格式标记。"
        if correction:
            head = ("请将以下日语原文翻译成" + target_lang +
                    "。请只输出完整中文译文，逐字保留原文中的变量和格式标记，不要使用日语改写，不要解释。")
        rule = ("以下共%d段，以||分隔。请保持段数和顺序，原样保留||，不要合并或遗漏。" % piece_count if piece_count > 1 else "")
        prompt = head + rule + "\n\n" + text
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "top_p": 0.9, "top_k": 40,
                    "num_predict": max(256, min(4096, len(text) * 6 + 128))},
    }
    _t0 = _time.time()
    try:
        r = call_ollama_generate(payload)
    except OllamaGenerationError as exc:
        check_cancelled()
        if correction:
            raise
        if not any(reason in str(exc).lower() for reason in
                   ("token repeat limit reached", "translation output length limit reached")):
            raise
        log("generation retry: model=%s input_chars=%d reason=%s" % (model, len(text), exc))
        # One bounded retry; never disable Ollama's loop guard or accept truncated output.
        payload["options"].update(temperature=0.2, repeat_penalty=1.15, repeat_last_n=128)
        r = call_ollama_generate(payload)
    log("timing: ollama call %.2fs (%d chars in)" % (_time.time() - _t0, len(text)))
    resp = r.get("response", "")
    check_cancelled()
    return clean_response(resp)

def clean_response(resp):
    """Strip the think-close tag the model prepends to its output."""
    TAG = '</think>'
    idx = resp.find(TAG)
    if idx != -1:
        head = resp[:idx]
        filler = set(" " + chr(13) + chr(10) + "`#*")
        if not head or set(head) <= filler:
            resp = resp[idx + len(TAG):]
    s = resp.strip()
    if s.startswith("```"):
        s = s[3:].lstrip(chr(13) + chr(10))
        close = s.find("```")
        if close != -1:
            s = s[close + 3:].lstrip(chr(13) + chr(10))
        resp = s
    return resp.strip()


def has_kana(s):
    return any('\u3040' <= c <= '\u30ff' for c in s)

# common characters that only exist in Simplified Chinese (not in Japanese kanji)
_ZH_ONLY = set("这们吗说语门药剂银钥设认让读课谁误钱铁锁转轮软轻输边迁远选遗邮邻额飘飞饭饮骗")
# katakana long vowel and other marks that confuse "has_kana"
def looks_chinese(s):
    """True when text is already Simplified Chinese (skip translation)."""
    return any(c in _ZH_ONLY for c in s)

def needs_translation(p):
    """A piece needs translation when it contains Japanese (kana, or kanji
    that is not Simplified-Chinese-only). Pure ASCII/symbols/Chinese pass through."""
    p = p.strip()
    if not p:
        return False
    if has_kana(p):
        return True
    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in p)
    source = getattr(_REQUEST_LOCAL, "source", "auto")
    if source == "ja":
        return has_cjk
    if source not in ("auto", "zh"):
        return any(c.isalpha() for c in p)
    if has_cjk and not looks_chinese(p):
        return True
    return False

def extract_input_json(prompt):
    """Find the {"text_blocks": ...} JSON embedded anywhere in the prompt."""
    key = '"text_blocks"'
    # the REAL input JSON sits at the end of the prompt; the instructions
    # contain an example JSON earlier, so search from the tail
    start = prompt.rfind(key)
    if start == -1:
        return None
    # walk back to the opening brace
    brace_start = prompt.rfind("{", 0, start)
    if brace_start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(brace_start, len(prompt)):
        c = prompt[i]
        if in_str:
            if esc:
                esc = False
            elif c == chr(92):
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                candidate = prompt[brace_start:i + 1]
                try:
                    return json.loads(candidate)
                except Exception:
                    return None
    return None

def normalize_source(value):
    value = str(value or "auto").strip().lower()
    if value in ("ja", "japanese", "日语", "日文"):
        return "ja"
    if value.startswith("zh") or value in ("chinese", "chinese (simplified)", "chinese (traditional)", "简体中文", "繁体中文", "中文"):
        return "zh"
    return value


def handle_generate_request(body):
    """Bind cancellation and source language to this request, never to other threads."""
    data = json.loads(body.decode("utf-8"))
    metadata = data.get("_rst", {})
    request_id = metadata.get("request_id") if isinstance(metadata, dict) else None
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        request_id = None
    input_json = extract_input_json(data.get("prompt", "")) if isinstance(data.get("prompt"), str) else None
    context = RequestContext(request_id) if request_id and input_json else None
    previous_context = getattr(_REQUEST_LOCAL, "context", None)
    previous_source = getattr(_REQUEST_LOCAL, "source", "auto")
    _REQUEST_LOCAL.context = context
    _REQUEST_LOCAL.source = normalize_source((input_json or {}).get("source_language"))
    try:
        if context:
            with _REQUEST_LOCK:
                if request_id in _ACTIVE_REQUESTS:
                    raise OllamaGenerationError("duplicate active RST request ID")
                _ACTIVE_REQUESTS[request_id] = context
                if request_id in _CANCELLED_REQUESTS:
                    context.cancel()
        check_cancelled()
        return _handle_generate_request(body)
    finally:
        if context:
            with _REQUEST_LOCK:
                if _ACTIVE_REQUESTS.get(request_id) is context:
                    del _ACTIVE_REQUESTS[request_id]
        _REQUEST_LOCAL.context = previous_context
        _REQUEST_LOCAL.source = previous_source


def _handle_generate_request(body):
    """Return the response dict for a translated RST request, or None to passthrough."""
    import hashlib
    try:
        _probe = json.loads(body.decode("utf-8"))
        _blocks = _probe.get("prompt", "")
        cache_key = hashlib.md5((_blocks + str(_probe.get("model", ""))).encode("utf-8", "replace")).hexdigest()
    except Exception:
        cache_key = hashlib.md5(body).hexdigest()
    now = _time.time()
    hit = _RESULT_CACHE.get(cache_key)
    if hit and now - hit[1] < _CACHE_TTL:
        _CACHE_HIT_LOG["n"] += 1
        if _CACHE_HIT_LOG["n"] % 20 == 1:
            log("cache hit #%d (deduped identical OCR frame)" % _CACHE_HIT_LOG["n"])
        return hit[0]
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    prompt = data.get("prompt")
    model = data.get("model", "index-translate-2b")
    if not isinstance(prompt, str):
        return None
    # RST's live prompt has no section banner: the instructions are followed
    # directly by the input JSON. Locate it via the text_blocks key and match
    # braces (strings may contain braces, so track quote state).
    input_json = extract_input_json(prompt)
    if input_json is None:
        log("diag: no input JSON found; prompt_len=%d tail=%r"
            % (len(prompt), prompt[-100:]))
        return None
    blocks = input_json.get("text_blocks")
    if not isinstance(blocks, list) or not blocks:
        return None
    target_lang = norm_lang(input_json.get("target_language"))


    with translation_gate():
        # A duplicate may have arrived while another request held the GPU lock.
        hit = _RESULT_CACHE.get(cache_key)
        if hit and _time.time() - hit[1] < _CACHE_TTL:
            return hit[0]
        result = _translate_blocks(blocks, target_lang, model)
        check_cancelled()
        if len(_RESULT_CACHE) >= 200:
            _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))
        _RESULT_CACHE[cache_key] = (result, _time.time())
    return result

# Bound both prompt size and the number of mappings a small translation model must preserve.
_BATCH_PIECES = 12
_BATCH_CHARS = 900


def _translate_blocks(blocks, target_lang, model):
    rows = []
    missing = {}
    resolved = {}
    cached_count = 0
    source = getattr(_REQUEST_LOCAL, "source", "auto")
    def cache_key(piece):
        base = (piece.strip(), target_lang, model)
        return base if source == "auto" else base + (source,)

    def store(key, value):
        resolved[key] = value
        _PIECE_CACHE[key] = value

    def wrong_language(value):
        # Katakana names may legitimately survive translation; target only obvious
        # Japanese grammar, not every Japanese-script character or shared kanji.
        return target_lang in ("简体中文", "中文", "繁体中文") and sum('\u3041' <= c <= '\u3096' for c in value) >= 4

    def protected_tokens(value):
        return collections.Counter(re.findall(r"\{[^{}]+\}|%[sd]|</?[A-Za-z][^>]*>", value))

    for block in blocks:
        pieces = block.get("text", "").split(SEP)
        rows.append((block.get("id"), pieces))
        for piece in pieces:
            if not needs_translation(piece):
                continue
            key = cache_key(piece)
            if key in _PIECE_CACHE:
                cached_count += 1
                resolved[key] = _PIECE_CACHE[key]
            else:
                # Translate identical fragments only once, even across different block IDs.
                missing.setdefault(key, piece)

    fresh = 0
    calls = 0
    fallbacks = 0

    def translate_group(group):
        nonlocal fresh, calls, fallbacks
        if not group:
            return
        check_cancelled()
        keys = [key for key, _ in group]
        texts = [text for _, text in group]
        calls += 1
        try:
            translated = translate_native(MODEL_SEP.join(texts), target_lang, model, piece_count=len(group))
            # A source fragment containing literal || is translated on its own.
            parts = ([translated.strip()] if len(group) == 1 else
                     [part.strip() for part in translated.split(MODEL_SEP)])
            valid = len(parts) == len(group) and all(parts)
            if SEP in translated or (len(group) > 1 and any("|" in part for part in parts)):
                valid = False
            if len(group) == 1 and MODEL_SEP not in texts[0] and MODEL_SEP in translated:
                valid = False
            if not valid and len(group) == 1:
                raise OllamaGenerationError("model returned an empty or misaligned translation")
        except OllamaGenerationError as exc:
            if len(group) == 1 or not any(reason in str(exc).lower() for reason in
                    ("token repeat limit reached", "translation output length limit reached")):
                raise
            valid = False
        if not valid:
            # Never assign a translation to an ambiguous ID. Smaller batches are more reliable.
            fallbacks += 1
            midpoint = len(group) // 2
            translate_group(group[:midpoint])
            translate_group(group[midpoint:])
            return
        for key, translated in zip(keys, parts):
            check_cancelled()
            expected_tokens = protected_tokens(missing[key])
            def invalid_content(value):
                tokens = protected_tokens(value)
                return wrong_language(value) or any(tokens[token] < count for token, count in expected_tokens.items())
            if invalid_content(translated):
                log("translation correction: input_chars=%d" % len(missing[key]))
                calls += 1
                translated = translate_native(missing[key], target_lang, model, correction=True)
                if not translated or invalid_content(translated) or SEP in translated:
                    raise OllamaGenerationError("model returned untranslated Japanese or lost protected format markers")
                if MODEL_SEP not in missing[key] and MODEL_SEP in translated:
                    raise OllamaGenerationError("misaligned corrected translation")
            store(key, translated)
            fresh += 1

    group = []
    chars = 0
    for key, text in missing.items():
        if "|" in text:
            # Preserve literal logical-OR / pipe text without treating it as batch framing.
            translate_group(group)
            group = []
            chars = 0
            translate_group([(key, text)])
            continue
        added = len(text) + (len(MODEL_SEP) if group else 0)
        if group and (len(group) >= _BATCH_PIECES or chars + added > _BATCH_CHARS):
            translate_group(group)
            group = []
            chars = 0
            added = len(text)
        group.append((key, text))
        chars += added
    translate_group(group)

    results = []
    for block_id, pieces in rows:
        output = []
        for piece in pieces:
            key = cache_key(piece)
            output.append(resolved[key] if needs_translation(piece) else piece)
        results.append({"id": block_id, "text": SEP.join(output)})
    if cached_count:
        log("piece-cache: %d reused, %d fresh" % (cached_count, fresh))
    log("direct-translate: %d blocks, %d pieces translated -> %s; batches=%d fallback_splits=%d" %
        (len(blocks), fresh, target_lang, calls, fallbacks))
    return {"text_blocks": results}

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, value):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _forward(self, method):
        if method == "GET" and self.path.split("?")[0] == "/rst/health":
            payload = json.dumps({"service": "rst-ollama-proxy", "pid": os.getpid(),
                                  "source_sha256": SOURCE_SHA256,
                                  "prompt_revision": PROMPT_REVISION,
                                  "cancellation": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        path = self.path
        if method == "POST" and path.split("?")[0] == "/rst/cancel":
            try:
                data = json.loads(body or b"{}")
                active = cancel_request(data.get("request_id"))
                self.send_json(200, {"cancelled": True, "was_active": active})
            except (ValueError, AttributeError):
                self.send_json(400, {"error": "invalid cancellation request"})
            return
        out_body = body
        is_generate = method == "POST" and path.split("?")[0] == "/api/generate"
        translated_response = None
        if is_generate and body:
            _t0 = _time.time()
            try:
                translated_response = handle_generate_request(body)
            except TranslationCancelled:
                log("request cancelled")
                self.send_json(499, {"error": "translation cancelled"})
                return
            except Exception as e:
                log("translate error: %s" % e)
                # This was a recognized RST translation request. Sending its original
                # raw=true instruction prompt to the translation model would run a
                # second, incompatible generation and can trigger another loop.
                payload = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            finally:
                _dt = _time.time() - _t0
                if translated_response is not None and _dt > 0.5:
                    log("timing: request handled in %.2fs" % _dt)
        if translated_response is not None:
            # RST parses the standard Ollama /api/generate envelope and reads
            # the "response" string field (then extracts the text_blocks JSON
            # from it). Wrap our result accordingly.
            inner = json.dumps(translated_response, ensure_ascii=False)
            envelope = {
                "model": "index-translate-2b",
                "created_at": _time.strftime("%Y-%m-%dT%H:%M:%S.000000%z") + "Z",
                "response": inner,
                "done": True,
                "done_reason": "stop",
            }
            self.send_json(200, envelope)
            return
        req = urllib.request.Request(UPSTREAM + path, data=out_body, method=method)
        for h in ("Content-Type", "Accept"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        try:
            with urllib.request.urlopen(req, timeout=600) as up:
                code = up.status
                ct = up.headers.get("Content-Type")
                payload = up.read()
        except urllib.error.HTTPError as e:
            code = e.code
            ct = e.headers.get("Content-Type")
            payload = e.read()
        except Exception as e:
            msg = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            log("ERROR forward %s %s: %s" % (method, path, e))
            return
        self.send_response(code)
        self.send_header("Content-Type", ct or "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")

    def do_HEAD(self):
        self._forward("HEAD")

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else LISTEN_PORT
    log("proxy starting on %d -> %s (direct-translate mode)" % (port, UPSTREAM))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
