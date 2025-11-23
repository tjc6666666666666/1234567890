from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
import aiohttp
import os
import platform
import threading
from pathlib import Path
from urllib.parse import quote, unquote
from typing import Dict, Optional

# 服务器依赖（需提前安装：pip install flask waitress pyftpdlib wsgidav cheroot）
try:
    from flask import Flask, render_template_string, request, redirect, url_for
    from waitress import serve
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import ThreadedFTPServer
    from wsgidav.wsgidav_app import WsgiDAVApp
    from wsgidav.fs_dav_provider import FilesystemProvider
    DEPENDENCIES_INSTALLED = True
except ImportError:
    DEPENDENCIES_INSTALLED = False

@register("file_server", "本地文件服务器", "自动启动多协议文件服务器（HTTP/FTP/WebDAV），支持自定义目录浏览。\n使用 /img 获取随机图片。", "1.0")
class FileServerPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.api_url = config.get("api_url", "")  # 保留原有配置项
        
        # 服务器配置（从插件配置读取）
        self.http_port = config.get("http_port", 8080)
        self.ftp_port = config.get("ftp_port", 2121)
        self.webdav_port = config.get("webdav_port", 8081)
        self.default_root = config.get("default_root", None)
        
        # 服务器状态管理
        self.server_threads: Dict[str, threading.Thread] = {}
        self.server_instances: Dict[str, any] = {}
        
        # ========== 插件加载时自动启动服务器 ==========
        if DEPENDENCIES_INSTALLED:
            self.start_servers()
            print(f"\n✅ 文件服务器已自动启动！\n🌐 HTTP网页：http://localhost:{self.http_port}\n📁 FTP服务：ftp://localhost:{self.ftp_port}（匿名登录）\n🔗 WebDAV：http://localhost:{self.webdav_port}")
        else:
            print("\n❌ 文件服务器依赖未安装，请执行：\npip install flask waitress pyftpdlib wsgidav cheroot\nWindows需额外安装：pip install pywin32")

    # ========== 原有图片功能（保留） ==========
    @filter.command("img")
    async def get_setu(self, event: AstrMessageEvent):
        if not DEPENDENCIES_INSTALLED:
            yield event.plain_result("\n请先安装依赖：pip install aiohttp flask waitress pyftpdlib wsgidav cheroot pywin32(Windows)")
            return
            
        if not self.api_url:
            yield event.plain_result("\n请先在配置文件中设置API地址")
            return
            
        ssl_context = aiohttp.TCPConnector(verify_ssl=False)
        async with aiohttp.ClientSession(connector=ssl_context) as session:
            try:
                async with session.get(self.api_url) as response:
                    content_type = response.headers.get('Content-Type', '')
                    
                    if 'application/json' in content_type:
                        data = await response.json()
                        if data.get("error"):
                            yield event.plain_result(f"\n获取图片失败：{data['error']}")
                            return
                        if not data.get("data"):
                            yield event.plain_result("\n未获取到图片")
                            return
                        image_data = data["data"][0]
                        image_url = image_data["urls"]["original"]
                    elif 'image' in content_type:
                        image_url = str(response.url)
                    else:
                        yield event.plain_result(f"\n不支持的响应类型: {content_type}")
                        return
                    
                    chain = [Image.fromURL(image_url)]
                    yield event.chain_result(chain)
                    
            except Exception as e:
                yield event.plain_result(f"\n请求失败: {str(e)}")

    # ========== 服务器核心功能 ==========
    def get_system_roots(self):
        """适配系统根目录（Windows驱动器/Linux根）"""
        if platform.system() == "Windows":
            try:
                import win32api
                drives = win32api.GetLogicalDriveStrings().split('\000')[:-1]
                return {drive[:2]: drive for drive in drives}
            except ImportError:
                return {"C:\\": "C:\\"}
        else:
            return {"/": "/"}

    def create_flask_app(self):
        """创建Flask网页应用（文件浏览器）"""
        app = Flask(__name__)
        app.secret_key = "astrbot_file_server"

        # 网页模板
        HTML_TEMPLATE = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>AstrBot文件浏览器 - {{ current_path }}</title>
            <style>
                body { font-family: Arial; margin: 20px; }
                .nav { margin: 10px 0; padding: 10px; background: #f0f0f0; }
                .file-list { list-style: none; padding: 0; }
                .file-list li { padding: 5px; border-bottom: 1px solid #eee; }
                .dir { color: #0066cc; font-weight: bold; }
                .file { color: #333; }
                .custom-dir { margin: 20px 0; }
            </style>
        </head>
        <body>
            <h1>AstrBot 本地文件服务器</h1>
            
            <div class="custom-dir">
                <form method="POST">
                    <input type="text" name="custom_dir" placeholder="输入目录路径（如C:\\或/home）" value="{{ current_path }}" style="width: 400px;">
                    <button type="submit">跳转</button>
                </form>
            </div>

            <div class="nav">
                系统根目录：
                {% for name, path in system_roots.items() %}
                    <a href="{{ url_for('browse', path=quote(path)) }}">{{ name }}</a> |
                {% endfor %}
            </div>

            <div class="nav">
                当前路径：{{ current_path }}
            </div>

            <ul class="file-list">
                {% if parent_path %}
                    <li class="dir"><a href="{{ url_for('browse', path=quote(parent_path)) }}">../ (上级目录)</a></li>
                {% endif %}
                {% for item in items %}
                    <li class="{{ 'dir' if item.is_dir else 'file' }}">
                        {% if item.is_dir %}
                            <a href="{{ url_for('browse', path=quote(item.path)) }}">{{ item.name }}/</a>
                        {% else %}
                            {{ item.name }} ({{ item.size }} bytes)
                        {% endif %}
                    </li>
                {% endfor %}
            </ul>
        </body>
        </html>
        """

        @app.route("/", methods=["GET", "POST"])
        @app.route("/browse/<path:path>", methods=["GET", "POST"])
        def browse(path=None):
            if request.method == "POST":
                custom_dir = request.form.get("custom_dir", "").strip()
                if os.path.isdir(custom_dir):
                    return redirect(url_for("browse", path=quote(custom_dir)))
            
            if path is None:
                current_path = next(iter(self.get_system_roots().values()))
            else:
                current_path = unquote(path)
                if not os.path.isabs(current_path) or not os.path.exists(current_path):
                    current_path = next(iter(self.get_system_roots().values()))
            
            parent_path = os.path.dirname(current_path) if current_path != os.path.splitdrive(current_path)[0] + os.sep else None
            
            try:
                items = []
                for entry in os.scandir(current_path):
                    items.append({
                        "name": entry.name,
                        "path": entry.path,
                        "is_dir": entry.is_dir(),
                        "size": entry.stat().st_size if entry.is_file() else "-"
                    })
                items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            except PermissionError:
                items = [{"name": "权限不足，无法访问", "is_dir": False}]
            
            return render_template_string(
                HTML_TEMPLATE,
                current_path=current_path,
                parent_path=parent_path,
                items=items,
                system_roots=self.get_system_roots(),
                quote=quote
            )

        return app

    def run_http_server(self):
        """启动多线程HTTP服务"""
        app = self.create_flask_app()
        serve(app, host="0.0.0.0", port=self.http_port, threads=10)

    def run_ftp_server(self):
        """启动多线程FTP服务"""
        authorizer = DummyAuthorizer()
        root_dir = self.default_root or next(iter(self.get_system_roots().values()))
        authorizer.add_anonymous(root_dir, perm="elradfmw")
        
        handler = FTPHandler
        handler.authorizer = authorizer
        handler.banner = "AstrBot文件服务器 - FTP服务"
        
        server = ThreadedFTPServer(("0.0.0.0", self.ftp_port), handler)
        self.server_instances["ftp"] = server
        server.serve_forever()

    def run_webdav_server(self):
        """启动多线程WebDAV服务"""
        root_dir = self.default_root or next(iter(self.get_system_roots().values()))
        provider = FilesystemProvider(root_dir)
        
        dav_config = {
            "provider_mapping": {"/": provider},
            "user_mapping": {},
            "verbose": 0,
            "host": "0.0.0.0",
            "port": self.webdav_port,
            "server": "cheroot",
            "cheroot_config": {"numthreads": 10}
        }
        
        app = WsgiDAVApp(dav_config)
        app.run()

    def start_servers(self):
        """自动启动所有服务器线程"""
        # HTTP服务线程
        self.server_threads["http"] = threading.Thread(target=self.run_http_server, daemon=True)
        # FTP服务线程
        self.server_threads["ftp"] = threading.Thread(target=self.run_ftp_server, daemon=True)
        # WebDAV服务线程
        self.server_threads["webdav"] = threading.Thread(target=self.run_webdav_server, daemon=True)
        
        # 启动所有线程
        for t in self.server_threads.values():
            t.start()
