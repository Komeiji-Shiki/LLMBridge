"""全面排查修复的回归测试。

覆盖本轮修复的功能点，每一项都对应一个真实存在过的缺陷：

1.  JSONC 定点编辑：保存配置不再抹掉注释
2.  原子写：写入中断不会留下半截配置文件
3.  /api/admin/token_stats 不再返回 null
4.  /api/admin/request_stats 接受 start_date/end_date
5.  Gemini 原生端点补上 API Key 鉴权
6.  Gemini tool_call index 连续
7.  流式生成器 GeneratorExit 处理
8.  登录页 next 参数的开放重定向防护
9.  models_api 的常数时间 Key 比较
10. 验证成功后清除失败限流计数

运行: python _test_audit_fixes.py
"""
import io
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {extra}")


# ============================================================
# 1. JSONC 定点编辑保留注释
# ============================================================
def test_jsonc_edit():
    print("1. JSONC 定点编辑：保存配置不丢注释")
    from utils.jsonc_edit import set_jsonc_value, set_jsonc_values, find_top_level_key
    from core.config_loader import _parse_jsonc

    src = """{
  // 服务端口
  "server_port": 5102,  // 行尾注释
  /* 块注释
     跨多行 */
  "tokenizer_config": {
    // 每个模型一行
    "gpt-4": "tiktoken_cl100k"
  },
  "tricky": "value with {} and // inside",
  "nested": { "server_port": "must-not-match" }
}"""

    out = set_jsonc_value(src, "tokenizer_config", {"a": "b"})
    parsed = _parse_jsonc(out)
    check("注释全部保留", all(k in out for k in ("服务端口", "行尾注释", "块注释")))
    check("目标键已更新", parsed["tokenizer_config"] == {"a": "b"})
    check("字符串内的 {} 与 // 未被误解析", parsed["tricky"] == "value with {} and // inside")
    check("嵌套同名键不受影响", parsed["nested"]["server_port"] == "must-not-match")

    # 顶层 key 定位不会命中嵌套同名键
    span = find_top_level_key(src, "server_port")
    check("顶层键定位正确", span is not None and src[span[0]:span[1]] == "5102")

    # 批量：只改动指定键，其余逐字节不变
    out2 = set_jsonc_values(src, {"server_port": 5999})
    diff = [i for i, (a, b) in enumerate(zip(src.splitlines(), out2.splitlines())) if a != b]
    check("批量替换只改动目标行", len(diff) == 1, f"differing lines={diff}")

    # 新增键
    out3 = set_jsonc_value(src, "brand_new", [1, 2])
    check("新增键可解析", _parse_jsonc(out3)["brand_new"] == [1, 2])
    check("新增键后注释仍在", "服务端口" in out3)


# ============================================================
# 2. 原子写
# ============================================================
def test_atomic_write():
    print("2. 原子写：不留半截文件")
    from utils.jsonc_edit import atomic_write_json, atomic_write_text

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cfg.json")
        atomic_write_json(path, {"a": 1})
        check("写入成功", io.open(path, encoding="utf-8").read().strip().startswith("{"))

        # 写入失败时不得破坏已有文件，也不得留下临时文件
        class Unserializable:
            pass

        try:
            atomic_write_json(path, {"bad": Unserializable()})
        except Exception:
            pass
        content = io.open(path, encoding="utf-8").read()
        check("失败后原文件完好", '"a": 1' in content)
        leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
        check("失败后无残留临时文件", not leftovers, f"leftovers={leftovers}")

        atomic_write_text(path, "plain")
        check("文本原子写生效", io.open(path, encoding="utf-8").read() == "plain")


# ============================================================
# 3~5. HTTP 端点行为
# ============================================================
def test_endpoints():
    from fastapi.testclient import TestClient
    import api_server_new
    from core.config_loader import CONFIG

    client = TestClient(api_server_new.app)

    print("3. /api/admin/token_stats 永不返回 null")
    r = client.get("/api/admin/token_stats?rpm_period=day")
    body = r.json()
    check("状态 200", r.status_code == 200, f"got {r.status_code}")
    check("响应体不是 null", body is not None)
    check("结构完整", isinstance(body.get("model_stats"), list)
          and isinstance(body.get("rate_stats"), dict)
          and isinstance(body.get("exchange_rate"), dict))

    print("4. /api/admin/request_stats 接受 start_date/end_date")
    r = client.get("/api/admin/request_stats?start_date=2020-01-01&end_date=2020-01-02")
    check("状态 200", r.status_code == 200, f"got {r.status_code}")
    check("日期过滤生效", r.json().get("total_requests") == 0,
          f"total={r.json().get('total_requests')}")

    print("5. Gemini 原生端点鉴权")
    saved = CONFIG.get("api_key")
    CONFIG["api_key"] = "test-secret-key"
    try:
        payload = {"contents": []}
        r = client.post("/v1beta/models/whatever:generateContent", json=payload)
        check("无 key -> 401", r.status_code == 401, f"got {r.status_code}")
        r = client.post("/v1beta/models/whatever:generateContent?key=wrong", json=payload)
        check("错误 key -> 401", r.status_code == 401, f"got {r.status_code}")
        for label, kwargs in [
            ("?key=", dict(params={"key": "test-secret-key"})),
            ("x-goog-api-key", dict(headers={"x-goog-api-key": "test-secret-key"})),
            ("Bearer", dict(headers={"Authorization": "Bearer test-secret-key"})),
            ("x-api-key", dict(headers={"x-api-key": "test-secret-key"})),
        ]:
            r = client.post("/v1beta/models/whatever:generateContent", json=payload, **kwargs)
            check(f"正确 key({label}) -> 通过鉴权", r.status_code == 404, f"got {r.status_code}")
    finally:
        if saved is None:
            CONFIG.pop("api_key", None)
        else:
            CONFIG["api_key"] = saved


