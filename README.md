# 日语错题本（JP Notebook）

本地自用的「日语生词 / 语法错题」拍照录入与学习督促桌面软件。

- **拍照录入**：电脑选图，或手机在同一局域网内扫码拍照上传
- **AI 识别**：调用 OpenAI 兼容的多模态接口，产出结构化字段
- **人工校正**：识别结果一律可编辑，确认后才入库
- **本地存储**：SQLite 单文件，数据不出本机
- **学习督促**：每日打卡 + 桌面托盘提醒

## 技术栈

| 层 | 技术 |
|---|---|
| 桌面端 | Python 3.13 + PySide6 |
| 局域网上传 | FastAPI + uvicorn |
| 存储 | SQLite（标准库 `sqlite3`，无 ORM） |
| 图片处理 | Pillow + base64 |
| 配置 | python-dotenv（密钥只放 `.env`） |
| 二维码 | qrcode |

## 目录结构

```
jp-notebook/
  app/
    ui/          # PySide6 窗口、托盘、提醒小窗
    services/    # 识别、打卡等业务门面
    storage/     # SQLite 读写封装（db.py 建表，repository.py 增删改查）
    prompts/     # 识别提示词模板
    models.py    # 数据模型 Entry（生词与语法共用）
    config.py    # 路径、环境变量、模型名
    main.py      # 桌面端入口
  server/        # 手机局域网上传服务
  scripts/       # 一次性验证脚本（存储、识别探针）
  data/          # 运行时生成：app.db、uploads/、logs/（不入库）
  venv/          # 虚拟环境（不入库）
```

依赖方向单一，禁止反向 import：

```
ui ──► services ──► storage
server ──► services ──► storage
services / ui / server ──► config
```

## 环境准备

> **重要：本项目必须使用 `venv/` 里的解释器运行**，不要用全局 Python。

### 1. 创建虚拟环境（含 Windows 权限绕行）

Python 3.13 起，Windows 上 `os.mkdir(path, mode)` 会真正应用权限；而标准库
`tempfile.mkdtemp()` 内部固定 `os.mkdir(file, 0o700)`，会生成一个「仅所有者」
的 DACL。若解释器运行在受限令牌下，临时目录会一创建就不可访问，导致
`ensurepip` / `pip` / `pytest` / 文件上传全部失败（表现为
`PermissionError: ... pip-*.whl`）。

绕行步骤：

```powershell
cd D:\dshwork\jp-notebook

# 1) 先不引导 pip，避免 ensurepip 直接失败
python -m venv --without-pip venv

# 2) 给 venv 注入 sitecustomize，把 mkdtemp 的 0o700 去掉
#    （内容见 docs/venv-bootstrap.md，venv/ 已在 .gitignore 中，不进仓库）

# 3) 再引导 pip
.\venv\Scripts\python.exe -m ensurepip --upgrade --default-pip

# 4) 安装依赖
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 配置密钥

```powershell
copy .env.example .env
```

编辑 `.env`，至少填写 `OPENAI_API_KEY` 和 `VISION_MODEL`。

### 3. 运行

```powershell
.\venv\Scripts\python.exe -m app.main
```

## 配置项

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 是 | 多模态接口密钥，只从环境变量读取 |
| `OPENAI_BASE_URL` | 是 | OpenAI 兼容接口地址，需含 `/v1` |
| `VISION_MODEL` | 是 | 视觉模型名，换模型只改这一行 |
| `UPLOAD_PORT` | 否 | 手机上传服务端口，默认 8765 |
| `REMINDER_TIME` | 否 | 每日提醒时间，默认 21:00 |

## 开发阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 项目骨架与配置 | ✅ 完成 |
| 1 | 存储层（SQLite） | ✅ 完成 |
| 2 | 识别服务 + 接口探针 | ⬜ |
| 3 | 录入页（上传 → 校正 → 保存） | ⬜ |
| 4 | 词库页（列表 / 搜索 / 编辑 / 删除） | ⬜ |
| 5 | 手机局域网上传 | ⬜ |
| 6 | 每日打卡 | ⬜ |
| 7 | 托盘提醒 | ⬜ |
| 8 | 收尾（日志 / 测试 / 文档） | ⬜ |

## 安全约定

- API Key 只存在于 `.env` 与本机进程环境变量中，不写入代码、不提交 Git
- 手机上传页面不注入任何环境变量，也看不到 Key
- `.env`、`*.db`、`data/`、`uploads/`、`venv/`、`__pycache__/` 均在 `.gitignore` 中