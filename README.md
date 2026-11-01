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
      library_page.py     # 词库页：筛选 + 翻页 + 编辑/删除入口
      entry_table_model.py   # 表格模型：把一条记录摊成 6 列
      entry_edit_dialog.py   # 编辑对话框（带原图预览）
      entry_fields.py     # 10 个字段的定义，录入页与编辑框共用
      upload_page.py      # 手机上传页：地址、二维码、收到的图、一键送去录入
      checkin_page.py     # 打卡页：连续天数、今日按钮、本月日历
    services/    # 识别、打卡等业务门面
      entry_service.py    # 录入门面：图片落盘、表单校验、保存
      library_service.py  # 词库门面：查询/筛选/翻页/统计/删除
      upload_service.py   # 上传门面：启停服务、本机 IP、收图落盘、收件箱
      checkin_service.py  # 打卡门面：打卡幂等、连续天数、月历网格
      recognizer.py       # 多模态识别
      errors.py           # 统一错误模型
    storage/     # SQLite 读写封装（db.py 建表，repository.py 增删改查）
    prompts/     # 识别提示词模板
    models.py    # 数据模型 Entry（生词与语法共用）
    config.py    # 路径、环境变量、模型名
    main.py      # 桌面端入口
  server/        # 手机局域网上传服务
    upload_server.py   # FastAPI 应用 + 后台线程跑 uvicorn（不含识别、不碰数据库）
    queue.py           # 线程安全收件箱（HTTP 线程放，界面线程取）
  scripts/       # 验证脚本（存储 / 识别 / 录入链路 / 界面 / 手机上传 / 打卡）
  docs/          # venv 权限绕行的记录、待办清单
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

录入之后都在「词库」页里管：

1. 顶部关键词框搜全部字段（词形、读音、释义、例句、错因、备注都能搜到）
2. 类型下拉只看生词或只看语法；勾「按日期筛选」才按创建日期过滤
3. 每页 20 / 50 / 100 条切换，表格按创建时间倒序（最新在最上）
4. 双击某行或点「编辑」打开对话框改，左上角能改类型，右边能看原图
5. 点「删除」（或选中后按 Delete 键）会弹确认框，删掉的是数据库记录，
   原图仍留在 `data/uploads/` 里

手机拍的那张纸上的题，用手机传进来（比电脑端找根数据线快）：

1. 左侧点「手机上传」，页面一进来服务就自己起了，二维码旁边有地址
2. 手机连**同一个 Wi-Fi**，扫码打开页面（或者把地址手敲进手机浏览器）
3. 点页面上的「点这里拍照」，或者「从相册里选一张」，传完会显示"已收到（大小）"
4. 桌面端 1 秒内出现在右边的列表里，点「用这张图去录入」就跳到录入页
5. 用完点「停止服务」，端口立刻释放；不点也行，关窗口时也会自动停

手机打不开页面时按这个顺序查：两台设备在不在同一个 Wi-Fi（不是手机流量）→
Windows 防火墙有没有拦 Python（第一次启动会弹窗，选「允许」）→
`UPLOAD_PORT` 有没有被别的程序占用（换一个再启动）。

手机点了拍照却没反应时，看 `data/logs/upload.log`。每打开一次页面、每次选图、
每次上传结果都会记一行，例如：

```
2026-11-01 20:41:07 GET /ping e=page-load&t=1762000867000
2026-11-01 20:41:22 GET /ping e=pick%202.3%20MB%20image%2Fjpeg&t=1762000882000
2026-11-01 20:41:22 POST /upload name='IMG_20261101_204122.jpg' type='application/octet-stream' bytes=2381234 -> 200 saved 2026-11-01/9f1c....jpg
```

只有 `page-load` 没有 `pick` → 手机的浏览器没把照片交回页面（微信/QQ 内置浏览器常见），
换 Chrome 或走「从相册里选一张」；有 `pick` 没有 `POST` → 页面脚本出错，日志里会出现
`e=js-error ...`，把后面那段错误原文搜一下基本就知道是哪一行；有 `POST ... -> 415/400` →
服务端明确拒了，日志里带着原因。

