
"""JEV (TypeSafe System One) 客户端 — 缠论工作流快速判定层.

用法:
    from jev_client import jev_judge
    ans = jev_judge(state={...}, questions={...}, timeout=10)
    # ans["direction"]["choice"], ans["direction"]["confidence"], ...

API: POST https://api.typesafe.ai/v1/systemone  (model=jev-latest)
Key: ~/.typesafe_key  (或环境变量 TYPESAFE_API_KEY)
"""
import json
import os
import time
import urllib.request
import urllib.error

_API = "https://api.typesafe.ai/v1/systemone"
_MODEL = "jev-latest"
_DEFAULT_TIMEOUT = 12.0


def _load_key() -> str:
    env = os.environ.get("TYPESAFE_API_KEY")
    if env:
        return env.strip()
    kp = os.path.expanduser("~/.typesafe_key")
    if os.path.exists(kp):
        with open(kp) as f:
            k = f.read().strip()
        if k:
            return k
    raise RuntimeError("JEV key 缺失: 设 TYPESAFE_API_KEY 或 ~/.typesafe_key")


class JevError(Exception):
    pass


def jev_judge(state, questions, timeout=None, model=None):
    """一次并行判定. questions: {name: {"type","instructions","criteria"|...}}.

    返回 {name: {"type":..., "choice"|"score"|"noul":..., "confidence":..., "probabilities":...}}
    失败抛 JevError.
    """
    t = timeout or _DEFAULT_TIMEOUT
    key = _load_key()
    body = {
        "model": model or _MODEL,
        "state": state,
        "questions": questions,
    }
    req = urllib.request.Request(
        _API,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=t) as resp:
            raw = resp.read().decode("utf-8")
            dt = (time.time() - t0) * 1000
            data = json.loads(raw)
            data["_latency_ms"] = dt
            return data
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise JevError(f"JEV HTTP {e.code}: {detail}") from e
    except Exception as e:  # noqa: BLE001
        raise JevError(f"JEV 调用失败: {type(e).__name__}: {e}") from e


def _fmt_answer(a):
    t = a.get("type")
    if t == "choice":
        return f"choice={a.get('choice')} conf={a.get('confidence'):.2f}"
    if t == "score":
        return f"score={a.get('score'):.3f} conf={a.get('confidence'):.2f}"
    if t == "noul":
        return f"noul={a.get('noul'):.3f}"
    return json.dumps(a, ensure_ascii=False)[:120]


def quick_report(data):
    """把 JEV 返回压成一行便于日志."""
    answers = data.get("answers", {})
    parts = [f"{k}={_fmt_answer(v)}" for k, v in answers.items()]
    return " | ".join(parts)


if __name__ == "__main__":
    # 自检
    d = jev_judge(
        state={"test": "hello", "n": 1},
        questions={
            "ok": {"type": "noul", "instructions": "state 里 n 等于 1"},
        },
        timeout=15,
    )
    print(json.dumps(d, ensure_ascii=False, indent=2))
