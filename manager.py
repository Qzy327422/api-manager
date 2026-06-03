"""
API 代理与故障转移核心模块
- 管理多个上游 API
- 提供统一 OpenAI 兼容入口（任意路径透传）
- 支持按条件触发：超时、空响应、关键词命中、HTTP 异常状态、连接异常
- 支持每个 API 自定义重试次数，以及失败后切换到下一个或指定 API
- 支持可选上游代理（默认端口 7897）
- 支持检测上游模型列表，并把请求中的 model=default 改写为该上游选中的真实模型
"""
import json
import os
import threading
import time
from itertools import count
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
from flask import Flask, Response, request


class APIManager:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.lock = threading.Lock()
        self.config = self._load()
        self.request_counter = count(1)
        self.request_semaphore = threading.BoundedSemaphore(max(1, int(self.config.get("max_concurrent_requests", 1) or 1)))
        self.log_callback: Optional[Callable[[str], None]] = None
        self._server_thread: Optional[threading.Thread] = None
        self._server = None
        self._running = False

    def _default_config(self) -> dict:
        return {
            "listen_host": "127.0.0.1",
            "listen_port": 8000,
            "timeout": 300,
            "connect_timeout": 10,
            "read_timeout": 300,
            "fail_keywords": ["请求失败", "error", "failed"],
            "outbound_proxy": {
                "enabled": False,
                "scheme": "http",
                "host": "127.0.0.1",
                "port": 7897,
            },
            "max_concurrent_requests": 1,
            "auto_start_service": False,
            "ui": {
                "api_list_columns": {
                    "index": 60,
                    "name": 120,
                    "url": 320,
                    "enabled": 60,
                    "policy": 400,
                }
            },
            "apis": [],
        }

    def _default_api_policy(self) -> dict:
        default_connect = int(self.config.get("connect_timeout", 10)) if hasattr(self, "config") else 10
        default_read = int(self.config.get("read_timeout", self.config.get("timeout", 300))) if hasattr(self, "config") else 300
        return {
            "timeout_seconds": default_read,
            "connect_timeout_seconds": default_connect,
            "read_timeout_seconds": default_read,
            "trigger_timeout": True,
            "trigger_empty_response": True,
            "trigger_keywords": True,
            "keywords": [],
            "retry_times": 0,
            "action": "next",
            "target_name": "",
            "target_index": -1,
        }

    def _default_model_config(self) -> dict:
        return {
            "available_models": [],
            "allowed_models": [],
            "selected_model": "",
            "default_alias": "default",
        }

    def _normalize_api(self, api: dict) -> dict:
        item = {
            "name": api.get("name", ""),
            "url": str(api.get("url", "")).rstrip("/"),
            "headers": api.get("headers") or {},
            "enabled": bool(api.get("enabled", True)),
        }
        policy = self._default_api_policy()
        policy.update(api.get("policy") or {})
        if not isinstance(policy.get("keywords"), list):
            policy["keywords"] = []
        policy["timeout_seconds"] = int(policy.get("timeout_seconds", self.config.get("timeout", 30)) or 30)
        policy["connect_timeout_seconds"] = max(1, int(policy.get("connect_timeout_seconds", 10) or 10))
        policy["read_timeout_seconds"] = max(1, int(policy.get("read_timeout_seconds", max(policy["timeout_seconds"], 300)) or max(policy["timeout_seconds"], 300)))
        policy["retry_times"] = max(0, int(policy.get("retry_times", 0) or 0))
        policy["target_index"] = int(policy.get("target_index", -1) or -1)
        item["policy"] = policy

        model_cfg = self._default_model_config()
        model_cfg.update(api.get("model_config") or {})
        if not isinstance(model_cfg.get("available_models"), list):
            model_cfg["available_models"] = []
        model_cfg["available_models"] = [str(x).strip() for x in model_cfg.get("available_models", []) if str(x).strip()]
        if not isinstance(model_cfg.get("allowed_models"), list):
            model_cfg["allowed_models"] = []
        model_cfg["allowed_models"] = [str(x).strip() for x in model_cfg.get("allowed_models", []) if str(x).strip()]
        if not model_cfg["allowed_models"]:
            selected_fallback = str(model_cfg.get("selected_model", "") or "").strip()
            if selected_fallback:
                model_cfg["allowed_models"] = [selected_fallback]
        model_cfg["selected_model"] = str(model_cfg.get("selected_model", "") or "").strip()
        model_cfg["default_alias"] = str(model_cfg.get("default_alias", "default") or "default").strip() or "default"
        item["model_config"] = model_cfg
        return item

    def _normalize_config(self, data: dict) -> dict:
        base = self._default_config()
        base.update(data or {})
        if not isinstance(base.get("fail_keywords"), list):
            base["fail_keywords"] = ["请求失败", "error", "failed"]

        proxy = self._default_config()["outbound_proxy"]
        proxy.update(base.get("outbound_proxy") or {})
        proxy["port"] = int(proxy.get("port", 7897) or 7897)
        base["outbound_proxy"] = proxy

        base["timeout"] = int(base.get("timeout", base.get("read_timeout", 300)) or 300)
        base["connect_timeout"] = max(1, int(base.get("connect_timeout", 10) or 10))
        base["read_timeout"] = max(1, int(base.get("read_timeout", base.get("timeout", 300)) or 300))
        base["timeout"] = base["read_timeout"]
        base["max_concurrent_requests"] = max(1, int(base.get("max_concurrent_requests", 1) or 1))
        base["auto_start_service"] = bool(base.get("auto_start_service", False))
        ui = self._default_config()["ui"]
        ui.update(base.get("ui") or {})
        base["ui"] = ui
        raw_apis = base.get("apis") or []
        self.config = base
        base["apis"] = [self._normalize_api(api) for api in raw_apis]
        return base

    def _load(self) -> dict:
        if not os.path.exists(self.config_path):
            cfg = self._normalize_config({})
            self._save(cfg)
            return cfg
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._normalize_config(data)
        except Exception:
            return self._normalize_config({})

    def _save(self, cfg: Optional[dict] = None):
        with self.lock:
            data = self._normalize_config(cfg if cfg is not None else self.config)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.config = data

    def save(self):
        self._save()

    def log(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        if self.log_callback:
            try:
                self.log_callback(line)
            except Exception:
                pass
        else:
            print(line)

    def add_api(self, name: str, url: str, headers: Optional[dict] = None,
                enabled: bool = True, policy: Optional[dict] = None,
                model_config: Optional[dict] = None):
        item = self._normalize_api({
            "name": name,
            "url": url,
            "headers": headers or {},
            "enabled": enabled,
            "policy": policy or {},
            "model_config": model_config or {},
        })
        with self.lock:
            self.config["apis"].append(item)
        self._save()

    def update_api(self, index: int, **fields):
        with self.lock:
            if 0 <= index < len(self.config["apis"]):
                current = dict(self.config["apis"][index])
                current.update(fields)
                self.config["apis"][index] = self._normalize_api(current)
        self._save()

    def remove_api(self, index: int):
        with self.lock:
            if 0 <= index < len(self.config["apis"]):
                self.config["apis"].pop(index)
        self._save()

    def move_api(self, index: int, direction: int):
        with self.lock:
            apis = self.config["apis"]
            new_idx = index + direction
            if 0 <= index < len(apis) and 0 <= new_idx < len(apis):
                apis[index], apis[new_idx] = apis[new_idx], apis[index]
        self._save()

    def set_keywords(self, keywords):
        with self.lock:
            self.config["fail_keywords"] = [k for k in keywords if str(k).strip()]
        self._save()

    def set_listen(self, host: str, port: int, connect_timeout: int, read_timeout: int):
        with self.lock:
            self.config["listen_host"] = host
            self.config["listen_port"] = int(port)
            self.config["connect_timeout"] = max(1, int(connect_timeout))
            self.config["read_timeout"] = max(1, int(read_timeout))
            self.config["timeout"] = max(1, int(read_timeout))
        self._save()

    def set_proxy(self, enabled: bool, scheme: str, host: str, port: int):
        with self.lock:
            self.config["outbound_proxy"] = {
                "enabled": bool(enabled),
                "scheme": scheme or "http",
                "host": host or "127.0.0.1",
                "port": int(port),
            }
        self._save()

    def set_concurrency(self, max_concurrent_requests: int):
        value = max(1, int(max_concurrent_requests or 1))
        with self.lock:
            self.config["max_concurrent_requests"] = value
            self.request_semaphore = threading.BoundedSemaphore(value)
        self._save()

    def _rlog(self, req_id: int, msg: str):
        self.log(f"[请求#{req_id}] {msg}")

    def detect_models(self, index: int) -> list:
        if not (0 <= index < len(self.config.get("apis", []))):
            raise ValueError("API 索引无效")
        api = self.config["apis"][index]
        models = self.detect_models_for_api(api)
        model_cfg = dict(api.get("model_config") or {})
        model_cfg["available_models"] = models
        if models and not model_cfg.get("selected_model"):
            model_cfg["selected_model"] = models[0]
        self.update_api(index, model_config=model_cfg)
        return models

    def detect_models_for_api(self, api: dict) -> list:
        api = self._normalize_api(api)
        proxies = self._get_proxies()
        timeout_seconds = int((api.get("policy") or {}).get("read_timeout_seconds", self.config.get("timeout", 30)) or 30)
        connect_timeout = int((api.get("policy") or {}).get("connect_timeout_seconds", 10) or 10)
        target = self._join_url(api["url"], "models")
        headers = dict(api.get("headers") or {})
        self.log(f"-> 检测模型 [{api['name']}] GET {target}")
        resp = requests.get(target, headers=headers, timeout=(connect_timeout, timeout_seconds), allow_redirects=False, proxies=proxies)
        resp.raise_for_status()
        data = resp.json()
        models = []
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            for item in data["data"]:
                if isinstance(item, dict) and item.get("id"):
                    models.append(str(item["id"]))
        models = sorted(set(models))
        self.log(f"   ✓ [{api['name']}] 检测到 {len(models)} 个模型")
        return models

    def _join_url(self, base: str, subpath: str) -> str:
        base = base.rstrip("/")
        sub = (subpath or "").lstrip("/")
        if not sub:
            return base
        base_path = urlparse(base).path.strip("/")
        if base_path:
            base_segs = base_path.split("/")
            sub_segs = sub.split("/")
            overlap = 0
            max_check = min(len(base_segs), len(sub_segs))
            for k in range(max_check, 0, -1):
                if base_segs[-k:] == sub_segs[:k]:
                    overlap = k
                    break
            if overlap:
                sub = "/".join(sub_segs[overlap:])
        return f"{base}/{sub}" if sub else base

    def _get_api_keywords(self, api: dict) -> list:
        policy = api.get("policy") or {}
        kws = [str(x).strip() for x in (policy.get("keywords") or []) if str(x).strip()]
        return kws or [str(x).strip() for x in self.config.get("fail_keywords", []) if str(x).strip()]

    def _contains_fail_keyword(self, text: str, api: dict) -> Optional[str]:
        for kw in self._get_api_keywords(api):
            if kw and kw in text:
                return kw
        return None

    def _get_proxies(self) -> Optional[dict]:
        proxy = self.config.get("outbound_proxy") or {}
        if not proxy.get("enabled"):
            return None
        scheme = str(proxy.get("scheme", "http") or "http").strip().lower()
        host = str(proxy.get("host", "127.0.0.1") or "127.0.0.1").strip()
        port = int(proxy.get("port", 7897) or 7897)
        proxy_url = f"{scheme}://{host}:{port}"
        return {"http": proxy_url, "https": proxy_url}

    def _resolve_next_index(self, enabled_apis: list, current_idx: int, visited: set) -> Optional[int]:
        for i in range(current_idx + 1, len(enabled_apis)):
            if i not in visited:
                return i
        return None

    def _resolve_specific_index(self, enabled_apis: list, api: dict, visited: set) -> Optional[int]:
        policy = api.get("policy") or {}
        target_name = str(policy.get("target_name", "") or "").strip()
        if target_name:
            for i, item in enumerate(enabled_apis):
                if i not in visited and item.get("name", "") == target_name:
                    return i
        target_index = int(policy.get("target_index", -1) or -1)
        if 0 <= target_index < len(enabled_apis) and target_index not in visited:
            return target_index
        return None

    def _decide_next_index(self, enabled_apis: list, current_idx: int, visited: set) -> Optional[int]:
        api = enabled_apis[current_idx]
        action = str((api.get("policy") or {}).get("action", "next") or "next").strip().lower()
        if action == "specific":
            target = self._resolve_specific_index(enabled_apis, api, visited)
            if target is not None:
                return target
            self.log(f"   ! [{api['name']}] 指定切换目标无效，改为切换下一个")
        return self._resolve_next_index(enabled_apis, current_idx, visited)

    def _make_failure(self, kind: str, detail: str = "") -> dict:
        return {"kind": kind, "detail": detail}

    def _should_fail_response(self, resp: requests.Response, api: dict) -> Optional[dict]:
        policy = api.get("policy") or {}
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            return self._make_failure("status", f"状态码 {resp.status_code}")
        if policy.get("trigger_empty_response", True) and len(resp.content or b"") == 0:
            return self._make_failure("empty", "返回内容为空")
        if policy.get("trigger_keywords", True):
            try:
                text_preview = resp.content.decode(resp.encoding or "utf-8", errors="ignore")
            except Exception:
                text_preview = ""
            hit = self._contains_fail_keyword(text_preview, api)
            if hit:
                return self._make_failure("keyword", f"命中关键词 '{hit}'")
        return None

    def _extract_requested_model(self, body: bytes) -> str:
        if not body:
            return ""
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("model", "") or "").strip()

    def _is_stream_request(self, body: bytes) -> bool:
        if not body:
            return False
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return False
        return isinstance(payload, dict) and bool(payload.get("stream", False))

    def _rewrite_model_for_api(self, api: dict, body: bytes, req_id: Optional[int] = None) -> tuple[bytes, str, Optional[str]]:
        """
        返回: (转发body, 实际发送模型名, 错误信息)
        严格单绑定规则：
        - 每个上游可配置 allowed_models 作为允许模型列表
        - 请求 default_alias 时，自动改写为 selected_model
        - 请求其他模型名时，只有在 allowed_models 中才允许转发
        - 否则直接判定模型不匹配并切换，不误发到上游
        """
        requested_model = self._extract_requested_model(body)
        if not requested_model:
            return body, "", None

        model_cfg = api.get("model_config") or {}
        alias = str(model_cfg.get("default_alias", "default") or "default").strip() or "default"
        selected = str(model_cfg.get("selected_model", "") or "").strip()
        available = [str(x).strip() for x in (model_cfg.get("available_models") or []) if str(x).strip()]
        allowed = [str(x).strip() for x in (model_cfg.get("allowed_models") or []) if str(x).strip()]
        if not allowed and selected:
            allowed = [selected]

        if not selected:
            return body, requested_model, "当前上游未绑定模型"

        if available and selected not in available:
            return body, requested_model, f"已绑定模型 {selected} 不在当前上游已检测模型列表中"

        if requested_model == alias:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                return body, requested_model, None
            if not isinstance(payload, dict):
                return body, requested_model, None
            payload["model"] = selected
            if req_id is None:
                self.log(f"   ↺ [{api['name']}] 模型别名 {alias} -> {selected}")
            else:
                self._rlog(req_id, f"   ↺ [{api['name']}] 模型别名 {alias} -> {selected}")
            return json.dumps(payload, ensure_ascii=False).encode("utf-8"), selected, None

        if requested_model != selected and requested_model not in allowed:
            return body, requested_model, f"当前上游仅允许模型 {', '.join(allowed) if allowed else selected}，不接受 {requested_model}"

        return body, requested_model, None

    def forward(self, subpath: str, method: str, headers: dict, params: dict, body: bytes):
        req_id = next(self.request_counter)
        acquired = self.request_semaphore.acquire(timeout=300)
        if not acquired:
            return Response("服务器繁忙：等待并发队列超时", status=503)
        try:
            return self._forward_locked(req_id, subpath, method, headers, params, body)
        finally:
            try:
                self.request_semaphore.release()
            except ValueError:
                pass

    def _forward_locked(self, req_id: int, subpath: str, method: str, headers: dict, params: dict, body: bytes):
        enabled_apis = [a for a in self.config["apis"] if a.get("enabled", True)]
        if not enabled_apis:
            return Response("没有可用的上游 API", status=503)

        is_stream = self._is_stream_request(body)
        last_response: Optional[requests.Response] = None
        last_err: Optional[str] = None
        visited = set()
        current_idx = 0
        proxies = self._get_proxies()

        forwardable_headers = {
            k: v for k, v in headers.items()
            if k.lower() not in {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
        }

        while current_idx is not None and current_idx not in visited and 0 <= current_idx < len(enabled_apis):
            visited.add(current_idx)
            api = enabled_apis[current_idx]
            policy = api.get("policy") or {}
            connect_timeout = int(policy.get("connect_timeout_seconds", 10) or 10)
            read_timeout = int(policy.get("read_timeout_seconds", self.config.get("timeout", 30)) or 30)
            timeout_seconds = read_timeout
            retries = max(0, int(policy.get("retry_times", 0) or 0))
            target = self._join_url(api["url"], subpath)
            merged_headers = dict(forwardable_headers)
            merged_headers.update(api.get("headers") or {})
            send_body, actual_model, model_err = self._rewrite_model_for_api(api, body, req_id=req_id)
            if model_err:
                last_err = model_err
                self._rlog(req_id, f"   x [{api['name']}] {model_err}")
                next_idx = self._decide_next_index(enabled_apis, current_idx, visited)
                if next_idx is None:
                    self._rlog(req_id, f"!! [{api['name']}] 无可继续切换的目标")
                    break
                action_name = "指定目标" if str((api.get("policy") or {}).get("action", "next")) == "specific" else "下一个"
                self._rlog(req_id, f"   -> [{api['name']}] 模型不匹配，切换到 {action_name}: [{enabled_apis[next_idx]['name']}]")
                current_idx = next_idx
                continue
            if send_body != body and "Content-Length" in merged_headers:
                merged_headers.pop("Content-Length", None)

            for attempt in range(retries + 1):
                round_text = f"第 {attempt + 1}/{retries + 1} 次"
                model_text = f" model={actual_model}" if actual_model else ""
                self._rlog(req_id, f"-> 尝试 [{api['name']}] {round_text} {method} {target}{model_text}")
                try:
                    resp = requests.request(
                        method=method,
                        url=target,
                        headers=merged_headers,
                        params=params,
                        data=send_body,
                        timeout=(connect_timeout, read_timeout),
                        allow_redirects=False,
                        proxies=proxies,
                        stream=is_stream,
                    )
                except requests.Timeout:
                    last_err = f"超时 {timeout_seconds}s"
                    self._rlog(req_id, f"   x [{api['name']}] {last_err}")
                    if attempt < retries and policy.get("trigger_timeout", True):
                        self._rlog(req_id, f"   ↻ [{api['name']}] 满足超时重试条件，准备重试")
                        continue
                    break
                except requests.RequestException as e:
                    last_err = f"连接异常: {e}"
                    self._rlog(req_id, f"   x [{api['name']}] {last_err}")
                    if attempt < retries:
                        self._rlog(req_id, f"   ↻ [{api['name']}] 连接异常，准备重试")
                        continue
                    break

                last_response = resp
                if is_stream and resp.status_code < 400:
                    self._rlog(req_id, f"   ✓ [{api['name']}] 流式响应已连接 ({resp.status_code})")
                    return self._build_stream_response(req_id, api, resp)

                failure = self._should_fail_response(resp, api)
                if failure is None:
                    self._rlog(req_id, f"   ✓ [{api['name']}] 成功 ({resp.status_code})")
                    return self._build_flask_response(resp)

                last_err = failure["detail"]
                self._rlog(req_id, f"   x [{api['name']}] {last_err}")
                should_retry = attempt < retries and (
                    (failure["kind"] == "timeout" and policy.get("trigger_timeout", True)) or
                    failure["kind"] in {"empty", "keyword", "status", "network"}
                )
                if should_retry:
                    self._rlog(req_id, f"   ↻ [{api['name']}] 满足重试条件，准备重试")
                    continue
                break

            next_idx = self._decide_next_index(enabled_apis, current_idx, visited)
            if next_idx is None:
                self._rlog(req_id, f"!! [{api['name']}] 无可继续切换的目标")
                break
            action_name = "指定目标" if str((api.get("policy") or {}).get("action", "next")) == "specific" else "下一个"
            self._rlog(req_id, f"   -> [{api['name']}] 失败后切换到 {action_name}: [{enabled_apis[next_idx]['name']}]")
            current_idx = next_idx

        self._rlog(req_id, f"!! 所有上游均失败：{last_err}")
        if last_response is not None:
            return self._build_flask_response(last_response)
        return Response(f"所有上游 API 均失败: {last_err}", status=502)

    def _configured_model_list(self) -> list:
        """返回本程序已配置允许对外暴露的模型列表，兼容 OpenAI /v1/models。"""
        models = {}
        for api in self.config.get("apis", []):
            if not api.get("enabled", True):
                continue
            cfg = api.get("model_config") or {}
            alias = str(cfg.get("default_alias", "default") or "default").strip() or "default"
            selected = str(cfg.get("selected_model", "") or "").strip()
            allowed = [str(x).strip() for x in (cfg.get("allowed_models") or []) if str(x).strip()]
            if selected and selected not in allowed:
                allowed.append(selected)
            if selected:
                models.setdefault(alias, {"id": alias, "object": "model", "owned_by": "api-manager", "apis": []})["apis"].append(api.get("name", ""))
            for model in allowed:
                models.setdefault(model, {"id": model, "object": "model", "owned_by": "api-manager", "apis": []})["apis"].append(api.get("name", ""))
        result = []
        for item in models.values():
            item["apis"] = sorted(set([x for x in item.get("apis", []) if x]))
            result.append(item)
        return sorted(result, key=lambda x: x["id"])

    def _build_stream_response(self, req_id: int, api: dict, resp: requests.Response) -> Response:
        excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
        headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excluded]
        if not any(k.lower() == "content-type" for k, _ in headers):
            headers.append(("Content-Type", "text/event-stream; charset=utf-8"))
        headers.append(("Cache-Control", "no-cache"))
        headers.append(("X-Accel-Buffering", "no"))

        def generate():
            try:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            except requests.RequestException as e:
                self._rlog(req_id, f"   x [{api['name']}] 流式读取中断: {e}")
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                self._rlog(req_id, f"   ✓ [{api['name']}] 流式响应结束")

        return Response(generate(), status=resp.status_code, headers=headers)

    def _build_flask_response(self, resp: requests.Response) -> Response:
        excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
        headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excluded]
        return Response(resp.content, status=resp.status_code, headers=headers)

    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return
        app = Flask(__name__)

        @app.route("/__status", methods=["GET"])
        def status():
            return {
                "service": "api-manager",
                "proxy": self.config.get("outbound_proxy", {}),
                "models": self._configured_model_list(),
                "apis": [
                    {
                        "name": a["name"],
                        "url": a["url"],
                        "enabled": a.get("enabled", True),
                        "policy": a.get("policy", {}),
                        "model_config": a.get("model_config", {}),
                    }
                    for a in self.config["apis"]
                ],
            }

        @app.route("/v1/models", methods=["GET"])
        @app.route("/models", methods=["GET"])
        def local_models():
            return {"object": "list", "data": self._configured_model_list()}

        @app.route("/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
        @app.route("/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
        def proxy(subpath):
            return self.forward(
                subpath=subpath,
                method=request.method,
                headers=dict(request.headers),
                params=request.args.to_dict(flat=False),
                body=request.get_data(),
            )

        from werkzeug.serving import make_server
        host = self.config.get("listen_host", "127.0.0.1")
        port = int(self.config.get("listen_port", 8000))
        self._server = make_server(host, port, app, threaded=True)
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()
        self._running = True
        proxy = self.config.get("outbound_proxy") or {}
        proxy_text = f"，上游代理 {proxy.get('scheme', 'http')}://{proxy.get('host', '127.0.0.1')}:{proxy.get('port', 7897)}" if proxy.get("enabled") else ""
        self.log(f"服务已启动: http://{host}:{port}  (任意路径都会转发到上游{proxy_text})")

    def stop(self):
        if not self._running:
            return
        try:
            self._server.shutdown()
        except Exception:
            pass
        self._running = False
        self.log("服务已停止")
