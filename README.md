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
      main_window.py      # 主窗口：左侧导航 + 页面容器
      capture_page.py     # 录入页：选图/预览/识别/校正/保存
      recognition_worker.py  # 把识别放到子线程，避免界面卡死
    services/    # 识别、打卡等业务门面
      entry_service.py    # 录入门面：图片落盘、表单校验、保存
      recognizer.py       # 多模态识别
      errors.py           # 统一错误模型
    storage/     # SQLite 读写封装（db.py 建表，repository.py 增删改查）
    prompts/     # 识别提示词模板
    models.py    # 数据模型 Entry（生词与语法共用）
    config.py    # 路径、环境变量、模型名
    main.py      # 桌面端入口
  server/        # 手机局域网上传服务
  scripts/       # 验证脚本（存储 / 识别 / 录入链路 / 界面）
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

界面上的一条记录这样录入：

1. 左侧点「录入」，选类型（生词 / 语法）
2. 点「选择图片」或把图片拖进预览框
3. 点「开始识别」，等识别结果自动填进表单（识别在子线程跑，界面不会卡）
4. **对照原图逐个字段检查**——模型可能抄漏或抄一半，这一步不能省
5. 点「保存到词库」

识别失败不会丢东西：红色提示条会说明原因，表单照样能手动填完再保存。
保存时原图会被复制进 `data/uploads/<日期>/`，库里只存相对路径。

### 4. 先用探针确认接口能用

换 Key、换模型、换服务商之后，**先跑探针再开界面**，它会把 HTTP 状态码和
服务端返回的原文打出来：

```powershell
# 只用内置小图，验证"地址 + Key + 模型收不收图"
.\venv\Scripts\python.exe scripts\probe_vision.py

# 用真题照片，验证"认不认得出日文"
.\venv\Scripts\python.exe scripts\probe_vision.py 一张真题照片.jpg

# 命令行跑一次完整识别（不写库）
.\venv\Scripts\python.exe scripts\try_recognize.py 照片.jpg --type grammar
```

## 识别相关的约定

- 图片长边超过 1600px 会等比缩小后再发，单张上限 20MB；带透明通道的图会被
  转成白底 JPEG，手机照片会先按 EXIF 校正方向。
- **iPhone 默认的 HEIC 格式 Pillow 打不开**，会提示"图片打不开"。用微信/QQ
  传图，或在手机上先转成 JPG。
- `type`、`source_image`、时间戳一律由本地写入，模型返回的同名字段会被忽略。
- 模型返回的字段走白名单，未知字段丢弃，非字符串值当空处理。
- 所有失败都收敛成 `app/services/errors.py` 里的 `RecognitionError` 子类，
  UI 只读 `user_message`，`detail` 只进日志。
- **思考型模型必须先关思考**。实测端点（百炼 MaaS compatible-mode）默认走思考：
  同一张图 `reasoning_content` 长度 939~3202 字不等，`max_tokens` 被吃光后正文
  是空字符串、`finish_reason=length`。`recognizer.EXTRA_BODY` 里发
  `enable_thinking: False`，稳定 2.3~2.5 秒出结果；若端点不认这个参数返回 400，
  会自动去掉它重试一次。换模型时如果换成不支持该参数的厂商，只会多一次重试。
- 提示词里明确要求"不要猜测"，但模型仍可能把长句抄一半（实测「誤り原因」只抄到
  第 25 字就停，三种参数组合结果完全一致，属于模型固有行为）。这正是
  **识别结果必须人工校正**的原因，不为此调参。

## 配置项

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 是 | 多模态接口密钥，只从环境变量读取 |
| `OPENAI_BASE_URL` | 是 | OpenAI 兼容接口地址，需含 `/v1` |
| `VISION_MODEL` | 是 | 视觉模型名，换模型只改这一行 |
| `UPLOAD_PORT` | 否 | 手机上传服务端口，默认 8765 |
| `REMINDER_TIME` | 否 | 每日提醒时间，默认 21:00 |

> `OPENAI_BASE_URL` 虽然必填，但代码里有官方默认值，所以缺省时会回落到
> `https://api.openai.com/v1`。**这台机器连不上 api.openai.com**（连接超时），
> 实际要换成一个国内可直连、且支持图片输入的兼容端点。

## 开发阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 项目骨架与配置 | ✅ 完成 |
| 1 | 存储层（SQLite） | ✅ 完成 |
| 2 | 识别服务 + 接口探针 | ✅ 完成 |
| 3 | 录入页（上传 → 校正 → 保存） | ✅ 完成 |
| 4 | 词库页（列表 / 搜索 / 编辑 / 删除） | ⬜ |
| 5 | 手机局域网上传 | ⬜ |
| 6 | 每日打卡 | ⬜ |
| 7 | 托盘提醒 | ⬜ |
| 8 | 收尾（日志 / 测试 / 文档） | ⬜ |

## 验收脚本

每个阶段都有对应的脚本，全部不联网、不弹窗，用独立的临时库跑完即删：

```powershell
.\venv\Scripts\python.exe scripts\verify_storage.py       # 阶段 1：建表、增删改查、并发
.\venv\Scripts\python.exe scripts\verify_recognition.py   # 阶段 2：识别解析与各种错误分支
.\venv\Scripts\python.exe scripts\verify_capture.py       # 阶段 3：图片落盘、表单校验、保存
.\venv\Scripts\python.exe scripts\verify_ui.py            # 阶段 3：offscreen 模式跑真实窗口与点击
```

## 安全约定

- API Key 只存在于 `.env` 与本机进程环境变量中，不写入代码、不提交 Git
- 手机上传页面不注入任何环境变量，也看不到 Key
- `.env`、`*.db`、`data/`、`uploads/`、`venv/`、`__pycache__/` 均在 `.gitignore` 中