> 踩过的坑：页面脚本早期写了 `var status = document.getElementById('status')`，
> 撞上浏览器内置的 `window.status`（它是个字符串），于是 `status.style` 变成 undefined，
> 每次选完图第一行就抛异常、上传请求根本没发出去，而电脑端只看得到"没收到"。
> 现在整段脚本包在 IIFE 里、变量叫 `statusBox`，并且有 `scripts/check_upload_page.py`
> 用 Node 跑一遍这几条路径。

传图限制：只收 JPG / PNG / WEBP / BMP / GIF / TIFF，单张上限 15MB。
传 PDF、传超大图、传坏文件都会被挡住，手机页面和桌面端都会给出原因，不会静默失败。
安卓相机常把类型报成 `application/octet-stream`，这类也照收，真是不是图片由 Pillow 判断。

每天学完点一下「打卡」：

1. 左侧点「打卡」，大数字是**已连续天数**，右边一行小字是今天的状态
2. 想留一句"今天学了什么"就填在备注框里，然后点「今日学习完成」
3. 点完按钮会变成「今日已打卡」并且变灰，今天那格在日历上被涂成蓝色
4. 用 `‹` `›` 可以翻上个月下个月看以前的记录，标题里带着当月打卡天数

同一天点多少次都只有一行记录（表上 `date` 有 UNIQUE 约束），也不会覆盖第一次写的备注。
连续天数按这个规则数：今天打了就从今天往前数；今天还没打但昨天打了，就先显示昨天那段
（今天还有机会不断）；昨天也没打就是断了，显示 0。这样第二天早上打开程序看到的数字
不会突然掉到 0。

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
| 4 | 词库页（列表 / 搜索 / 编辑 / 删除） | ✅ 完成 |
| 5 | 手机局域网上传 | ✅ 完成 |
| 6 | 每日打卡 | ✅ 完成 |
| 7 | 托盘提醒 | ⬜ |
| 8 | 收尾（日志 / 测试 / 文档） | ⬜ |

> 想到但暂时不做的功能（框选识别范围、孤儿图片清理、导出等）记在
> `docs/todo.md`，别在这里堆。

## 验收脚本

每个阶段都有对应的脚本，全部不联网、不弹窗，用独立的临时库跑完即删：

```powershell
.\venv\Scripts\python.exe scripts\verify_storage.py       # 阶段 1：建表、增删改查、并发
.\venv\Scripts\python.exe scripts\verify_recognition.py   # 阶段 2：识别解析与各种错误分支
.\venv\Scripts\python.exe scripts\verify_capture.py       # 阶段 3：图片落盘、表单校验、保存
.\venv\Scripts\python.exe scripts\verify_ui.py            # 阶段 3：offscreen 模式跑真实窗口与点击
.\venv\Scripts\python.exe scripts\verify_library.py       # 阶段 4：搜索/筛选/翻页/统计/删除
.\venv\Scripts\python.exe scripts\verify_library_ui.py    # 阶段 4：offscreen 点一遍词库页与编辑框
.\venv\Scripts\python.exe scripts\verify_upload.py        # 阶段 5：真起 HTTP 服务，测收图与各种失败
.\venv\Scripts\python.exe scripts\verify_upload_ui.py     # 阶段 5：offscreen 点一遍手机上传页
.\venv\Scripts\python.exe scripts\check_upload_page.py    # 阶段 5：用 node 跑手机页面里的 JS（缺 node 时跳过）
.\venv\Scripts\python.exe scripts\verify_checkin.py       # 阶段 6：打卡幂等、连续天数、月历网格
.\venv\Scripts\python.exe scripts\verify_checkin_ui.py    # 阶段 6：offscreen 点一遍打卡页
```

最后那条是给手机上传页里那段 JS 用的：它只在手机浏览器里执行，Python 的测试看不见它。
没装 Node 会自动跳过，不影响其它验收。

## 安全约定

- API Key 只存在于 `.env` 与本机进程环境变量中，不写入代码、不提交 Git
- 手机上传页面不注入任何环境变量，也看不到 Key
- `.env`、`*.db`、`data/`、`uploads/`、`venv/`、`__pycache__/` 均在 `.gitignore` 中