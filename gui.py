"""
图形化界面 (Tkinter)
- 管理 API 列表（增/删/改/排序/启停）
- 在每个 API 设置页中配置：URL、Key、模型检测、default 模型映射、故障切换
- 设置统一监听地址与端口
- 设置全局故障关键词与可选上游代理
- 启动/停止本地代理服务
- 实时显示日志
"""
import json
import os
import queue
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from manager import APIManager

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class APIManagerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("API 管理器 - 多上游故障自动转移")
        self.root.geometry("1120x780")

        self.manager = APIManager(CONFIG_FILE)
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.manager.log_callback = self.log_queue.put

        self._build_ui()
        self._refresh_api_list()
        self._refresh_settings()
        self.root.after(150, self._drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_apis = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_log = ttk.Frame(nb)
        nb.add(self.tab_apis, text="API 列表")
        nb.add(self.tab_settings, text="服务设置")
        nb.add(self.tab_log, text="运行日志")

        self._build_apis_tab()
        self._build_settings_tab()
        self._build_log_tab()
        self._build_status_bar()

    def _build_apis_tab(self):
        frm = self.tab_apis

        cols = ("index", "name", "url", "enabled", "policy")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", height=16)
        self.tree.heading("index", text="编号")
        self.tree.heading("name", text="名称")
        self.tree.heading("url", text="URL")
        self.tree.heading("enabled", text="启用")
        self.tree.heading("policy", text="故障流程 / 模型")
        self.tree.column("index", width=60, anchor="center")
        self.tree.column("name", width=120)
        self.tree.column("url", width=320)
        self.tree.column("enabled", width=60, anchor="center")
        self.tree.column("policy", width=400)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        self.tree.bind("<Double-1>", lambda e: self._edit_api())

        sb = ttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="left", fill="y", pady=8)

        side = ttk.Frame(frm)
        side.pack(side="left", fill="y", padx=8, pady=8)
        ttk.Button(side, text="新增", command=self._add_api).pack(fill="x", pady=2)
        ttk.Button(side, text="编辑", command=self._edit_api).pack(fill="x", pady=2)
        ttk.Button(side, text="删除", command=self._delete_api).pack(fill="x", pady=2)
        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(side, text="上移", command=lambda: self._move_api(-1)).pack(fill="x", pady=2)
        ttk.Button(side, text="下移", command=lambda: self._move_api(1)).pack(fill="x", pady=2)
        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(side, text="启用/禁用", command=self._toggle_enabled).pack(fill="x", pady=2)

    def _build_settings_tab(self):
        frm = self.tab_settings
        pad = {"padx": 8, "pady": 6}

        base = ttk.LabelFrame(frm, text="本地服务")
        base.grid(row=0, column=0, sticky="nw", padx=8, pady=8)
        ttk.Label(base, text="监听地址:").grid(row=0, column=0, sticky="e", **pad)
        self.var_host = tk.StringVar()
        ttk.Entry(base, textvariable=self.var_host, width=20).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(base, text="监听端口:").grid(row=1, column=0, sticky="e", **pad)
        self.var_port = tk.StringVar()
        ttk.Entry(base, textvariable=self.var_port, width=10).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(base, text="默认超时(秒):").grid(row=2, column=0, sticky="e", **pad)
        self.var_timeout = tk.StringVar()
        ttk.Entry(base, textvariable=self.var_timeout, width=10).grid(row=2, column=1, sticky="w", **pad)

        proxy = ttk.LabelFrame(frm, text="上游代理（可选）")
        proxy.grid(row=1, column=0, sticky="nw", padx=8, pady=8)
        self.var_proxy_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(proxy, text="启用代理", variable=self.var_proxy_enabled).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=6)
        ttk.Label(proxy, text="协议:").grid(row=1, column=0, sticky="e", **pad)
        self.var_proxy_scheme = tk.StringVar(value="http")
        ttk.Combobox(proxy, textvariable=self.var_proxy_scheme, width=10, state="readonly", values=["http", "socks5"]).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(proxy, text="主机:").grid(row=2, column=0, sticky="e", **pad)
        self.var_proxy_host = tk.StringVar(value="127.0.0.1")
        ttk.Entry(proxy, textvariable=self.var_proxy_host, width=18).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(proxy, text="端口:").grid(row=3, column=0, sticky="e", **pad)
        self.var_proxy_port = tk.StringVar(value="7897")
        ttk.Entry(proxy, textvariable=self.var_proxy_port, width=10).grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(proxy, text="默认端口 7897", foreground="#888").grid(row=4, column=1, sticky="w", padx=8)

        keywords = ttk.LabelFrame(frm, text="全局故障关键词（每行一个）")
        keywords.grid(row=0, column=1, rowspan=2, sticky="nw", padx=8, pady=8)
        self.txt_keywords = tk.Text(keywords, width=45, height=12)
        self.txt_keywords.pack(padx=8, pady=8)

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=10)
        ttk.Button(btns, text="保存设置", command=self._save_settings).pack(side="left", padx=4)
        ttk.Button(btns, text="启动服务", command=self._start_service).pack(side="left", padx=4)
        ttk.Button(btns, text="停止服务", command=self._stop_service).pack(side="left", padx=4)

        hint = (
            "用法（OpenAI 兼容）：上游填 https://api.openai.com/v1，客户端把 base_url 改成\n"
            "http://<监听地址>:<端口>/v1 即可。在每个 API 设置页可以先检测支持的模型，再选择一个作为\n"
            "default 的真实映射模型。以后请求 model=default 时，程序会自动改成该上游选择的模型。"
        )
        ttk.Label(frm, text=hint, justify="left", foreground="#444").grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=10)

    def _build_log_tab(self):
        frm = self.tab_log
        self.txt_log = tk.Text(frm, wrap="none", bg="#111", fg="#cfc", insertbackground="#fff")
        self.txt_log.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(frm, text="清空日志", command=lambda: self.txt_log.delete("1.0", "end")).pack(pady=4)

    def _build_status_bar(self):
        self.var_status = tk.StringVar(value="● 服务未启动")
        ttk.Label(self.root, textvariable=self.var_status, anchor="w", relief="sunken").pack(side="bottom", fill="x")

    def _policy_summary(self, api: dict) -> str:
        policy = api.get("policy") or {}
        model_cfg = api.get("model_config") or {}
        triggers = []
        if policy.get("trigger_timeout", True):
            triggers.append(f"超时{policy.get('timeout_seconds', self.manager.config.get('timeout', 30))}s")
        if policy.get("trigger_empty_response", True):
            triggers.append("空响应")
        if policy.get("trigger_keywords", True):
            triggers.append("关键词")
        action = "切下一个"
        if str(policy.get("action", "next")) == "specific":
            target_name = str(policy.get("target_name", "") or "").strip()
            target_index = int(policy.get("target_index", -1) or -1)
            if target_name:
                action = f"切指定[{target_name}]"
            elif target_index >= 0:
                action = f"切指定#{target_index}"
            else:
                action = "切指定(未设)"
        selected_model = str(model_cfg.get("selected_model", "") or "").strip() or "未选模型"
        alias = str(model_cfg.get("default_alias", "default") or "default").strip() or "default"
        return f"{alias}->{selected_model}；当 {'/'.join(triggers) or '异常'} → 重试{policy.get('retry_times', 0)}次 → {action}"

    def _refresh_api_list(self):
        self.tree.delete(*self.tree.get_children())
        for i, api in enumerate(self.manager.config["apis"]):
            self.tree.insert(
                "", "end", iid=str(i),
                values=(
                    i,
                    api.get("name", ""),
                    api.get("url", ""),
                    "是" if api.get("enabled", True) else "否",
                    self._policy_summary(api),
                ),
            )

    def _refresh_settings(self):
        cfg = self.manager.config
        self.var_host.set(cfg.get("listen_host", "127.0.0.1"))
        self.var_port.set(str(cfg.get("listen_port", 8000)))
        self.var_timeout.set(str(cfg.get("timeout", 30)))
        proxy = cfg.get("outbound_proxy") or {}
        self.var_proxy_enabled.set(bool(proxy.get("enabled", False)))
        self.var_proxy_scheme.set(proxy.get("scheme", "http"))
        self.var_proxy_host.set(proxy.get("host", "127.0.0.1"))
        self.var_proxy_port.set(str(proxy.get("port", 7897)))
        self.txt_keywords.delete("1.0", "end")
        self.txt_keywords.insert("1.0", "\n".join(cfg.get("fail_keywords", [])))

    def _selected_index(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _add_api(self):
        self._open_api_dialog()

    def _edit_api(self):
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选择一项")
            return
        self._open_api_dialog(idx)

    def _delete_api(self):
        idx = self._selected_index()
        if idx is None:
            return
        if messagebox.askyesno("确认", "确定删除选中的 API 吗？"):
            self.manager.remove_api(idx)
            self._refresh_api_list()

    def _move_api(self, direction: int):
        idx = self._selected_index()
        if idx is None:
            return
        self.manager.move_api(idx, direction)
        self._refresh_api_list()
        new_idx = max(0, min(len(self.manager.config["apis"]) - 1, idx + direction))
        self.tree.selection_set(str(new_idx))

    def _toggle_enabled(self):
        idx = self._selected_index()
        if idx is None:
            return
        cur = self.manager.config["apis"][idx].get("enabled", True)
        self.manager.update_api(idx, enabled=not cur)
        self._refresh_api_list()
        self.tree.selection_set(str(idx))

    def _open_api_dialog(self, index: int = None):
        dlg = tk.Toplevel(self.root)
        dlg.title("编辑 API" if index is not None else "新增 API")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.geometry("820x860")

        data = self.manager.config["apis"][index] if index is not None else {
            "name": "", "url": "", "headers": {}, "enabled": True, "policy": {}, "model_config": {}
        }
        policy = data.get("policy") or {}
        model_cfg = data.get("model_config") or {}

        existing_headers = dict(data.get("headers") or {})
        existing_key = ""
        existing_scheme = "Bearer"
        for hk in list(existing_headers.keys()):
            if hk.lower() == "authorization":
                val = existing_headers.pop(hk).strip()
                parts = val.split(None, 1)
                if len(parts) == 2 and parts[0].lower() in ("bearer", "basic", "token"):
                    existing_scheme = parts[0]
                    existing_key = parts[1]
                else:
                    existing_key = val
                    existing_scheme = ""
                break

        pad = {"padx": 10, "pady": 6}
        ttk.Label(dlg, text="名称:").grid(row=0, column=0, sticky="e", **pad)
        v_name = tk.StringVar(value=data.get("name", ""))
        ttk.Entry(dlg, textvariable=v_name, width=64).grid(row=0, column=1, columnspan=4, sticky="w", **pad)

        ttk.Label(dlg, text="API URL:").grid(row=1, column=0, sticky="e", **pad)
        v_url = tk.StringVar(value=data.get("url", ""))
        ttk.Entry(dlg, textvariable=v_url, width=64).grid(row=1, column=1, columnspan=4, sticky="w", **pad)
        ttk.Label(dlg, text="例: https://api.openai.com/v1", foreground="#888").grid(row=2, column=1, columnspan=4, sticky="w", padx=10)

        ttk.Label(dlg, text="API Key:").grid(row=3, column=0, sticky="e", **pad)
        v_key = tk.StringVar(value=existing_key)
        ent_key = ttk.Entry(dlg, textvariable=v_key, width=50, show="•")
        ent_key.grid(row=3, column=1, sticky="w", padx=(10, 0), pady=6)
        v_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(dlg, text="显示", variable=v_show, command=lambda: ent_key.config(show="" if v_show.get() else "•")).grid(row=3, column=2, sticky="w")

        ttk.Label(dlg, text="鉴权方式:").grid(row=4, column=0, sticky="e", **pad)
        v_scheme = tk.StringVar(value=existing_scheme or "Bearer")
        ttk.Combobox(dlg, textvariable=v_scheme, width=15, state="readonly", values=["Bearer", "Token", "Basic", "(原样)", "(无)"]).grid(row=4, column=1, sticky="w", padx=(10, 0), pady=6)

        model_frame = ttk.LabelFrame(dlg, text="模型设置")
        model_frame.grid(row=5, column=0, columnspan=5, sticky="we", padx=10, pady=10)
        ttk.Label(model_frame, text="default 别名:").grid(row=0, column=0, sticky="e", padx=8, pady=6)
        v_default_alias = tk.StringVar(value=str(model_cfg.get("default_alias", "default") or "default"))
        ttk.Entry(model_frame, textvariable=v_default_alias, width=18).grid(row=0, column=1, sticky="w", pady=6)
        ttk.Label(model_frame, text="推荐保持 default", foreground="#888").grid(row=0, column=2, sticky="w", padx=6)

        ttk.Label(model_frame, text="检测到的模型:").grid(row=1, column=0, sticky="ne", padx=8, pady=6)
        model_values = list(model_cfg.get("available_models") or [])
        v_selected_model = tk.StringVar(value=str(model_cfg.get("selected_model", "") or ""))
        cmb_models = ttk.Combobox(model_frame, textvariable=v_selected_model, width=48, values=model_values)
        cmb_models.grid(row=1, column=1, columnspan=2, sticky="w", pady=6)

        def detect_models_action():
            nonlocal model_values
            name = v_name.get().strip() or "未命名API"
            url = v_url.get().strip()
            if not url:
                messagebox.showerror("错误", "请先填写 API URL")
                return
            raw = t_headers.get("1.0", "end").strip() or "{}"
            try:
                headers = json.loads(raw)
                if not isinstance(headers, dict):
                    raise ValueError
            except Exception:
                messagebox.showerror("错误", "额外 Headers 必须是合法的 JSON 对象")
                return
            key = v_key.get().strip()
            scheme = v_scheme.get().strip()
            if key:
                if scheme in ("", "(无)"):
                    pass
                elif scheme == "(原样)":
                    headers["Authorization"] = key
                else:
                    headers["Authorization"] = f"{scheme} {key}"
            temp_policy = {
                "timeout_seconds": max(1, int(v_timeout_seconds.get().strip() or "30")),
                "trigger_timeout": v_trigger_timeout.get(),
                "trigger_empty_response": v_trigger_empty.get(),
                "trigger_keywords": v_trigger_keywords.get(),
                "keywords": [line.strip() for line in txt_local_keywords.get("1.0", "end").splitlines() if line.strip()],
                "retry_times": max(0, int(v_retry_times.get().strip() or "0")),
                "action": v_action.get(),
                "target_name": v_target_name.get().strip(),
                "target_index": int(v_target_index.get().strip() or "-1"),
            }
            temp_model_cfg = {
                "available_models": list(model_values),
                "selected_model": v_selected_model.get().strip(),
                "default_alias": v_default_alias.get().strip() or "default",
            }
            temp_api = {
                "name": name,
                "url": url,
                "headers": headers,
                "enabled": v_enabled.get(),
                "policy": temp_policy,
                "model_config": temp_model_cfg,
            }
            try:
                models = self.manager.detect_models_for_api(temp_api)
            except Exception as e:
                messagebox.showerror("检测失败", str(e))
                return
            model_values = models
            cmb_models["values"] = model_values
            if models and not v_selected_model.get().strip():
                v_selected_model.set(models[0])
            messagebox.showinfo("检测完成", f"检测到 {len(models)} 个模型")

        ttk.Button(model_frame, text="检测支持模型", command=detect_models_action).grid(row=1, column=3, sticky="w", padx=8)
        ttk.Label(model_frame, text="选中的真实模型:").grid(row=2, column=0, sticky="e", padx=8, pady=6)
        ttk.Entry(model_frame, textvariable=v_selected_model, width=50).grid(row=2, column=1, columnspan=2, sticky="w", pady=6)
        ttk.Label(model_frame, text="请求 model=default 时会改写成这里的模型", foreground="#888").grid(row=3, column=1, columnspan=3, sticky="w", padx=2)

        flow = ttk.LabelFrame(dlg, text="故障流程（接近流程图）")
        flow.grid(row=6, column=0, columnspan=5, sticky="we", padx=10, pady=10)
        ttk.Label(flow, text="当 A 发生以下情况时：").grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=6)

        v_trigger_timeout = tk.BooleanVar(value=policy.get("trigger_timeout", True))
        v_timeout_seconds = tk.StringVar(value=str(policy.get("timeout_seconds", self.manager.config.get("timeout", 30))))
        ttk.Checkbutton(flow, text="超时", variable=v_trigger_timeout).grid(row=1, column=0, sticky="w", padx=8)
        ttk.Entry(flow, textvariable=v_timeout_seconds, width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(flow, text="秒（默认30）").grid(row=1, column=2, sticky="w")

        v_trigger_empty = tk.BooleanVar(value=policy.get("trigger_empty_response", True))
        ttk.Checkbutton(flow, text="返回内容为空", variable=v_trigger_empty).grid(row=2, column=0, columnspan=3, sticky="w", padx=8)

        v_trigger_keywords = tk.BooleanVar(value=policy.get("trigger_keywords", True))
        ttk.Checkbutton(flow, text="返回内容包含关键词", variable=v_trigger_keywords).grid(row=3, column=0, columnspan=3, sticky="w", padx=8)
        ttk.Label(flow, text="关键词（留空则用全局）:").grid(row=4, column=0, sticky="ne", padx=8, pady=6)
        txt_local_keywords = tk.Text(flow, width=48, height=4)
        txt_local_keywords.grid(row=4, column=1, columnspan=3, sticky="w", pady=6)
        txt_local_keywords.insert("1.0", "\n".join(policy.get("keywords", [])))

        ttk.Separator(flow, orient="horizontal").grid(row=5, column=0, columnspan=4, sticky="we", padx=8, pady=8)
        ttk.Label(flow, text="下一步：先重试").grid(row=6, column=0, sticky="w", padx=8)
        v_retry_times = tk.StringVar(value=str(policy.get("retry_times", 0)))
        ttk.Entry(flow, textvariable=v_retry_times, width=8).grid(row=6, column=1, sticky="w")
        ttk.Label(flow, text="次；若仍失败则").grid(row=6, column=2, sticky="w")

        v_action = tk.StringVar(value=policy.get("action", "next"))
        action_frame = ttk.Frame(flow)
        action_frame.grid(row=7, column=0, columnspan=4, sticky="w", padx=8, pady=4)
        ttk.Radiobutton(action_frame, text="切换下一个", variable=v_action, value="next").pack(side="left", padx=4)
        ttk.Radiobutton(action_frame, text="切换指定", variable=v_action, value="specific").pack(side="left", padx=4)

        target_frame = ttk.Frame(flow)
        target_frame.grid(row=8, column=0, columnspan=4, sticky="w", padx=8, pady=4)
        ttk.Label(target_frame, text="指定名称:").pack(side="left")
        api_names = [a.get("name", "") for i, a in enumerate(self.manager.config["apis"]) if i != index]
        v_target_name = tk.StringVar(value=policy.get("target_name", ""))
        ttk.Combobox(target_frame, textvariable=v_target_name, width=20, values=api_names).pack(side="left", padx=4)
        ttk.Label(target_frame, text="或指定序号:").pack(side="left", padx=(12, 0))
        v_target_index = tk.StringVar(value=str(policy.get("target_index", -1)))
        ttk.Entry(target_frame, textvariable=v_target_index, width=8).pack(side="left", padx=4)
        ttk.Label(target_frame, text="（从 0 开始）", foreground="#888").pack(side="left")

        adv_frame = ttk.LabelFrame(dlg, text="高级：额外 Headers (JSON, 可选)")
        adv_frame.grid(row=7, column=0, columnspan=5, sticky="we", padx=10, pady=8)
        t_headers = tk.Text(adv_frame, width=80, height=7)
        t_headers.pack(fill="both", expand=True, padx=4, pady=4)
        t_headers.insert("1.0", json.dumps(existing_headers, ensure_ascii=False, indent=2) if existing_headers else "{}")

        v_enabled = tk.BooleanVar(value=data.get("enabled", True))
        ttk.Checkbutton(dlg, text="启用此 API", variable=v_enabled).grid(row=8, column=1, sticky="w", padx=10, pady=4)

        def save():
            name = v_name.get().strip()
            url = v_url.get().strip()
            if not name or not url:
                messagebox.showerror("错误", "名称和URL不能为空")
                return
            try:
                timeout_seconds = int(v_timeout_seconds.get().strip() or "30")
                retry_times = int(v_retry_times.get().strip() or "0")
                target_index = int(v_target_index.get().strip() or "-1")
            except ValueError:
                messagebox.showerror("错误", "超时、重试次数、目标序号必须是整数")
                return
            raw = t_headers.get("1.0", "end").strip() or "{}"
            try:
                headers = json.loads(raw)
                if not isinstance(headers, dict):
                    raise ValueError
            except Exception:
                messagebox.showerror("错误", "额外 Headers 必须是合法的 JSON 对象")
                return

            key = v_key.get().strip()
            scheme = v_scheme.get().strip()
            if key:
                if scheme in ("", "(无)"):
                    pass
                elif scheme == "(原样)":
                    headers["Authorization"] = key
                else:
                    headers["Authorization"] = f"{scheme} {key}"

            local_keywords = [line.strip() for line in txt_local_keywords.get("1.0", "end").splitlines() if line.strip()]
            policy_data = {
                "timeout_seconds": max(1, timeout_seconds),
                "trigger_timeout": v_trigger_timeout.get(),
                "trigger_empty_response": v_trigger_empty.get(),
                "trigger_keywords": v_trigger_keywords.get(),
                "keywords": local_keywords,
                "retry_times": max(0, retry_times),
                "action": v_action.get(),
                "target_name": v_target_name.get().strip(),
                "target_index": target_index,
            }
            model_data = {
                "available_models": list(model_values),
                "selected_model": v_selected_model.get().strip(),
                "default_alias": v_default_alias.get().strip() or "default",
            }

            if index is None:
                self.manager.add_api(name, url, headers, v_enabled.get(), policy_data, model_data)
            else:
                self.manager.update_api(index, name=name, url=url, headers=headers,
                                        enabled=v_enabled.get(), policy=policy_data, model_config=model_data)
            self._refresh_api_list()
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=9, column=0, columnspan=5, sticky="e", padx=10, pady=10)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="保存", command=save).pack(side="right", padx=4)

    def _save_settings(self):
        try:
            self.manager.set_listen(
                self.var_host.get().strip() or "127.0.0.1",
                int(self.var_port.get().strip() or "8000"),
                int(self.var_timeout.get().strip() or "30"),
            )
            self.manager.set_proxy(
                self.var_proxy_enabled.get(),
                self.var_proxy_scheme.get().strip() or "http",
                self.var_proxy_host.get().strip() or "127.0.0.1",
                int(self.var_proxy_port.get().strip() or "7897"),
            )
        except ValueError:
            messagebox.showerror("错误", "端口和超时必须是整数")
            return
        kws = [line.strip() for line in self.txt_keywords.get("1.0", "end").splitlines() if line.strip()]
        self.manager.set_keywords(kws)
        messagebox.showinfo("提示", "设置已保存")

    def _start_service(self):
        self._save_settings_silent()
        try:
            self.manager.start()
            proxy = self.manager.config.get("outbound_proxy") or {}
            proxy_text = f"，上游代理 {proxy.get('scheme', 'http')}://{proxy.get('host', '127.0.0.1')}:{proxy.get('port', 7897)}" if proxy.get("enabled") else ""
            self.var_status.set(
                f"● 服务运行中 http://{self.manager.config['listen_host']}:"
                f"{self.manager.config['listen_port']}  (OpenAI 兼容入口{proxy_text})"
            )
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def _stop_service(self):
        self.manager.stop()
        self.var_status.set("● 服务未启动")

    def _save_settings_silent(self):
        try:
            self.manager.set_listen(
                self.var_host.get().strip() or "127.0.0.1",
                int(self.var_port.get().strip() or "8000"),
                int(self.var_timeout.get().strip() or "30"),
            )
            self.manager.set_proxy(
                self.var_proxy_enabled.get(),
                self.var_proxy_scheme.get().strip() or "http",
                self.var_proxy_host.get().strip() or "127.0.0.1",
                int(self.var_proxy_port.get().strip() or "7897"),
            )
            kws = [line.strip() for line in self.txt_keywords.get("1.0", "end").splitlines() if line.strip()]
            self.manager.set_keywords(kws)
        except Exception:
            pass

    def _drain_log_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.txt_log.insert("end", line + "\n")
                self.txt_log.see("end")
        except queue.Empty:
            pass
        self.root.after(150, self._drain_log_queue)

    def _on_close(self):
        try:
            self.manager.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if sys.platform == "win32":
            style.theme_use("vista")
    except Exception:
        pass
    APIManagerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
