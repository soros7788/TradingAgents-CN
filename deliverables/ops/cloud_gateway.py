from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape as xml_escape

DEFAULT_CLI = Path(os.environ.get("NOTEBOOKLM_CLI", "/root/venvs/notebooklm/bin/notebooklm"))
DEFAULT_STORAGE = Path(os.environ.get("NOTEBOOKLM_STORAGE", "/root/.notebooklm/profiles/default/storage_state.json"))
CODEX_ROOT = Path(os.environ.get("CODEX_GATEWAY_ROOT", str(Path.home()))).resolve()
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_TOKEN = os.environ.get("CODEX_GATEWAY_TOKEN", "")
CODEX_MAX_BYTES = int(os.environ.get("CODEX_GATEWAY_MAX_BYTES", "30000"))
LOCAL_NOTEBOOK_ROOT = Path(os.environ.get("LOCAL_NOTEBOOK_ROOT", str(Path.home() / "trae-local-notebooks"))).expanduser().resolve()
LOCAL_NOTEBOOK_INDEX = LOCAL_NOTEBOOK_ROOT / "notebooks.json"
LOCAL_EXPORT_DIR = LOCAL_NOTEBOOK_ROOT / "exports"


def as_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("notebooks", "items", "data", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def terms(query: str) -> list[str]:
    query = " ".join(str(query or "").lower().split())
    parts = [part for part in re.split(r"\s+", query) if part]
    if query and query not in parts:
        parts.append(query)
    return parts


def rank_items(items: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    if not query:
        return items[:limit]
    tokens = terms(query)
    ranked: list[dict[str, Any]] = []
    for item in items:
        body = json.dumps(item, ensure_ascii=False).lower()
        score = 0
        for token in tokens:
            if token and token in body:
                score += 1
        title = str(item.get("title") or item.get("name") or "").lower()
        if query.lower() in title:
            score += 5
        if score > 0:
            ranked.append({**item, "score": score})
    ranked.sort(key=lambda row: (-int(row.get("score", 0)), str(row.get("title") or row.get("name") or "")))
    return ranked[:limit]


def codex_safe_path(raw: str | None) -> Path:
    rel = (raw or ".").strip().lstrip("/")
    path = (CODEX_ROOT / rel).resolve()
    if path != CODEX_ROOT and CODEX_ROOT not in path.parents:
        raise ValueError("path escapes codex gateway root")
    return path


def codex_task_response(payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    payload = payload or {}

    user_prompt = str(
        payload.get("prompt")
        or payload.get("question")
        or ""
    ).strip()

    if not user_prompt:
        return 400, {
            "ok": False,
            "mode": "trae_gateway_codex_task",
            "error": "prompt is required",
        }

    shortcut_model = None
    shortcut_sandbox = None

    shortcuts = (
        ("5.5读写", "gpt-5.5", "workspace-write"),
        ("5.5只读", "gpt-5.5", "read-only"),
        ("mini读写", "gpt-5.4-mini", "workspace-write"),
        ("mini只读", "gpt-5.4-mini", "read-only"),
        ("5.4mini读写", "gpt-5.4-mini", "workspace-write"),
        ("5.4mini只读", "gpt-5.4-mini", "read-only"),
    )

    for label, selected_model, selected_sandbox in shortcuts:
        for separator in ("：", ":", " "):
            marker = label + separator
            if user_prompt.startswith(marker):
                shortcut_model = selected_model
                shortcut_sandbox = selected_sandbox
                user_prompt = user_prompt[len(marker):].strip()
                break
        if shortcut_model:
            break

    if not user_prompt:
        return 400, {
            "ok": False,
            "mode": "trae_gateway_codex_task",
            "error": "prompt is required after shortcut",
        }

    raw_model = (
        payload.get("model")
        or shortcut_model
        or "gpt-5.5"
    )
    model_key = (
        str(raw_model)
        .strip()
        .casefold()
        .replace("_", "-")
        .replace(" ", "")
    )

    model_aliases = {
        "gpt-5.5": "gpt-5.5",
        "gpt5.5": "gpt-5.5",
        "5.5": "gpt-5.5",
        "gpt-5.4-mini": "gpt-5.4-mini",
        "gpt-5.4mini": "gpt-5.4-mini",
        "gpt5.4-mini": "gpt-5.4-mini",
        "5.4-mini": "gpt-5.4-mini",
        "5.4mini": "gpt-5.4-mini",
        "mini": "gpt-5.4-mini",
    }

    model = model_aliases.get(model_key)
    if not model:
        return 400, {
            "ok": False,
            "mode": "trae_gateway_codex_task",
            "error": f"unsupported model: {raw_model}",
            "allowed_models": [
                "gpt-5.5",
                "gpt-5.4-mini",
            ],
        }

    raw_sandbox = (
        payload.get("sandbox")
        or shortcut_sandbox
        or "read-only"
    )
    sandbox_key = (
        str(raw_sandbox)
        .strip()
        .casefold()
        .replace("_", "-")
        .replace(" ", "-")
    )

    sandbox_aliases = {
        "read-only": "read-only",
        "readonly": "read-only",
        "ro": "read-only",
        "workspace-write": "workspace-write",
        "workspacewrite": "workspace-write",
        "read-write": "workspace-write",
        "readwrite": "workspace-write",
        "rw": "workspace-write",
        "write": "workspace-write",
    }

    sandbox = sandbox_aliases.get(sandbox_key)
    if not sandbox:
        return 400, {
            "ok": False,
            "mode": "trae_gateway_codex_task",
            "error": f"unsupported sandbox: {raw_sandbox}",
            "allowed_sandboxes": [
                "read-only",
                "workspace-write",
            ],
        }

    try:
        workdir = codex_safe_path(payload.get("path") or ".")
    except Exception as exc:
        return 400, {
            "ok": False,
            "mode": "trae_gateway_codex_task",
            "error": str(exc),
        }

    if not workdir.exists():
        return 400, {
            "ok": False,
            "mode": "trae_gateway_codex_task",
            "error": f"path not found: {workdir}",
        }

    if workdir.is_file():
        workdir = workdir.parent

    try:
        timeout = int(payload.get("timeout") or 180)
    except (TypeError, ValueError):
        timeout = 180

    timeout = min(max(timeout, 1), 300)

    if sandbox == "read-only":
        policy = (
            "当前模式为 read-only。"
            "只能读取、分析和回答，禁止修改、创建或删除文件。"
        )
    else:
        policy = (
            "当前模式为 workspace-write。"
            "允许在当前工作目录内读写文件并运行必要命令。"
            "禁止修改工作区外、认证信息、密钥、systemd 和 ngrok 配置。"
            "修改前先检查，修改后必须验证。"
        )

    prompt = (
        "你是 TRAE 通过网关调用的 Codex 助手。\n"
        f"{policy}\n"
        "如果信息不足，说明还需要读取哪些文件。\n"
        "优先使用中文简洁回答。\n\n"
        f"用户问题：\n{user_prompt}\n"
    )

    out_path = ""

    try:
        with tempfile.NamedTemporaryFile(
            prefix="codex-gateway-",
            suffix=".txt",
            delete=False,
        ) as output_file:
            out_path = output_file.name

        cmd = [
            CODEX_BIN,
            "exec",
            "--model",
            model,
            "--sandbox",
            sandbox,
            "--skip-git-repo-check",
            "--ephemeral",
            "-C",
            str(workdir),
            "--output-last-message",
            out_path,
            prompt,
        ]

        completed = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )

        try:
            answer = Path(out_path).read_text(
                encoding="utf-8",
                errors="replace",
            ).strip()
        except OSError:
            answer = ""

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()

        if completed.returncode != 0:
            return 503, {
                "ok": False,
                "mode": "trae_gateway_codex_task",
                "kind": "codex_exec_error",
                "exit_code": completed.returncode,
                "model": model,
                "sandbox": sandbox,
                "answer": answer,
                "stdout": stdout[-CODEX_MAX_BYTES:],
                "stderr": stderr[-CODEX_MAX_BYTES:],
            }

        try:
            relative_path = str(workdir.relative_to(CODEX_ROOT))
        except ValueError:
            relative_path = str(workdir)

        return 200, {
            "ok": True,
            "mode": "trae_gateway_codex_task",
            "model": model,
            "sandbox": sandbox,
            "path": relative_path or ".",
            "answer": answer or stdout[-CODEX_MAX_BYTES:],
            "summary_for_trae": (
                answer
                or stdout[-CODEX_MAX_BYTES:]
                or "Codex 已完成。"
            ),
        }

    except subprocess.TimeoutExpired:
        return 504, {
            "ok": False,
            "mode": "trae_gateway_codex_task",
            "kind": "timeout",
            "model": model,
            "sandbox": sandbox,
            "error": f"Codex timed out after {timeout}s",
        }

    except FileNotFoundError:
        return 503, {
            "ok": False,
            "mode": "trae_gateway_codex_task",
            "kind": "codex_missing",
            "model": model,
            "sandbox": sandbox,
            "error": f"Codex binary not found: {CODEX_BIN}",
        }

    except Exception as exc:
        return 400, {
            "ok": False,
            "mode": "trae_gateway_codex_task",
            "model": model,
            "sandbox": sandbox,
            "error": str(exc),
        }

    finally:
        if out_path:
            try:
                Path(out_path).unlink(missing_ok=True)
            except OSError:
                pass





_GEMINI_THOUGHT_SIGNATURES = {}
_GEMINI_DUMMY_SIGNATURE = "context_engineering_is_the_way to_go"


def _gemini_restore_thought_signatures(messages):
    import copy as _copy

    if not isinstance(messages, list):
        return messages

    messages = _copy.deepcopy(messages)

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        if msg.get("role") not in ("assistant", "model"):
            continue

        calls = msg.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue

        # Gemini 3 并行工具调用只有第一个 call 必须携带 signature。
        call = calls[0]
        if not isinstance(call, dict):
            continue

        google = (
            call.get("extra_content", {})
                .get("google", {})
        )

        if google.get("thought_signature"):
            continue

        call_id = str(call.get("id") or "")
        sig = _GEMINI_THOUGHT_SIGNATURES.get(call_id)

        # 如果 TRAE 重建了 tool call / 网关刚重启导致缓存不存在，
        # 使用 Google 官方为迁移/自定义 function call 提供的兼容签名。
        if not sig:
            sig = _GEMINI_DUMMY_SIGNATURE

        call.setdefault("extra_content", {})
        call["extra_content"].setdefault("google", {})
        call["extra_content"]["google"]["thought_signature"] = sig

    return messages


def _gemini_remember_thought_signatures(result):
    if not isinstance(result, dict):
        return

    choices = result.get("choices")
    if not isinstance(choices, list):
        return

    for choice in choices:
        if not isinstance(choice, dict):
            continue

        message = choice.get("message")
        if not isinstance(message, dict):
            continue

        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue

        # Gemini 3 parallel calls: signature normally在第一个 call 上。
        for call in calls:
            if not isinstance(call, dict):
                continue

            call_id = str(call.get("id") or "")
            sig = (
                call.get("extra_content", {})
                    .get("google", {})
                    .get("thought_signature")
            )

            if call_id and sig:
                _GEMINI_THOUGHT_SIGNATURES[call_id] = sig

    # 防止长期运行无限增长
    if len(_GEMINI_THOUGHT_SIGNATURES) > 512:
        keys = list(_GEMINI_THOUGHT_SIGNATURES.keys())
        for k in keys[:-256]:
            _GEMINI_THOUGHT_SIGNATURES.pop(k, None)



def fetch_stock_meta(code: str) -> dict:
    import urllib.request
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    url = f"http://qt.gtimg.cn/q={prefix}{code}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            text = r.read().decode("gbk", errors="ignore")
            if "~" in text:
                parts = text.split("~")
                if len(parts) > 4:
                    return {
                        "name": parts[1].strip(),
                        "realtime_price": float(parts[3]) if parts[3] and parts[3] != "0.00" else None,
                        "prev_close": float(parts[4]) if parts[4] else None
                    }
    except Exception:
        pass
    return {"name": "", "realtime_price": None, "prev_close": None}


def dual_plan_card_response(payload):
    """双系统计划卡只读接口（修复价格优先级漏洞：优先实时/最新收盘价，提供对撞基准价，带沙箱+VM-A容灾）。"""
    import subprocess as _sp
    import json as _json
    import os as _os

    if not isinstance(payload, dict):
        payload = {}
    code = str(payload.get("code", "")).strip()
    if not re.fullmatch(r"\d{6}", code):
        return 400, {"error": "code 须为6位数字"}

    VM_A = "gorgesoros39@136.66.64.228"
    KEY = "/home/katelolita7788/.ssh/hermes_key"

    # 1. 获取标的实时/最新收盘快照行情 (修复漏洞：优先级最高)
    meta = fetch_stock_meta(code)
    real_price = meta.get("realtime_price") or meta.get("prev_close")

    # 2. 从远端检索脚本（通用于沙箱 /data/home/chan_logs 与 VM-A /home/gorgesoros39/chan_logs）
    def build_query_script(logdir, backend_label):
        return f"""
import json, glob, os
code = {repr(code)}
LOGDIR = {repr(logdir)}

files = sorted(glob.glob(os.path.join(LOGDIR, 'dualscan_union_*.json')) + glob.glob(os.path.join(LOGDIR, 'dualscan_*.json')) + glob.glob(os.path.join(LOGDIR, 'dual2scan_*.json')), key=os.path.getmtime, reverse=True)
rec = None
latest_file = files[0] if files else None
if latest_file:
    try:
        with open(latest_file) as f:
            d = json.load(f)
            for r in (d.get('dual') or []):
                if r.get('code') == code:
                    rec = r
                    if not rec.get('ts') and d.get('ts'):
                        rec['ts'] = d['ts']
                    break
    except Exception: pass

if not rec:
    ledger = os.path.join(LOGDIR, 'scan_ledger.jsonl')
    if os.path.exists(ledger):
        try:
            for line in reversed(open(ledger).readlines()[-15000:]):
                try:
                    row = json.loads(line)
                    if row.get('code') == code:
                        rec = row
                        break
                except: pass
        except Exception: pass

grade_info = {{}}
graded_files = sorted(glob.glob(os.path.join(LOGDIR, 'trend_pool_graded_*.json')), key=os.path.getmtime, reverse=True)
if graded_files:
    try:
        with open(graded_files[0]) as f:
            gd = json.load(f)
            for bname, items in gd.get('buckets', {{}}).items():
                for item in items:
                    if item.get('code') == code:
                        grade_info = {{'bucket': bname, 'reason': item.get('reason'), 'date': gd.get('date')}}
                        break
    except Exception: pass

print(json.dumps({{'backend': {repr(backend_label)}, 'rec': rec, 'grade': grade_info, 'file': os.path.basename(latest_file) if latest_file else ''}}))
"""

    remote_res = None

    # Step 2A: 优先从 InstaBox 云端沙箱 (agy-sandbox-01) 检索
    try:
        sbox_script = build_query_script("/data/home/chan_logs", "sandbox")
        sbox_env = dict(_os.environ)
        sbox_env["INSTABOX_API_KEY"] = "ibk_ugtd67auowacprodrjsngdngth3wifz326ywyui52c36x6ygpkda"
        sbox_env["PATH"] = "/home/katelolita7788/.local/bin:" + sbox_env.get("PATH", "")
        r = _sp.run(
            ["/home/katelolita7788/.local/bin/instabox", "exec", "agy-sandbox-01", "--stdin", "--", "python3"],
            input=sbox_script,
            capture_output=True,
            text=True,
            env=sbox_env,
            timeout=15
        )
        if r.returncode == 0 and r.stdout.strip():
            for line in reversed(r.stdout.strip().splitlines()):
                if line.startswith("{") and line.endswith("}"):
                    remote_res = _json.loads(line)
                    break
        elif "is paused" in (r.stderr or ""):
            # 沙箱处于休眠态：后台发起无阻塞唤醒，当前请求无缝回退 VM-A 保障服务
            _sp.Popen(
                ["/home/katelolita7788/.local/bin/instabox", "resume", "agy-sandbox-01", "--no-wait"],
                env=sbox_env,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL
            )
    except Exception:
        remote_res = None

    # Step 2B: 若沙箱休眠或未产出，无缝回退 VM-A 物理节点保障服务不中断
    if not remote_res or not remote_res.get("rec"):
        try:
            vma_script = build_query_script("/home/gorgesoros39/chan_logs", "vm-a")
            r = _sp.run(
                ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", VM_A, "python3"],
                input=vma_script,
                capture_output=True,
                text=True,
                timeout=15
            )
            if r.returncode == 0 and r.stdout.strip():
                for line in reversed(r.stdout.strip().splitlines()):
                    if line.startswith("{") and line.endswith("}"):
                        remote_res = _json.loads(line)
                        break
        except Exception:
            pass

    rec = remote_res.get("rec") if remote_res else None
    grade = remote_res.get("grade") if remote_res else {}

    # 关键修复：真实价格优先于离线静态扫描价格
    scan_price = rec.get("price") if rec else None
    effective_price = real_price if real_price is not None else scan_price

    result = {
        "code": code,
        "name": meta.get("name") or (rec.get("name") if rec else ""),
        "gate": rec.get("gate") if rec else None,
        "price": effective_price,
        "scan_price": scan_price,
        "realtime_price": real_price,
        "dlp": rec.get("dlp") if rec else None,
        "slp": rec.get("slp") if rec else None,
        "ratio": rec.get("ratio") if rec else None,
        "grade": grade.get("bucket"),
        "recursive_direction": rec.get("recursive_direction") if rec else None,
        "source_file": remote_res.get("file") if remote_res else None,
        "backend": remote_res.get("backend") if remote_res else "unknown",
        "partial": False,
        "missing": []
    }

    if not rec and not meta.get("name"):
        return 404, {"error": "该标的无产出", "code": code}

    if not rec:
        result["missing"].append("scan_record")
        result["partial"] = True

    _ts = str((rec.get("ts") if rec else "") or "")
    result["scan_date"] = _ts[:10] if len(_ts) >= 10 else ""

    return 200, result



def antigravity_chat_response(payload):
    """调用本地 agy CLI 进行问答（2026-10-05 接入）。"""
    import subprocess as _sp

    if not isinstance(payload, dict):
        payload = {}
    # 兼容多种字段名
    message = str(payload.get("message") or payload.get("prompt") or payload.get("q") or "").strip()
    if not message:
        return 400, {"error": {"message": "missing message/prompt", "type": "invalid_request"}}

    try:
        r = _sp.run(
            ["/home/katelolita7788/.local/bin/agy", "-p", message],
            capture_output=True, text=True, timeout=120,
            env={**__import__("os").environ, "PATH": "/home/katelolita7788/.local/bin:" + __import__("os").environ.get("PATH", "")},
        )
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            err = (r.stderr or "").strip()[:500]
            return 500, {"error": {"message": f"agy failed: {err}", "type": "gateway_error"}}
        return 200, {"reply": out, "model": "antigravity-cli"}
    except _sp.TimeoutExpired:
        return 504, {"error": {"message": "agy timeout", "type": "gateway_timeout"}}
    except Exception as e:
        return 500, {"error": {"message": str(e)[:300], "type": "gateway_error"}}
def gemini_task_response(payload):
    import os as _os
    import json as _json
    import urllib.request as _request
    import urllib.error as _error

    key = _os.environ.get("SYNTEROLINK_API_KEY", "").strip() or _os.environ.get("GEMINI_API_KEY_2", "").strip() or _os.environ.get("GEMINI_API_KEY", "").strip()

    if not key:
        return 500, {
            "error": {
                "message": "GEMINI_API_KEY / GEMINI_API_KEY_2 is not configured",
                "type": "gateway_error"
            }
        }

    if not isinstance(payload, dict):
        payload = {}

    model = str(payload.get("model") or "gemini-3.6-flash")

    messages = payload.get("messages")

    if isinstance(messages, list):
        messages = _gemini_restore_thought_signatures(messages)

    if not isinstance(messages, list) or not messages:
        prompt = (
            payload.get("prompt")
            or payload.get("task")
            or payload.get("input")
            or payload.get("query")
        )

        if not isinstance(prompt, str) or not prompt.strip():
            return 400, {
                "error": {
                    "message": "messages or prompt/task/input/query is required",
                    "type": "invalid_request_error"
                }
            }

        messages = [
            {
                "role": "user",
                "content": prompt
            }
        ]

    body = {
        "model": model,
        "messages": messages,
        "stream": False
    }

    # TRAE 连通性测试通常只请求极少输出 token。
    # Gemini 3.x 可能先消耗 token 做 thinking，导致 content 为空。
    # 仅对无工具的小额度请求做兼容，不影响正常 Agent 工具调用。
    requested_max = payload.get("max_tokens")
    if isinstance(requested_max, int) and requested_max < 128 and not payload.get("tools"):
        body["max_tokens"] = 128
        body["reasoning_effort"] = "low"

    for name in (
        "temperature",
        "top_p",
        "max_tokens",
        "stop",
        "response_format",
        "tools",
        "tool_choice"
    ):
        if name in payload:
            body[name] = payload[name]

    data = _json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = _request.Request(
        "https://api.synterolink.com/v1/chat/completions",
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        }
    )

    try:
        with _request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                result = _json.loads(raw)
            except Exception:
                result = {"raw": raw}

            _gemini_remember_thought_signatures(result)

            return int(getattr(resp, "status", 200)), result

    except _error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")

        try:
            result = _json.loads(raw)
        except Exception:
            result = {
                "error": {
                    "message": raw or ("Gemini HTTP error " + str(e.code))
                }
            }

        return int(e.code), result

    except Exception as e:
        return 502, {
            "error": {
                "message": str(e),
                "type": "gemini_gateway_error"
            }
        }


def gemini_models_response():
    return 200, {
        "object": "list",
        "data": [
            {
                "id": "gemini-3.6-flash",
                "object": "model",
                "created": 0,
                "owned_by": "google"
            }
        ]
    }


class NotebookLMCloudClient:
    def __init__(self, cli_path: Path = DEFAULT_CLI, storage_path: Path = DEFAULT_STORAGE, timeout: int = 90):
        self.cli_path = Path(cli_path)
        self.storage_path = Path(storage_path)
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "NotebookLMCloudClient":
        return cls(
            cli_path=Path(os.environ.get("NOTEBOOKLM_CLI", str(DEFAULT_CLI))),
            storage_path=Path(os.environ.get("NOTEBOOKLM_STORAGE", str(DEFAULT_STORAGE))),
            timeout=as_int(os.environ.get("NOTEBOOKLM_TIMEOUT", "90"), 90),
        )

    def _run(self, args: list[str], input_text: str | None = None, timeout: int | None = None) -> dict[str, Any]:
        if not self.cli_path.exists():
            return {"ok": False, "kind": "cli_missing", "message": f"NotebookLM CLI not found: {self.cli_path}"}
        if not self.storage_path.exists():
            return {"ok": False, "kind": "auth_required", "message": f"NotebookLM storage not found: {self.storage_path}"}

        command = [str(self.cli_path), "--storage", str(self.storage_path), *args]
        try:
            completed = subprocess.run(
                command,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "kind": "timeout", "message": f"NotebookLM command timed out after {timeout or self.timeout}s"}

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        payload = None
        if stdout:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                payload = stdout

        if completed.returncode == 0:
            return {"ok": True, "exit_code": completed.returncode, "payload": payload}

        error_text = stderr or stdout or "NotebookLM CLI command failed"
        lowered = error_text.lower()
        if "authentication expired" in lowered or "notebooklm login" in lowered or "accounts.google.com" in lowered:
            kind = "auth_required"
        elif "not found" in lowered:
            kind = "not_found"
        else:
            kind = "cli_error"
        return {"ok": False, "kind": kind, "exit_code": completed.returncode, "message": error_text, "payload": payload}

    def list_notebooks(self, limit: int | None = None) -> dict[str, Any]:
        args = ["list", "--json"]
        if limit is not None:
            args.extend(["--limit", str(max(0, int(limit)))])
        result = self._run(args)
        if not result["ok"]:
            return result
        notebooks = extract_items(result.get("payload"))
        if limit is not None:
            notebooks = notebooks[:limit]
        return {**result, "notebooks": notebooks}

    def search_notebooks(self, query: str, limit: int = 10) -> dict[str, Any]:
        result = self.list_notebooks(limit=None)
        if not result["ok"]:
            return result
        notebooks = extract_items(result.get("payload"))
        ranked = rank_items(notebooks, query, limit)
        return {"ok": True, "payload": result.get("payload"), "query": query, "notebooks": ranked, "count": len(ranked)}

    def metadata(self, notebook_id: str) -> dict[str, Any]:
        return self._run(["metadata", "-n", notebook_id, "--json"])

    def ask(self, notebook_id: str, question: str, limit: int = 5, timeout: int | None = None) -> dict[str, Any]:
        args = ["ask", "-n", notebook_id, "--json"]
        if timeout is not None:
            args.extend(["--timeout", str(max(1, int(timeout)))])
        args.append(question)
        result = self._run(args, timeout=timeout or self.timeout)
        if result["ok"]:
            result["evidence_limit"] = limit
        return result

    def create_notebook(self, title: str, use: bool = False) -> dict[str, Any]:
        args = ["create", title, "--json"]
        if use:
            args.append("--use")
        return self._run(args)

    def add_source(
        self,
        notebook_id: str,
        content: str,
        source_type: str | None = None,
        title: str | None = None,
        mime_type: str | None = None,
        timeout: float | int | None = None,
        follow_symlinks: bool = False,
        allow_internal: bool = False,
    ) -> dict[str, Any]:
        content_text = str(content or "")
        effective_type = str(source_type or "").strip()
        normalized_type = (
            effective_type.casefold()
            .replace("_", "-")
            .replace(" ", "-")
        )

        if normalized_type in {
            "plain",
            "plaintext",
            "plain-text",
            "pasted-text",
        }:
            effective_type = "text"
            normalized_type = "text"

        is_url = content_text.lstrip().casefold().startswith(
            ("http://", "https://")
        )

        use_stdin = normalized_type == "text"

        if (
            not effective_type
            and not is_url
            and (
                "\n" in content_text
                or "\r" in content_text
                or len(content_text) >= 1024
            )
        ):
            effective_type = "text"
            use_stdin = True

        command_content = "-" if use_stdin else content_text

        args = [
            "source",
            "add",
            command_content,
            "-n",
            notebook_id,
            "--json",
        ]

        if effective_type:
            args.extend(["--type", effective_type])

        if title:
            args.extend(["--title", title])

        if mime_type:
            args.extend(["--mime-type", mime_type])


        if follow_symlinks:
            args.append("--follow-symlinks")

        if allow_internal:
            args.append("--allow-internal")

        return self._run(
            args,
            input_text=content_text if use_stdin else None,
            timeout=timeout or self.timeout,
        )



def build_capabilities() -> dict[str, Any]:
    return {
        "mode": "trae_gateway_capabilities",
        "cloud": True,
        "capabilities": [
            "GET /health",
            "GET /trae-api/openapi.json",
            "GET /trae-api/capabilities",
            "GET /trae-api/cloud/notebooks",
            "POST /trae-api/cloud/notebooks",
            "POST /trae-api/cloud/notebooks/search",
            "GET /trae-api/cloud/notebooks/{id}",
            "POST /trae-api/cloud/notebooks/{id}/ask",
            "POST /trae-api/query",
            "POST /trae-api/codex/task",
        ],
        "notes": [
            "这是云端 NotebookLM 版，依赖 NotebookLM CLI 登录态。",
            "如果 storage_state.json 失效，需要先重新登录 NotebookLM。",
            "POST /trae-api/cloud/notebooks 支持 title/use 创建，也支持 notebook_id + sources 追加来源。",
            "Codex 默认 gpt-5.5 + read-only；支持 5.5读写、5.5只读、mini读写、mini只读。",
        ],
    }


def build_openapi(base_url: str) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "TRAE NotebookLM Cloud API",
            "version": "0.1.0",
            "description": "Cloud NotebookLM gateway for TRAE: list, search, open, ask, create notebooks.",
        },
        "servers": [{"url": base_url}],
        "paths": {
            "/health": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/trae-api/openapi.json": {"get": {"responses": {"200": {"description": "OpenAPI"}}}},
            "/trae-api/capabilities": {"get": {"responses": {"200": {"description": "Capabilities"}}}},
            "/trae-api/query": {
                "post": {
                    "requestBody": {"required": False},
                    "responses": {"200": {"description": "Cloud search result"}},
                }
            },
            "/trae-api/cloud/notebooks": {
                "get": {
                    "parameters": [{"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "default": 100}}],
                    "responses": {"200": {"description": "Notebook list"}, "401": {"description": "NotebookLM auth required"}},
                },
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "anyOf": [{"required": ["title"]}, {"required": ["notebook_id"]}],
                                    "properties": {
                                        "notebook_id": {"type": "string", "description": "Add sources to an existing notebook instead of creating a new one."},
                                        "title": {"type": "string"},
                                        "use": {"type": "boolean", "default": False},
                                        "sources": {
                                            "type": "array",
                                            "description": "Optional sources to add after notebook creation or to the target notebook.",
                                            "items": {
                                                "oneOf": [
                                                    {"type": "string"},
                                                    {
                                                        "type": "object",
                                                        "properties": {
                                                            "content": {"type": "string"},
                                                            "source": {"type": "string"},
                                                            "url": {"type": "string"},
                                                            "text": {"type": "string"},
                                                            "path": {"type": "string"},
                                                            "type": {"type": "string", "enum": ["url", "text", "file", "youtube"]},
                                                            "title": {"type": "string"},
                                                            "mime_type": {"type": "string"},
                                                            "timeout": {"type": "number"},
                                                            "follow_symlinks": {"type": "boolean"},
                                                            "allow_internal": {"type": "boolean"},
                                                        },
                                                    },
                                                ]
                                            },
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Notebook created"}, "401": {"description": "NotebookLM auth required"}},
                },
            },
            "/trae-api/cloud/notebooks/search": {
                "post": {
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "query": {"type": "string"},
                                        "limit": {"type": "integer", "default": 10},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Notebook search"}, "401": {"description": "NotebookLM auth required"}},
                }
            },
            "/trae-api/cloud/notebooks/{id}": {
                "get": {"responses": {"200": {"description": "Notebook metadata"}, "401": {"description": "NotebookLM auth required"}}}
            },
            "/trae-api/cloud/notebooks/{id}/ask": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["question"],
                                    "properties": {
                                        "question": {"type": "string"},
                                        "query": {"type": "string"},
                                        "limit": {"type": "integer", "default": 5},
                                        "timeout": {"type": "integer", "default": 90},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Notebook answer"}, "401": {"description": "NotebookLM auth required"}},
                }
            },
            "/trae-api/codex/task": {
                "post": {
                    "summary": "Ask Codex with gpt-5.5 or gpt-5.4-mini",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["prompt"],
                                    "properties": {
                                        "prompt": {"type": "string"},
                                        "question": {"type": "string"},
                                        "path": {"type": "string", "default": "."},
                                        "timeout": {"type": "integer", "default": 180},
                                        "token": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Codex answer"},
                        "401": {"description": "Codex gateway token required"},
                        "503": {"description": "Codex execution error"},
                    },
                }
            },
        },
    }


def cloud_error(kind: str, result: dict[str, Any], notebook_id: str | None = None) -> tuple[int, dict[str, Any]]:
    status = 401 if result.get("kind") == "auth_required" else 503
    body = {
        "mode": kind,
        "ok": False,
        "error": result.get("message") or result.get("kind") or "NotebookLM cloud error",
        "kind": result.get("kind"),
    }
    if notebook_id:
        body["notebook_id"] = notebook_id
    if result.get("payload") is not None:
        body["payload"] = result.get("payload")
    return status, body


def _source_content(item: dict[str, Any]) -> str:
    for key in ("content", "source", "url", "text", "path"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _normalize_sources(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    payload = payload or {}
    raw_sources = payload.get("sources")
    if raw_sources is None and "source" in payload:
        raw_sources = payload.get("source")
    if raw_sources is None:
        return []
    if isinstance(raw_sources, dict):
        raw_sources = [raw_sources]
    elif isinstance(raw_sources, str):
        raw_sources = [raw_sources]
    if not isinstance(raw_sources, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_sources:
        if isinstance(item, str):
            content = item.strip()
            if content:
                normalized.append({"content": content})
            continue
        if not isinstance(item, dict):
            continue
        content = _source_content(item)
        if not content:
            continue
        normalized.append(
            {
                "content": content,
                "type": str(item.get("type") or item.get("source_type") or "").strip() or None,
                "title": str(item.get("title") or "").strip() or None,
                "mime_type": str(item.get("mime_type") or item.get("mimeType") or "").strip() or None,
                "timeout": item.get("timeout", item.get("request_timeout")),
                "follow_symlinks": bool(item.get("follow_symlinks", False)),
                "allow_internal": bool(item.get("allow_internal", False)),
            }
        )
    return normalized


def _first_notebook_record(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        for key in ("notebook", "item", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
    if isinstance(payload, dict):
        return payload
    return None


def _notebook_identifier(record: Any) -> str:
    if isinstance(record, dict):
        for key in ("id", "notebook_id", "notebookId", "name"):
            value = record.get(key)
            if value not in (None, ""):
                return str(value).strip()
    return ""


def _notebook_sources(record: Any) -> list[dict[str, Any]]:
    if not isinstance(record, dict):
        return []
    sources = record.get("sources")
    if isinstance(sources, list):
        return [source for source in sources if isinstance(source, dict)]
    return []


def _source_title_candidates(source_item: dict[str, Any]) -> set[str]:
    candidates: set[str] = set()
    for value in (source_item.get("title"), source_item.get("content")):
        text = str(value or "").strip()
        if text:
            candidates.add(text.casefold())
            candidates.add(Path(text).name.casefold())
    return {candidate for candidate in candidates if candidate}


def _matching_source(record: Any, source_item: dict[str, Any]) -> dict[str, Any] | None:
    candidates = _source_title_candidates(source_item)
    content = str(source_item.get("content") or "").strip()
    for source in _notebook_sources(record):
        title = str(source.get("title") or "").strip().casefold()
        url = str(source.get("url") or "").strip()
        if title and title in candidates:
            return source
        if content and url and url == content:
            return source
    return None


def _maybe_json_payload(result: dict[str, Any]) -> dict[str, Any] | Any:
    payload = result.get("payload")
    if isinstance(payload, dict):
        return payload
    if payload is None:
        return {}
    return payload


def cloud_query_response(client: NotebookLMCloudClient, payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    payload = payload or {}
    query = str(payload.get("query") or payload.get("q") or "").strip()
    material_no = str(payload.get("material_no") or "").strip()
    limit = as_int(payload.get("limit"), 5)
    search_text = " ".join(part for part in [query, material_no] if part).strip()
    result = client.search_notebooks(search_text, limit=limit) if search_text else client.list_notebooks(limit=limit)
    if not result.get("ok"):
        return cloud_error("trae_gateway_cloud_query", result)
    notebooks = result.get("notebooks") or []
    return 200, {
        "mode": "trae_gateway_cloud_query",
        "source": "notebooklm_cloud",
        "query": query,
        "material_no": material_no,
        "notebooks": notebooks,
        "count": len(notebooks),
        "summary_for_trae": f"云端查询 `{search_text}` 命中 {len(notebooks)} 个 NotebookLM 笔记本。",
    }


def cloud_list_response(client: NotebookLMCloudClient, limit: int | None = None) -> tuple[int, dict[str, Any]]:
    result = client.list_notebooks(limit=limit)
    if not result.get("ok"):
        return cloud_error("trae_gateway_cloud_list_notebooks", result)
    notebooks = result.get("notebooks") or []
    return 200, {
        "mode": "trae_gateway_cloud_list_notebooks",
        "source": "notebooklm_cloud",
        "notebooks": notebooks,
        "count": len(notebooks),
        "summary_for_trae": f"已读取 NotebookLM 云端笔记本 {len(notebooks)} 个。",
    }


def cloud_search_response(client: NotebookLMCloudClient, payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    payload = payload or {}
    query = str(payload.get("query") or payload.get("q") or "").strip()
    limit = as_int(payload.get("limit"), 10)
    result = client.search_notebooks(query, limit=limit)
    if not result.get("ok"):
        return cloud_error("trae_gateway_cloud_search_notebooks", result)
    notebooks = result.get("notebooks") or []
    return 200, {
        "mode": "trae_gateway_cloud_search_notebooks",
        "source": "notebooklm_cloud",
        "query": query,
        "notebooks": notebooks,
        "count": len(notebooks),
        "summary_for_trae": f"云端查询 `{query}` 命中 {len(notebooks)} 个 NotebookLM 笔记本。",
    }


def cloud_open_response(client: NotebookLMCloudClient, notebook_id: str) -> tuple[int, dict[str, Any]]:
    notebook_id = str(notebook_id or "").strip()
    result = client.metadata(notebook_id)
    if not result.get("ok"):
        return cloud_error("trae_gateway_cloud_open_notebook", result, notebook_id=notebook_id)
    return 200, {
        "mode": "trae_gateway_cloud_open_notebook",
        "source": "notebooklm_cloud",
        "notebook_id": notebook_id,
        "notebook": result.get("payload"),
        "summary_for_trae": f"已打开 NotebookLM 云端笔记本 `{notebook_id}`。",
    }


def cloud_ask_response(client: NotebookLMCloudClient, notebook_id: str, payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    payload = payload or {}
    notebook_id = str(notebook_id or "").strip()
    question = str(payload.get("question") or payload.get("query") or "").strip()
    if not question:
        return 400, {"error": "question is required"}
    limit = as_int(payload.get("limit"), 5)
    timeout_value = payload.get("timeout")
    timeout = None if timeout_value in (None, "") else as_int(timeout_value, 90)
    result = client.ask(notebook_id, question, limit=limit, timeout=timeout)
    if not result.get("ok"):
        return cloud_error("trae_gateway_cloud_ask_notebook", result, notebook_id=notebook_id)
    payload_obj = result.get("payload")
    if isinstance(payload_obj, dict):
        answer = str(payload_obj.get("answer") or payload_obj.get("content") or payload_obj.get("text") or "")
        refs = payload_obj.get("references") if isinstance(payload_obj.get("references"), list) else []
    else:
        answer = str(payload_obj or "")
        refs = []
    return 200, {
        "mode": "trae_gateway_cloud_ask_notebook",
        "source": "notebooklm_cloud",
        "notebook_id": notebook_id,
        "question": question,
        "answer": answer,
        "references": refs[:limit] if isinstance(refs, list) else [],
        "raw": payload_obj,
        "summary_for_trae": answer or "NotebookLM 云端已返回问答结果。",
    }


def cloud_create_response(client: NotebookLMCloudClient, payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    payload = payload or {}
    title = str(payload.get("title") or "").strip()
    requested_notebook_id = str(payload.get("notebook_id") or "").strip()
    notebook_id = requested_notebook_id
    use = bool(payload.get("use", False))
    sources = _normalize_sources(payload)

    created = False
    create_result: dict[str, Any] | None = None
    notebook_record: dict[str, Any] | None = None

    if requested_notebook_id:
        probe = client.metadata(notebook_id)
        if not probe.get("ok"):
            return cloud_error("trae_gateway_cloud_create_notebook", probe, notebook_id=notebook_id)
        notebook_record = _first_notebook_record(probe.get("payload"))
        if notebook_record is None:
            return 404, {"mode": "trae_gateway_cloud_create_notebook", "created": False, "error": f"notebook not found: {notebook_id}", "notebook_id": notebook_id}
    else:
        if not title:
            return 400, {"error": "title or notebook_id is required"}
        create_result = client.create_notebook(title=title, use=use)
        if not create_result.get("ok"):
            return cloud_error("trae_gateway_cloud_create_notebook", create_result)
        created = True
        notebook_record = _first_notebook_record(create_result.get("payload"))
        notebook_id = notebook_id or _notebook_identifier(notebook_record) or _notebook_identifier(create_result.get("payload"))
        if not notebook_id:
            return 503, {
                "mode": "trae_gateway_cloud_create_notebook",
                "created": True,
                "error": "unable to determine notebook_id from NotebookLM create response",
                "payload": _maybe_json_payload(create_result),
            }

    source_results: list[dict[str, Any]] = []
    source_errors: list[dict[str, Any]] = []
    added_sources: list[dict[str, Any]] = []
    already_present_sources: list[dict[str, Any]] = []

    for index, source_item in enumerate(sources, start=1):
        content = str(source_item.get("content") or "").strip()
        if not content:
            source_errors.append({"index": index, "error": "source content is required", "source": source_item})
            continue
        existing_source = _matching_source(notebook_record, source_item)
        if existing_source is not None:
            entry = {
                "index": index,
                "content": content,
                "type": source_item.get("type"),
                "title": source_item.get("title"),
                "mime_type": source_item.get("mime_type"),
                "ok": True,
                "action": "already_present",
                "source": existing_source,
            }
            already_present_sources.append(entry)
            source_results.append(entry)
            continue
        result = client.add_source(
            notebook_id=notebook_id,
            content=content,
            source_type=source_item.get("type"),
            title=source_item.get("title"),
            mime_type=source_item.get("mime_type"),
            timeout=source_item.get("timeout"),
            follow_symlinks=bool(source_item.get("follow_symlinks", False)),
            allow_internal=bool(source_item.get("allow_internal", False)),
        )
        entry = {
            "index": index,
            "content": content,
            "type": source_item.get("type"),
            "title": source_item.get("title"),
            "mime_type": source_item.get("mime_type"),
            "ok": bool(result.get("ok")),
        }
        if result.get("ok"):
            payload_obj = _maybe_json_payload(result)
            entry["source"] = payload_obj.get("source") if isinstance(payload_obj, dict) else payload_obj
            added_sources.append(entry)
        else:
            refreshed = client.metadata(notebook_id)
            refreshed_record = _first_notebook_record(refreshed.get("payload")) if refreshed.get("ok") else None
            matched_source = _matching_source(refreshed_record, source_item)
            if matched_source is not None:
                notebook_record = refreshed_record or notebook_record
                entry["ok"] = True
                entry["action"] = "metadata_confirmed"
                entry["warning"] = result.get("message") or result.get("error") or "NotebookLM source add returned an error after source appeared in metadata"
                entry["source"] = matched_source
                added_sources.append(entry)
            else:
                entry["kind"] = result.get("kind")
                entry["error"] = result.get("message") or result.get("error") or "NotebookLM source add failed"
                source_errors.append(entry)
        source_results.append(entry)

    if notebook_id:
        refreshed = client.metadata(notebook_id)
        if refreshed.get("ok"):
            notebook_record = _first_notebook_record(refreshed.get("payload")) or notebook_record

    summary_title = title or (str(notebook_record.get("title") or "") if isinstance(notebook_record, dict) else notebook_id)
    if sources:
        if created:
            summary = f"已在 NotebookLM 云端新建笔记本 `{summary_title or notebook_id}` 并添加 {len(added_sources)} 个来源。"
        else:
            summary = f"已向 NotebookLM 云端笔记本 `{summary_title or notebook_id}` 添加 {len(added_sources)} 个来源。"
        if already_present_sources:
            summary += f" 已存在 {len(already_present_sources)} 个来源。"
        if source_errors:
            summary += f" 另外有 {len(source_errors)} 个来源失败。"
    else:
        summary = f"已{'新建' if created else '打开'} NotebookLM 云端笔记本 `{summary_title or notebook_id}`。"

    status = 201 if created else 200
    if source_errors:
        status = 207 if added_sources else 503

    body: dict[str, Any] = {
        "mode": "trae_gateway_cloud_create_notebook",
        "source": "notebooklm_cloud",
        "created": created,
        "action": "create" if created and not sources else ("create_and_add_sources" if created else ("add_sources" if sources else "open")),
        "notebook_id": notebook_id,
        "notebook": notebook_record or _maybe_json_payload(create_result or {}),
        "sources_requested": len(sources),
        "source_count": len(added_sources),
        "added_sources": added_sources,
        "already_present_sources": already_present_sources,
        "source_results": source_results,
        "summary_for_trae": summary,
    }
    if source_errors:
        body["source_errors"] = source_errors
        if added_sources:
            body["partial_success"] = True
    return status, body


class App:
    def __init__(self) -> None:
        self.client = NotebookLMCloudClient.from_env()

    def handle(self, method: str, path: str, payload: dict[str, Any] | None, base_url: str) -> tuple[int, Any, str]:
        route = urlparse(path).path

        if method == "GET" and route in {"/", "/ui"}:
            return 200, "<h1>TRAE NotebookLM Cloud Gateway</h1>", "text/html; charset=utf-8"
        if method == "GET" and route == "/health":
            return 200, {"status": "ok"}, "application/json; charset=utf-8"
        if method == "GET" and route == "/trae-api/openapi.json":
            return 200, build_openapi(base_url), "application/json; charset=utf-8"
        if method == "GET" and route == "/trae-api/capabilities":
            return 200, build_capabilities(), "application/json; charset=utf-8"

        if method == "POST" and route == "/trae-api/query":
            return (*cloud_query_response(self.client, payload), "application/json; charset=utf-8")
        if method == "POST" and route == "/trae-api/codex/task":
            return (*codex_task_response(payload), "application/json; charset=utf-8")
        if method == "POST" and route == "/trae-api/gemini/task":
            return (*gemini_task_response(payload), "application/json; charset=utf-8")
        if method == "POST" and route == "/trae-api/antigravity/chat":
            return (*antigravity_chat_response(payload), "application/json; charset=utf-8")
        if method == "POST" and route == "/trae-api/dual/plan-card":
            return (*dual_plan_card_response(payload), "application/json; charset=utf-8")
        if method == "POST" and route == "/trae-api/antigravity/chat":
            return (*antigravity_chat_response(payload), "application/json; charset=utf-8")
        if method == "POST" and route == "/v1/chat/completions":
            return (*gemini_task_response(payload), "application/json; charset=utf-8")
        if method == "GET" and route == "/v1/models":
            return (*gemini_models_response(), "application/json; charset=utf-8")
        if method == "GET" and route == "/trae-api/cloud/notebooks":
            query = parse_qs(urlparse(path).query)
            limit = query.get("limit", [None])[0]
            return (*cloud_list_response(self.client, None if limit is None else as_int(limit, 100)), "application/json; charset=utf-8")
        if method == "POST" and route == "/trae-api/cloud/notebooks":
            return (*cloud_create_response(self.client, payload), "application/json; charset=utf-8")
        if method == "POST" and route == "/trae-api/cloud/notebooks/search":
            return (*cloud_search_response(self.client, payload), "application/json; charset=utf-8")

        if route.startswith("/trae-api/cloud/notebooks/") and route.endswith("/sources") and method == "POST":
            parts = route.split("/")
            notebook_id = parts[4] if len(parts) == 6 else ""
            source_payload = dict(payload) if isinstance(payload, dict) else {}
            source_payload["notebook_id"] = notebook_id
            return (*cloud_create_response(self.client, source_payload), "application/json; charset=utf-8")
        if route.startswith("/trae-api/cloud/notebooks/") and route.endswith("/ask") and method == "POST":
            parts = route.split("/")
            notebook_id = parts[4] if len(parts) > 4 else ""
            return (*cloud_ask_response(self.client, notebook_id, payload), "application/json; charset=utf-8")
        if route.startswith("/trae-api/cloud/notebooks/") and method == "GET":
            notebook_id = route.split("/", 4)[4]
            return (*cloud_open_response(self.client, notebook_id), "application/json; charset=utf-8")

        return 404, {"error": "route not found", "path": route, "method": method}, "application/json; charset=utf-8"


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def _base_url(self) -> str:
            host = self.headers.get("Host") or "127.0.0.1:8080"
            proto = self.headers.get("X-Forwarded-Proto") or "http"
            return f"{proto}://{host}"

        def _read_json(self) -> dict[str, Any] | None:
            size = int(self.headers.get("Content-Length") or 0)
            if size <= 0:
                return None
            raw = self.rfile.read(size)
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))

        def _write(self, status: int, body: Any, content_type: str) -> None:
            if isinstance(body, str):
                raw = body.encode("utf-8")
            else:
                raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _write_sse(self, body: Any) -> None:
            if isinstance(body, str):
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"choices": [{"message": {"content": body}}]}
            elif isinstance(body, dict):
                data = body
            else:
                data = {}

            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            message = choice.get("message") or {}
            content = message.get("content") or ""

            completion_id = data.get("id") or "chatcmpl-gateway"
            created = data.get("created") or 0
            model = data.get("model") or "gemini-3.6-flash"

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization",
            )
            self.send_header(
                "Access-Control-Allow-Methods",
                "GET, POST, OPTIONS",
            )
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(chunk: dict) -> None:
                raw = (
                    "data: "
                    + json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
                    + "\n\n"
                ).encode("utf-8")
                self.wfile.write(raw)
                self.wfile.flush()

            emit({
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant"},
                    "finish_reason": None,
                }],
            })

            calls = message.get("tool_calls") or []

            if isinstance(calls, list):

                for call_index, call in enumerate(calls):

                    if not isinstance(call, dict):

                        continue

                    function = call.get("function") or {}

                    if not isinstance(function, dict):

                        function = {}

                    arguments = function.get("arguments", "")

                    if not isinstance(arguments, str):

                        arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))

                    tool_delta = {

                        "index": call_index,

                        "id": str(call.get("id") or f"call_{call_index}"),

                        "type": str(call.get("type") or "function"),

                        "extra_content": call.get("extra_content") or {},
                        "function": {

                            "name": str(function.get("name") or ""),

                            "arguments": arguments,

                        },

                    }

                    emit({

                        "id": completion_id,

                        "object": "chat.completion.chunk",

                        "created": created,

                        "model": model,

                        "choices": [{

                            "index": 0,

                            "delta": {"tool_calls": [tool_delta]},

                            "finish_reason": None,

                        }],

                    })


            content = str(content)
            for i in range(0, len(content), 64):
                emit({
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": content[i:i + 64]},
                        "finish_reason": None,
                    }],
                })

            emit({
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": choice.get("finish_reason") or "stop",
                }],
            })

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802
            status, body, content_type = app.handle("GET", self.path, None, self._base_url())
            self._write(status, body, content_type)

        def do_POST(self) -> None:  # noqa: N802
            # UPLOAD_PDF_ENDPOINT_V1
            from urllib.parse import urlparse as _upload_urlparse

            _upload_parts = _upload_urlparse(self.path).path.split("/")
            if (
                len(_upload_parts) == 7
                and _upload_parts[1:4] == ["trae-api", "cloud", "notebooks"]
                and _upload_parts[5:] == ["sources", "file"]
            ):
                from gateway_pdf_upload import handle_request as _handle_pdf_upload

                status, body, content_type = _handle_pdf_upload(
                    self,
                    app.client,
                )
                self._write(status, body, content_type)
                return

            payload = self._read_json()
            status, body, content_type = app.handle(
                "POST",
                self.path,
                payload,
                self._base_url(),
            )

            route = self.path.split("?", 1)[0]
            if (
                route == "/v1/chat/completions"
                and isinstance(payload, dict)
                and payload.get("stream") is True
                and status == 200
            ):
                self._write_sse(body)
            else:
                self._write(status, body, content_type)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._write(204, b"", "application/json; charset=utf-8")

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="TRAE NotebookLM cloud gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    app = App()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"Serving on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
