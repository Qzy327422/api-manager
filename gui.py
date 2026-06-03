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
import sqlite3
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import requests

from manager import APIManager

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_history.db")


class APIManagerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("API 管理器 - 多上游故障自动转移")
        self.root.geometry("1120x780")

        self.manager = APIManager(CONFIG_FILE)
        self.db = sqlite3.connect(DB_FILE)
        self._init_chat_db()
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.manager.log_callback = self.log_queue.put

        self._build_ui()
        self._refresh_api_list()
        self._refresh_settings()
        self.root.after(150, self._drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.manager.config.get("auto_start_service", False):
            self.root.after(300, self._start_service)

    def _init_chat_db(self):
        cur = self.db.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        self.db.commit()

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_apis = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_test = ttk.Frame(nb)
        self.tab_log = ttk.Frame(nb)
        nb.add(self.tab_apis, text="API 列表")
        nb.add(self.tab_settings, text="服务设置")
        nb.add(self.tab_test, text="测试对话")
        nb.add(self.tab_log, text="运行日志")

        self._build_apis_tab()
        self._build_settings_tab()
        self._build_test_tab()
        self._build_log_tab()
        self._build_status_bar()

    def _build_apis_tab(self):
        frm = self.tab_apis

        cols = ("index", "name", "url", "enabled", "policy")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", height=16)
        col_widths = (self.manager.config.get("ui") or {}).get("api_list_columns", {})
        self.tree.heading("index", text="编号")
        self.tree.heading("name", text="名称")
        self.tree.heading("url", text="URL")
        self.tree.heading("enabled", text="启用")
        self.tree.heading("policy", text="故障流程 / 模型")
        self.tree.column("index", width=int(col_widths.get("index", 60)), anchor="center")
        self.tree.column("name", width=int(col_widths.get("name", 120)))
        self.tree.column("url", width=int(col_widths.get("url", 320)))
        self.tree.column("enabled", width=int(col_widths.get("enabled", 60)), anchor="center")
        self.tree.column("policy", width=int(col_widths.get("policy", 400)))
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        self.tree.bind("<Double-1>", lambda e: self._edit_api())
        self.tree.bind("<ButtonRelease-1>", lambda e: self._save_api_column_widths())

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
        ttk.Label(base, text="默认连接超时(秒):").grid(row=2, column=0, sticky="e", **pad)
        self.var_connect_timeout = tk.StringVar()
        ttk.Entry(base, textvariable=self.var_connect_timeout, width=10).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(base, text="默认读取/生成超时(秒):").grid(row=3, column=0, sticky="e", **pad)
        self.var_read_timeout = tk.StringVar()
        ttk.Entry(base, textvariable=self.var_read_timeout, width=10).grid(row=3, column=1, sticky="w", **pad)

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

    def _build_test_tab(self):
        frm = self.tab_test
        self.test_sessions = []
        self.current_test_session = None

        outer = ttk.PanedWindow(frm, orient="horizontal")
        outer.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(outer, width=230)
        right = ttk.Frame(outer)
        outer.add(left, weight=0)
        outer.add(right, weight=1)

        ttk.Button(left, text="新建对话", command=self._new_test_session).pack(fill="x", padx=4, pady=(0, 6))
        ttk.Button(left, text="删除对话", command=self._delete_current_test_session).pack(fill="x", padx=4, pady=(0, 6))
        ttk.Label(left, text="对话历史").pack(anchor="w", padx=4)
        self.lst_sessions = tk.Listbox(left, width=28)
        self.lst_sessions.pack(fill="both", expand=True, padx=4, pady=4)
        self.lst_sessions.bind("<<ListboxSelect>>", self._on_select_test_session)
        self.lst_sessions.bind("<Delete>", lambda e: self._delete_current_test_session())

        top = ttk.Frame(right)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="模型:").pack(side="left")
        self.var_test_model = tk.StringVar(value="default")
        models = ["default"]
        for api in self.manager.config.get("apis", []):
            selected = ((api.get("model_config") or {}).get("selected_model") or "").strip()
            if selected and selected not in models:
                models.append(selected)
        ttk.Combobox(top, textvariable=self.var_test_model, width=32, values=models).pack(side="left", padx=6)
        ttk.Label(top, text="接口:").pack(side="left", padx=(12, 0))
        self.var_test_path = tk.StringVar(value="/v1/chat/completions")
        ttk.Entry(top, textvariable=self.var_test_path, width=28).pack(side="left", padx=6)

        self.txt_chat = tk.Text(right, wrap="word", height=24)
        self.txt_chat.pack(fill="both", expand=True, pady=(0, 8))

        bottom = ttk.Frame(right)
        bottom.pack(fill="x")
        self.txt_user_msg = tk.Text(bottom, height=4, wrap="word")
        self.txt_user_msg.pack(side="left", fill="x", expand=True)
        self.txt_user_msg.bind("<Return>", self._on_test_enter)
        self.txt_user_msg.bind("<Shift-Return>", self._on_test_shift_enter)
        ttk.Button(bottom, text="发送消息", command=self._send_test_message).pack(side="left", padx=8)

        self._load_test_sessions()
        if not self.test_sessions:
            self._new_test_session()
        else:
            self.current_test_session = 0
            self.lst_sessions.selection_set(0)
            self._render_current_test_session()

    def _load_test_sessions(self):
        self.test_sessions = []
        self.lst_sessions.delete(0, "end")
        cur = self.db.cursor()
        rows = cur.execute("SELECT id, title FROM sessions ORDER BY updated_at DESC, id DESC").fetchall()
        for sid, title in rows:
            msgs = cur.execute(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY id ASC", (sid,)
            ).fetchall()
            session = {"id": sid, "title": title, "messages": [{"role": r, "content": c} for r, c in msgs]}
            self.test_sessions.append(session)
            self.lst_sessions.insert("end", title)

    def _new_test_session(self):
        title = f"新对话 {len(self.test_sessions) + 1}"
        now = int(time.time())
        cur = self.db.cursor()
        cur.execute("INSERT INTO sessions(title, created_at, updated_at) VALUES (?, ?, ?)", (title, now, now))
        self.db.commit()
        session = {"id": cur.lastrowid, "title": title, "messages": []}
        self.test_sessions.insert(0, session)
        self.lst_sessions.insert(0, title)
        self.current_test_session = 0
        self.lst_sessions.selection_clear(0, "end")
        self.lst_sessions.selection_set(0)
        self._render_current_test_session()

    def _on_select_test_session(self, event=None):
        sel = self.lst_sessions.curselection()
        if not sel:
            return
        self.current_test_session = int(sel[0])
        self._render_current_test_session()

    def _delete_current_test_session(self):
        if self.current_test_session is None or not self.test_sessions:
            return "break"
        idx = self.current_test_session
        title = self.test_sessions[idx].get("title", "当前对话")
        if not messagebox.askyesno("确认删除", f"确定删除对话“{title}”吗？"):
            return "break"
        sid = self.test_sessions[idx].get("id")
        if sid:
            self.db.execute("DELETE FROM messages WHERE session_id=?", (sid,))
            self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self.db.commit()
        self.test_sessions.pop(idx)
        self.lst_sessions.delete(idx)
        if not self.test_sessions:
            self.current_test_session = None
            self.txt_chat.delete("1.0", "end")
        else:
            self.current_test_session = min(idx, len(self.test_sessions) - 1)
            self.lst_sessions.selection_set(self.current_test_session)
            self._render_current_test_session()
        return "break"

    def _clear_current_test_session(self):
        if self.current_test_session is None:
            return
        sess = self.test_sessions[self.current_test_session]
        sess["messages"] = []
        sid = sess.get("id")
        if sid:
            self.db.execute("DELETE FROM messages WHERE session_id=?", (sid,))
            self.db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (int(time.time()), sid))
            self.db.commit()
        self._render_current_test_session()

    def _render_current_test_session(self):
        self.txt_chat.delete("1.0", "end")
        if self.current_test_session is None:
            return
        for msg in self.test_sessions[self.current_test_session]["messages"]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            name = "你" if role == "user" else "助手" if role == "assistant" else "错误" if role == "error" else "系统"
            self.txt_chat.insert("end", f"{name}: {content}\n\n")
        self.txt_chat.see("end")

    def _on_test_enter(self, event):
        self._send_test_message()
        return "break"

    def _on_test_shift_enter(self, event):
        self.txt_user_msg.insert("insert", "\n")
        return "break"

    def _send_test_message(self):
        msg = self.txt_user_msg.get("1.0", "end").strip()
        if not msg:
            messagebox.showinfo("提示", "请输入测试消息")
            return
        model = self.var_test_model.get().strip() or "default"
        path = self.var_test_path.get().strip() or "/v1/chat/completions"
        if not path.startswith("/"):
            path = "/" + path
        host = self.manager.config.get("listen_host", "127.0.0.1")
        port = int(self.manager.config.get("listen_port", 8000))
        url = f"http://{host}:{port}{path}"
        self.txt_chat.insert("end", f"\n你 ({model}): {msg}\n")
        session_idx = self.current_test_session
        session_id = None
        if self.current_test_session is not None:
            sess = self.test_sessions[self.current_test_session]
            session_id = sess.get("id")
            if sess["title"].startswith("新对话"):
                sess["title"] = msg[:18] + ("..." if len(msg) > 18 else "")
                self.lst_sessions.delete(self.current_test_session)
                self.lst_sessions.insert(self.current_test_session, sess["title"])
                self.lst_sessions.selection_set(self.current_test_session)
            sess["messages"].append({"role": "user", "content": msg})
            now = int(time.time())
            if session_id:
                self.db.execute("INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)", (session_id, "user", msg, now))
                self.db.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?", (sess["title"], now, session_id))
                self.db.commit()
        self.txt_chat.see("end")
        self.txt_user_msg.delete("1.0", "end")

        def worker():
            try:
                payload = {"model": model, "messages": [{"role": "user", "content": msg}], "stream": False}
                resp = requests.post(url, json=payload, timeout=300)
                text = resp.text
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("choices"):
                        choice = data["choices"][0]
                        if isinstance(choice, dict):
                            message = choice.get("message") or {}
                            if isinstance(message, dict) and message.get("content") is not None:
                                text = message.get("content") or ""
                            elif choice.get("text") is not None:
                                text = choice.get("text") or ""
                except Exception:
                    pass
                self.log_queue.put(f"[测试页] HTTP {resp.status_code}")
                status_code = resp.status_code
                reply_text = text
                self.root.after(0, lambda s=status_code, t=reply_text, sid=session_id: self._append_test_reply(s, t, sid))
            except Exception as e:
                err_text = f"请求失败: {e}"
                self.root.after(0, lambda t=err_text, sid=session_id: self._append_test_reply(0, t, sid))

        threading.Thread(target=worker, daemon=True).start()

    def _append_test_reply(self, status: int, text: str, session_id=None):
        prefix = "助手" if status and status < 400 else f"错误({status})"
        target_idx = self.current_test_session
        if session_id is not None:
            for i, sess in enumerate(self.test_sessions):
                if sess.get("id") == session_id:
                    target_idx = i
                    break
        if target_idx is None or not (0 <= target_idx < len(self.test_sessions)):
            return
        role = "assistant" if status and status < 400 else "error"
        sess = self.test_sessions[target_idx]
        sess["messages"].append({"role": role, "content": text})
        sid = sess.get("id")
        if sid:
            now = int(time.time())
            self.db.execute("INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)", (sid, role, text, now))
            self.db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sid))
            self.db.commit()
        if target_idx == self.current_test_session:
            self.txt_chat.insert("end", f"{prefix}: {text}\n")
            self.txt_chat.see("end")

    def _build_log_tab(self):
        frm = self.tab_log
        self.txt_log = tk.Text(frm, wrap="none", bg="#111", fg="#cfc", insertbackground="#fff")
        self.txt_log.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(frm, text="清空日志", command=lambda: self.txt_log.delete("1.0", "end")).pack(pady=4)

    def _build_status_bar(self):
        self.var_status = tk.StringVar(value="● 服务未启动")
        ttk.Label(self.root, textvariable=self.var_status, anchor="w", relief="sunken").pack(side="bottom", fill="x")

    def _save_api_column_widths(self):
        try:
            cols = ("index", "name", "url", "enabled", "policy")
            widths = {col: int(self.tree.column(col, "width")) for col in cols}
            ui = dict(self.manager.config.get("ui") or {})
            ui["api_list_columns"] = widths
            self.manager.config["ui"] = ui
            self.manager.save()
        except Exception:
            pass

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
        allowed_count = len(model_cfg.get("allowed_models") or [])
        extra = f"，允许{allowed_count}个" if allowed_count else ""
        return f"{alias}->{selected_model}{extra}；当 {'/'.join(triggers) or '异常'} → 重试{policy.get('retry_times', 0)}次 → {action}"

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
        self.var_connect_timeout.set(str(cfg.get("connect_timeout", 10)))
        self.var_read_timeout.set(str(cfg.get("read_timeout", cfg.get("timeout", 300))))
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
        allowed_models = list(model_cfg.get("allowed_models") or [])
        if not allowed_models and str(model_cfg.get("selected_model", "") or "").strip():
            allowed_models = [str(model_cfg.get("selected_model", "") or "").strip()]
        v_selected_model = tk.StringVar(value=str(model_cfg.get("selected_model", "") or ""))
        cmb_models = ttk.Combobox(model_frame, textvariable=v_selected_model, width=48, values=model_values)
        cmb_models.grid(row=1, column=1, columnspan=2, sticky="w", pady=6)

        def open_model_selector(models):
            nonlocal allowed_models
            if not models:
                messagebox.showinfo("提示", "没有可选择的模型")
                return
            win = tk.Toplevel(dlg)
            win.title("选择模型")
            win.transient(dlg)
            win.grab_set()
            win.geometry("720x520")

            header = ttk.Frame(win)
            header.pack(fill="x", padx=10, pady=8)
            ttk.Label(header, text="搜索模型:").pack(side="left")
            search_var = tk.StringVar()
            ttk.Entry(header, textvariable=search_var, width=42).pack(side="left", padx=8)
            count_var = tk.StringVar()
            ttk.Label(header, textvariable=count_var).pack(side="right")

            body = ttk.Frame(win)
            body.pack(fill="both", expand=True, padx=10, pady=4)
            canvas = tk.Canvas(body, highlightthickness=0)
            scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
            list_frame = ttk.Frame(canvas)
            list_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=list_frame, anchor="nw")
            canvas.configure(yscrollcommand=scroll.set)
            canvas.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")

            selected_set = set(allowed_models)
            if not selected_set and v_selected_model.get().strip():
                selected_set.add(v_selected_model.get().strip())
            vars_by_model = {}
            default_var = tk.StringVar(value=v_selected_model.get().strip())

            def render():
                for child in list_frame.winfo_children():
                    child.destroy()
                keyword = search_var.get().strip().lower()
                shown = [m for m in models if keyword in m.lower()]
                for m in shown:
                    row = ttk.Frame(list_frame)
                    row.pack(fill="x", pady=2)
                    var = tk.BooleanVar(value=m in selected_set)
                    vars_by_model[m] = var
                    ttk.Checkbutton(row, variable=var).pack(side="left")
                    ttk.Label(row, text=m, width=60).pack(side="left", anchor="w")
                    ttk.Radiobutton(row, text="默认", variable=default_var, value=m).pack(side="right")
                count_var.set(f"已显示 {len(shown)} / 总 {len(models)}")

            def select_all_visible():
                for m, var in vars_by_model.items():
                    var.set(True)

            def clear_all_visible():
                for m, var in vars_by_model.items():
                    var.set(False)

            def apply():
                new_allowed = []
                keyword = search_var.get().strip().lower()
                visible = {m for m in models if keyword in m.lower()}
                for m in models:
                    if m in vars_by_model:
                        if vars_by_model[m].get():
                            new_allowed.append(m)
                    elif m in selected_set and m not in visible:
                        new_allowed.append(m)
                default_model = default_var.get().strip()
                if default_model and default_model not in new_allowed:
                    new_allowed.append(default_model)
                if not new_allowed:
                    messagebox.showerror("错误", "至少选择一个允许模型")
                    return
                allowed_models = new_allowed
                v_selected_model.set(default_model or new_allowed[0])
                cmb_models["values"] = allowed_models
                win.destroy()

            search_var.trace_add("write", lambda *_: render())
            tools = ttk.Frame(win)
            tools.pack(fill="x", padx=10, pady=6)
            ttk.Button(tools, text="全选当前", command=select_all_visible).pack(side="left", padx=4)
            ttk.Button(tools, text="清空当前", command=clear_all_visible).pack(side="left", padx=4)
            ttk.Button(tools, text="取消", command=win.destroy).pack(side="right", padx=4)
            ttk.Button(tools, text="确定", command=apply).pack(side="right", padx=4)
            render()

        def detect_models_action():
            nonlocal model_values, allowed_models
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
                "connect_timeout_seconds": max(1, int(v_connect_timeout.get().strip() or "10")),
                "read_timeout_seconds": max(1, int(v_read_timeout.get().strip() or "300")),
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
                "allowed_models": list(allowed_models),
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
            if not allowed_models:
                allowed_models = list(models)
            cmb_models["values"] = allowed_models
            if models and not v_selected_model.get().strip():
                v_selected_model.set(models[0])
            open_model_selector(models)

        ttk.Button(model_frame, text="检测/选择模型", command=detect_models_action).grid(row=1, column=3, sticky="w", padx=8)
        ttk.Button(model_frame, text="重新选择", command=lambda: open_model_selector(model_values)).grid(row=2, column=3, sticky="w", padx=8)
        ttk.Label(model_frame, text="默认真实模型:").grid(row=2, column=0, sticky="e", padx=8, pady=6)
        ttk.Entry(model_frame, textvariable=v_selected_model, width=50).grid(row=2, column=1, columnspan=2, sticky="w", pady=6)
        ttk.Label(model_frame, text="请求 model=default 时会改写成这里的模型", foreground="#888").grid(row=3, column=1, columnspan=3, sticky="w", padx=2)

        flow = ttk.LabelFrame(dlg, text="故障流程（接近流程图）")
        flow.grid(row=6, column=0, columnspan=5, sticky="we", padx=10, pady=10)
        ttk.Label(flow, text="当 A 发生以下情况时：").grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=6)

        v_trigger_timeout = tk.BooleanVar(value=policy.get("trigger_timeout", True))
        v_timeout_seconds = tk.StringVar(value=str(policy.get("timeout_seconds", self.manager.config.get("timeout", 30))))
        v_connect_timeout = tk.StringVar(value=str(policy.get("connect_timeout_seconds", 10)))
        v_read_timeout = tk.StringVar(value=str(policy.get("read_timeout_seconds", max(int(policy.get("timeout_seconds", self.manager.config.get("timeout", 30)) or 30), 300))))
        ttk.Checkbutton(flow, text="启用超时故障判断", variable=v_trigger_timeout).grid(row=1, column=0, sticky="w", padx=8)
        ttk.Label(flow, text="连接超时").grid(row=1, column=1, sticky="e")
        ttk.Entry(flow, textvariable=v_connect_timeout, width=8).grid(row=1, column=2, sticky="w")
        ttk.Label(flow, text="秒；读取/生成超时").grid(row=1, column=3, sticky="w")
        ttk.Entry(flow, textvariable=v_read_timeout, width=8).grid(row=1, column=4, sticky="w")
        ttk.Label(flow, text="秒（长输出建议300+）").grid(row=1, column=5, sticky="w")

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
                connect_timeout = int(v_connect_timeout.get().strip() or "10")
                read_timeout = int(v_read_timeout.get().strip() or "300")
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
                "connect_timeout_seconds": max(1, connect_timeout),
                "read_timeout_seconds": max(1, read_timeout),
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
                "allowed_models": list(allowed_models),
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
                int(self.var_connect_timeout.get().strip() or "10"),
                int(self.var_read_timeout.get().strip() or "300"),
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
            self.manager.config["auto_start_service"] = True
            self.manager.save()
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
        self.manager.config["auto_start_service"] = False
        self.manager.save()
        self.var_status.set("● 服务未启动")

    def _save_settings_silent(self):
        try:
            self.manager.set_listen(
                self.var_host.get().strip() or "127.0.0.1",
                int(self.var_port.get().strip() or "8000"),
                int(self.var_connect_timeout.get().strip() or "10"),
                int(self.var_read_timeout.get().strip() or "300"),
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
        was_running = self.manager.is_running()
        try:
            self.manager.config["auto_start_service"] = was_running
            self.manager.save()
        except Exception:
            pass
        try:
            self.manager.stop()
        except Exception:
            pass
        try:
            self.db.close()
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
