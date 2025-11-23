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
    from flask import Flask, render_template_string, request, redirect, url_for, send_file
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
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>AstrBot文件浏览器 - {{ current_path }}</title>
            <style>
                * { box-sizing: border-box; }
                body { 
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    margin: 0; 
                    padding: 20px; 
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                }
                .container {
                    max-width: 1200px;
                    margin: 0 auto;
                    background: rgba(255, 255, 255, 0.95);
                    border-radius: 15px;
                    box-shadow: 0 20px 40px rgba(0,0,0,0.1);
                    overflow: hidden;
                }
                .header {
                    background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
                    color: white;
                    padding: 30px;
                    text-align: center;
                }
                .header h1 {
                    margin: 0;
                    font-size: 2.5em;
                    font-weight: 300;
                }
                .nav { 
                    margin: 20px; 
                    padding: 20px; 
                    background: #f8f9fa; 
                    border-radius: 10px;
                    border: 1px solid #e9ecef;
                }
                .nav-section {
                    margin-bottom: 15px;
                }
                .nav-section:last-child {
                    margin-bottom: 0;
                }
                .nav-section label {
                    font-weight: 600;
                    color: #495057;
                    margin-bottom: 8px;
                    display: block;
                }
                .file-list { 
                    list-style: none; 
                    padding: 0; 
                    margin: 20px;
                }
                .file-list li { 
                    padding: 15px; 
                    border-bottom: 1px solid #e9ecef;
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    transition: all 0.3s ease;
                    border-radius: 8px;
                    margin-bottom: 5px;
                }
                .file-list li:hover {
                    background: #f8f9fa;
                    transform: translateX(5px);
                }
                .file-info {
                    display: flex;
                    align-items: center;
                    flex: 1;
                }
                .file-icon {
                    margin-right: 15px;
                    font-size: 1.5em;
                }
                .dir { 
                    color: #007bff; 
                    font-weight: 600; 
                }
                .file { 
                    color: #495057; 
                }
                .file-actions {
                    display: flex;
                    gap: 10px;
                }
                .btn {
                    padding: 8px 16px;
                    border: none;
                    border-radius: 6px;
                    cursor: pointer;
                    font-size: 14px;
                    transition: all 0.3s ease;
                    text-decoration: none;
                    display: inline-block;
                }
                .btn-primary {
                    background: #007bff;
                    color: white;
                }
                .btn-primary:hover {
                    background: #0056b3;
                }
                .btn-success {
                    background: #28a745;
                    color: white;
                }
                .btn-success:hover {
                    background: #1e7e34;
                }
                .btn-danger {
                    background: #dc3545;
                    color: white;
                }
                .btn-danger:hover {
                    background: #c82333;
                }
                .custom-dir { 
                    margin: 20px; 
                    padding: 20px; 
                    background: #e3f2fd;
                    border-radius: 10px;
                    border-left: 4px solid #2196f3;
                }
                .upload-section {
                    margin: 20px;
                    padding: 20px;
                    background: #e8f5e8;
                    border-radius: 10px;
                    border-left: 4px solid #28a745;
                }
                .upload-area {
                    border: 2px dashed #28a745;
                    border-radius: 8px;
                    padding: 30px;
                    text-align: center;
                    transition: all 0.3s ease;
                    cursor: pointer;
                }
                .upload-area:hover {
                    background: rgba(40, 167, 69, 0.1);
                }
                .upload-area.dragover {
                    background: rgba(40, 167, 69, 0.2);
                    border-color: #1e7e34;
                }
                input[type="text"], input[type="file"] {
                    padding: 10px;
                    border: 1px solid #ced4da;
                    border-radius: 6px;
                    font-size: 14px;
                    width: 100%;
                    margin-bottom: 10px;
                }
                input[type="text"]:focus {
                    outline: none;
                    border-color: #007bff;
                    box-shadow: 0 0 0 3px rgba(0,123,255,0.1);
                }
                .path-input-group {
                    display: flex;
                    gap: 10px;
                    align-items: center;
                }
                .path-input-group input {
                    flex: 1;
                    margin-bottom: 0;
                }
                .root-links {
                    display: flex;
                    flex-wrap: wrap;
                    gap: 10px;
                }
                .root-links a {
                    padding: 8px 12px;
                    background: #007bff;
                    color: white;
                    text-decoration: none;
                    border-radius: 6px;
                    font-size: 14px;
                    transition: background 0.3s ease;
                }
                .root-links a:hover {
                    background: #0056b3;
                }
                .current-path {
                    font-family: 'Courier New', monospace;
                    background: #f8f9fa;
                    padding: 10px;
                    border-radius: 6px;
                    border: 1px solid #e9ecef;
                    word-break: break-all;
                }
                .file-size {
                    color: #6c757d;
                    font-size: 14px;
                }
                .progress-bar {
                    width: 100%;
                    height: 4px;
                    background: #e9ecef;
                    border-radius: 2px;
                    overflow: hidden;
                    margin-top: 10px;
                }
                .progress-fill {
                    height: 100%;
                    background: #28a745;
                    width: 0%;
                    transition: width 0.3s ease;
                }
                .upload-list {
                    margin-top: 15px;
                    max-height: 200px;
                    overflow-y: auto;
                }
                .upload-item {
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    padding: 8px;
                    background: white;
                    border-radius: 4px;
                    margin-bottom: 5px;
                }
                .upload-item-name {
                    flex: 1;
                    font-size: 14px;
                }
                .upload-item-size {
                    color: #6c757d;
                    font-size: 12px;
                    margin-left: 10px;
                }
                .upload-item-remove {
                    background: #dc3545;
                    color: white;
                    border: none;
                    padding: 4px 8px;
                    border-radius: 4px;
                    cursor: pointer;
                    font-size: 12px;
                }
                @media (max-width: 768px) {
                    .container {
                        margin: 10px;
                        border-radius: 10px;
                    }
                    .header h1 {
                        font-size: 2em;
                    }
                    .path-input-group {
                        flex-direction: column;
                    }
                    .path-input-group button {
                        width: 100%;
                    }
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>🚀 AstrBot 本地文件服务器</h1>
                    <p>支持文件浏览、上传、下载的多功能文件管理器</p>
                </div>
                
                <div class="custom-dir">
                    <div class="nav-section">
                        <label>📁 自定义目录跳转</label>
                        <form method="POST" style="margin: 0;">
                            <div class="path-input-group">
                                <input type="text" name="custom_dir" placeholder="输入目录路径（如C:\\或/home）" value="{{ current_path }}">
                                <button type="submit" class="btn btn-primary">跳转</button>
                            </div>
                        </form>
                    </div>
                </div>

                <div class="nav">
                    <div class="nav-section">
                        <label>🖥️ 系统根目录</label>
                        <div class="root-links">
                            {% for name, path in system_roots.items() %}
                                <a href="{{ url_for('browse', path=quote(path)) }}">{{ name }}</a>
                            {% endfor %}
                        </div>
                    </div>
                    
                    <div class="nav-section">
                        <label>📍 当前路径</label>
                        <div class="current-path">{{ current_path }}</div>
                    </div>
                </div>

                <div class="upload-section">
                    <label>📤 文件上传</label>
                    <div class="upload-area" id="uploadArea">
                        <p>📁 拖拽文件到此处或点击选择文件</p>
                        <input type="file" id="fileInput" multiple style="display: none;">
                        <button type="button" class="btn btn-success" onclick="document.getElementById('fileInput').click()">选择文件</button>
                        <button type="button" class="btn btn-primary" onclick="uploadFiles()" style="margin-left: 10px;">开始上传</button>
                    </div>
                    <div class="upload-list" id="uploadList"></div>
                    <div class="progress-bar">
                        <div class="progress-fill" id="progressFill"></div>
                    </div>
                </div>

                <ul class="file-list" id="fileList">
                    {% if parent_path %}
                        <li>
                            <div class="file-info">
                                <span class="file-icon">📁</span>
                                <a href="{{ url_for('browse', path=quote(parent_path)) }}" class="dir">../ (上级目录)</a>
                            </div>
                        </li>
                    {% endif %}
                    {% for item in items %}
                        <li>
                            <div class="file-info">
                                <span class="file-icon">{{ '📁' if item.is_dir else '📄' }}</span>
                                {% if item.is_dir %}
                                    <a href="{{ url_for('browse', path=quote(item.path)) }}" class="dir">{{ item.name }}/</a>
                                {% else %}
                                    <span class="file">{{ item.name }}</span>
                                    <span class="file-size">({{ item.size | filesizeformat }})</span>
                                {% endif %}
                            </div>
                            <div class="file-actions">
                                {% if not item.is_dir %}
                                    <a href="{{ url_for('download', path=quote(item.path)) }}" class="btn btn-success">下载</a>
                                {% endif %}
                            </div>
                        </li>
                    {% endfor %}
                </ul>
            </div>

            <script>
                const uploadArea = document.getElementById('uploadArea');
                const fileInput = document.getElementById('fileInput');
                const uploadList = document.getElementById('uploadList');
                const progressFill = document.getElementById('progressFill');
                let selectedFiles = [];

                // 拖拽上传
                uploadArea.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    uploadArea.classList.add('dragover');
                });

                uploadArea.addEventListener('dragleave', () => {
                    uploadArea.classList.remove('dragover');
                });

                uploadArea.addEventListener('drop', (e) => {
                    e.preventDefault();
                    uploadArea.classList.remove('dragover');
                    handleFiles(e.dataTransfer.files);
                });

                fileInput.addEventListener('change', (e) => {
                    handleFiles(e.target.files);
                });

                function handleFiles(files) {
                    for (let file of files) {
                        if (!selectedFiles.find(f => f.name === file.name)) {
                            selectedFiles.push(file);
                        }
                    }
                    updateUploadList();
                }

                function updateUploadList() {
                    uploadList.innerHTML = '';
                    selectedFiles.forEach((file, index) => {
                        const item = document.createElement('div');
                        item.className = 'upload-item';
                        item.innerHTML = `
                            <span class="upload-item-name">${file.name}</span>
                            <span class="upload-item-size">${formatFileSize(file.size)}</span>
                            <button class="upload-item-remove" onclick="removeFile(${index})">删除</button>
                        `;
                        uploadList.appendChild(item);
                    });
                }

                function removeFile(index) {
                    selectedFiles.splice(index, 1);
                    updateUploadList();
                }

                function formatFileSize(bytes) {
                    if (bytes === 0) return '0 Bytes';
                    const k = 1024;
                    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
                    const i = Math.floor(Math.log(bytes) / Math.log(k));
                    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
                }

                async function uploadFiles() {
                    if (selectedFiles.length === 0) {
                        alert('请先选择文件');
                        return;
                    }

                    const currentPath = '{{ current_path }}';
                    let totalUploaded = 0;
                    const totalSize = selectedFiles.reduce((sum, file) => sum + file.size, 0);

                    for (let file of selectedFiles) {
                        const formData = new FormData();
                        formData.append('file', file);
                        formData.append('current_path', currentPath);

                        try {
                            const response = await fetch('/upload', {
                                method: 'POST',
                                body: formData
                            });

                            if (response.ok) {
                                totalUploaded += file.size;
                                progressFill.style.width = (totalUploaded / totalSize * 100) + '%';
                            } else {
                                alert(`上传 ${file.name} 失败`);
                            }
                        } catch (error) {
                            alert(`上传 ${file.name} 时发生错误: ${error.message}`);
                        }
                    }

                    // 上传完成
                    setTimeout(() => {
                        progressFill.style.width = '0%';
                        selectedFiles = [];
                        updateUploadList();
                        fileInput.value = '';
                        // 刷新页面显示新上传的文件
                        window.location.reload();
                    }, 1000);
                }
            </script>
        </body>
        </html>
        """

        # 文件大小格式化过滤器
        @app.template_filter('filesizeformat')
        def filesizeformat(size):
            if size == "-":
                return "-"
            for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                if size < 1024.0:
                    return f"{size:.1f} {unit}"
                size /= 1024.0
            return f"{size:.1f} PB"

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

        @app.route("/upload", methods=["POST"])
        def upload():
            try:
                if 'file' not in request.files:
                    return "没有文件", 400
                
                file = request.files['file']
                current_path = request.form.get('current_path', '')
                
                if file.filename == '':
                    return "没有选择文件", 400
                
                if file:
                    filename = file.filename
                    # 安全处理文件名，防止路径遍历攻击
                    filename = os.path.basename(filename)
                    if not filename:
                        return "无效的文件名", 400
                    
                    save_path = os.path.join(current_path, filename)
                    
                    # 确保保存路径在当前目录或子目录内
                    try:
                        os.makedirs(current_path, exist_ok=True)
                        file.save(save_path)
                        return "上传成功", 200
                    except Exception as e:
                        return f"保存失败: {str(e)}", 500
                        
            except Exception as e:
                return f"上传失败: {str(e)}", 500

        @app.route("/download/<path:path>")
        def download(path):
            try:
                current_path = unquote(path)
                if not os.path.isfile(current_path):
                    return "文件不存在", 404
                
                # 获取文件名和目录
                directory = os.path.dirname(current_path)
                filename = os.path.basename(current_path)
                
                return send_file(current_path, as_attachment=True, download_name=filename)
                
            except Exception as e:
                return f"下载失败: {str(e)}", 500

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