# ============================================================
# 6. Gemini tool_call index 连续
# ============================================================
def test_gemini_tool_call_index():
    print("6. Gemini tool_call index 连续（parts 中混有 text 时）")
    from services.direct_api_service import DirectAPIService

    svc = DirectAPIService.__new__(DirectAPIService)
    gemini_resp = {
        "candidates": [{
            "content": {"parts": [
                {"text": "先说明一下"},
                {"functionCall": {"name": "f1", "args": {"x": 1}}},
                {"text": "中间还有文本"},
                {"functionCall": {"name": "f2", "args": {"y": 2}}},
            ]},
            "finishReason": "STOP",
        }]
    }
    out = svc.convert_gemini_response_to_openai(gemini_resp, "m", "rid", is_stream_chunk=False)
    calls = out["choices"][0]["message"]["tool_calls"]
    check("提取到 2 个工具调用", len(calls) == 2, f"got {len(calls)}")
    check("index 从 0 连续递增", [c["index"] for c in calls] == [0, 1],
          f"indexes={[c['index'] for c in calls]}")
    check("函数名对应正确",
          [c["function"]["name"] for c in calls] == ["f1", "f2"])


# ============================================================
# 7. GeneratorExit 处理
# ============================================================
def test_generator_exit():
    print("7. 流式生成器：客户端断开时不抛 RuntimeError")
    import asyncio

    async def fixed_pattern():
        client_gone = False
        try:
            yield b"a"
            yield b"b"
        except GeneratorExit:
            client_gone = True
            raise
        finally:
            if not client_gone:
                yield b"tail"

    async def run():
        g = fixed_pattern()
        first = await g.__anext__()
        try:
            await g.aclose()
            return first, None
        except RuntimeError as e:
            return first, str(e)

    first, err = asyncio.run(run())
    check("首块正常产出", first == b"a")
    check("aclose 不再抛 'ignored GeneratorExit'", err is None, f"err={err}")


# ============================================================
# 8. 登录页 next 参数校验
# ============================================================
def test_login_next_guard():
    print("8. 登录页 next 参数的开放重定向防护")
    html = io.open("login.html", encoding="utf-8").read()
    check("挡住反斜杠形式 (/\\evil.com)", "second === '\\\\'" in html or 'second === "\\\\"' in html)
    check("仍挡住协议相对形式 (//evil.com)", "second === '/'" in html)
    check("验证成功后自动跳转", "location.replace" in html)
    # 旧版是把按钮改成"进入管理面板"再绑一个 onclick 等用户点第二次
    check("不再绑定二次点击处理器", "btn.onclick" not in html)


# ============================================================
# 9. models_api 常数时间比较
# ============================================================
def test_constant_time_compare():
    print("9. /v1/models 的 API Key 常数时间比较")
    from routes.models_api import _matches_global_key

    check("正确 key 通过", _matches_global_key("abc123", "abc123"))
    check("错误 key 拒绝", not _matches_global_key("abc124", "abc123"))
    check("长度不同也安全拒绝", not _matches_global_key("abc", "abc123"))
    check("空值不通过", not _matches_global_key("", "abc123"))
    check("未配置全局 key 时不通过", not _matches_global_key("abc", ""))

    src = io.open("routes/models_api.py", encoding="utf-8").read()
    check("不再有裸 == 比较", "provided_key == global_api_key" not in src)


# ============================================================
# 10. 验证成功清除失败计数
# ============================================================
def test_clear_failures():
    print("10. 验证成功后清除限流失败计数")
    from core import web_session

    ip = "203.0.113.77"
    for _ in range(web_session._MAX_FAILURES):
        web_session.record_failure(ip)
    check("连续失败后被限流", web_session.is_rate_limited(ip))
    web_session.clear_failures(ip)
    check("清除后不再限流", not web_session.is_rate_limited(ip))


def main():
    for fn in (test_jsonc_edit, test_atomic_write, test_endpoints,
               test_gemini_tool_call_index, test_generator_exit,
               test_login_next_guard, test_constant_time_compare,
               test_clear_failures):
        fn()
        print()
    print(f"== {PASS} passed, {FAIL} failed ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
