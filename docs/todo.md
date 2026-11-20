# 待办（先记下来，等主体功能跑顺了再做）

放这里的都是**已经想清楚、但刻意往后放**的事。不放这里的东西就是真没做。

## 识别

- [ ] **识别后自动补全**（用户 2026-11-20 提出，方案已定，做到第 1 步）
  学生拍的只是题册上的一个词/一条语法，光有词没法复习。识别只抄图上有的，
  另起一步纯文本调用把空字段补上：读音 / 释义 / 词性 / 接续 / 例句 / 例句翻译。
  - 原则：只补空字段（已有内容一个字不动）；补完把字段名记进 `generated_fields`；
    本地检查不过就丢弃（例句必须用上这个词/这个接续、读音纯假名、接续含 ＋、
    词性收敛到 `models.PART_OF_SPEECH_OPTIONS`）；补全失败不影响识别结果入库
  - 为什么分两次调用：混在一次里模型会把补的写进"照抄"的字段，来源就分不清了；
    分开还让"重新补全"不用重发图片
  - 第 1 步 ✅ `app/prompts/enrich.py` + `app/services/enricher.py`（`enrich/apply/parse_payload`）
    + `scripts/verify_enrich.py`（67 项）+ `scripts/try_enrich.py`（手动试真实效果）
    + `tests/test_enricher.py`（11 项）；实测只给「取り組み」1.4s 补全 5 个字段
  - 第 2 步 ✅ 服务层接线：`entry_service.recognize_and_enrich()`（识别完自动补）与
    `enrich_entry()`（给已有记录补）；补全失败只把一句话放进 `RecognitionResult.enrich_error`，
    识别结果照常返回；`normalize()` 会清掉"用户已经删空的字段"的 AI 标记，切成语法时也会丢掉
    生词专属标记。验收：`verify_enrich.py` 81 项、`tests/test_entry_service.py` 新增 5 项
  - 第 3 步 ✅ `app/storage/db.py` 的 `SCHEMA_VERSION` 1→2 与 `_MIGRATIONS` 迁移表：
    `_migrate_1_to_2()` 用 `ALTER TABLE entries ADD COLUMN generated_fields` 就地加列
    （不重建表、不动旧数据，老记录升级后就是"没有 AI 补的内容"）；repository 的
    `_WRITABLE_COLUMNS` 与 `_row_to_entry()` 一并读写这一列，但它不参与关键词搜索。
    验收：`verify_storage.py` 63 项（含"造一个 v1 库再升级"六步）、`tests/test_storage.py`
    新增迁移与往返 2 项
  - 第 4 步 ✅：录入页给 AI 补的字段打浅蓝底（`#eef4fb`）+ 标签后加「· AI 补」+
    表单上方汇总"有 N 个字段是 AI 补的"；右上「识别后自动补全空缺字段」开关
    默认开、状态存 `ui.json` 的 `capture.auto_enrich`；「重新补全」按钮填好主字段
    后可用。落点：`app/ui/entry_fields.py`（`mark_generated`）、`app/ui/capture_page.py`、
    `app/ui/recognition_worker.py`（`RecognitionWorker` 改调 `recognize_and_enrich`、
    新增 `EnrichWorker`）、`app/ui/window_state.py`（`load_auto_enrich`/`save_auto_enrich`）。
    验收：`verify_ui.py` 60→74 项、`verify_crop_ui.py` 81 项
  - 第 5 步 ✅：编辑框打开时按 `generated_fields` 上色，新增「补全空缺字段」按钮
    （同样走 `EnrichWorker`），补完合并标记再保存，老记录也能用。
    验收：`verify_library_ui.py` 74 项
- [x] **自主框选识别范围**（用户 2026-11-01 提出，2026-11-02 完成）
  在录入页的预览图上按住鼠标拖一个框，只把框里那块送去识别；右键或 Esc 取消，
  「还原整张」回到整图识别，框太小（小于 14 像素）当误触丢掉。
  - 落点：`app/ui/crop_view.py`（新组件）+ `app/services/recognizer.py` 的 `normalize_crop()` /
    `prepare_image(image_path, crop=None)`（先裁再压缩，顺序别反）
  - 数据库不用改，存的原图仍然是整张；`source_image` 语义不变
  - 验收：`scripts/verify_crop.py`（34 项，比对裁出来的像素颜色）、`scripts/verify_crop_ui.py`（81 项）
  - 已知限制：框选状态不跟随图片旋转（本项目不提供旋转）；框选比例不含 EXIF 旋转前的坐标，
    由服务层在 `exif_transpose` 之后再裁，所以手机竖拍也不会错位
- [x] **放大框选**（用户 2026-11-03 提出，当天完成）
  小预览只有 300x220，字密的语法笔记框不准。双击图片（或点「放大框选」）把图铺到接近
  整屏再拖，点「用这块识别」把框选结果带回小预览。落点：`app/ui/crop_dialog.py`。
- [x] **一页连录**（用户 2026-11-03 提出，当天完成）
  保存成功后图片留在预览里、框选和表单清空，接着拖下一个框就能录下一条；同一张图只复制
  一次进 `uploads`，几条记录共用同一个 `source_image`。顺带把校验挪到复制图片之前，
  填错了不再留下孤儿图。落点：`app/ui/capture_page.py` + `app/services/entry_service.py`。
- [ ] 一页多题时批量录入 —— **已放弃**（用户 2026-11-03：多条一起录入容易出错，
      反而增加人工修正负担，不如一条条来。这条留着当记录，不再做）
- [ ] 字段截断的兜底提示：偶尔有字段收在半句上，界面可以在该字段旁标注
      "图片里似乎还有下半句"，提示手动补
      - 只做本地启发式（引号括号没闭合 / 结句助词收尾 / 长度够却没有句末标点），
        **不再调第二次 API**，也**不拦保存**，只在字段旁点一个琥珀色小标 + 顶部汇总
      - 只对句子型字段生效：`example` / `example_cn` / `error_reason` / `note`
      - 落点：`app/services/truncation.py`（纯函数）+ `capture_page.py` + `entry_edit_dialog.py`
      - 依据（2026-11-17 更正）：原以为"模型固有截断"，用"只转写"的探针一验，转写也停在
        同样位置——真实原因是我的测试卡片画布只有 900px、那句日文宽 1064px，右边被裁掉了。
        改成折行后整句 33 字一次抄全。所以这条提示的定位降级为**兜底**：
        真实照片上确实出现过 `example` 停在「…どのように」这种半句，但更多时候是图上就
        看不到那么多。提示只说"可能"，别当成 bug 修复

## 词库与数据

- [ ] 孤儿图片清理：删记录时只删库行、保留原图（有意为之），需要一个"扫一遍没被引用的图"的工具
- [ ] 导出 CSV / Anki（简历项目里常被问，但用户明确说先不做）

## 界面

- [x] **整体排版四步**（用户 2026-11-03 说"按4步来吧"，当天完成）
  1. 统一底座：`app/ui/style.py` 收掉全站字号/间距/横幅/主按钮；窗口最小 960x640、
     默认 1000x720，大小位置记进 `data/ui.json`（`app/ui/window_state.py`）
  2. 录入页：预览与表单换成可拖的 `QSplitter`（预览不再写死 300x220），
     读音与词性并排一行，按钮统一加高 30px，保存按钮改成蓝底主按钮
  3. 词库 + 打卡：六列列宽可拖且写进 `ui.json`（宽度不够时由释义列补齐）；
     打卡页从竖排改成左日历右操作，下面不再空出 285px
  4. 上传页：地址用等宽字体、按钮加高、统一 16px 边距；三个 UI 脚本补几何断言
  - 验收：`verify_ui.py` 60 项（含窗口底座 8 项、拉伸后预览变宽）、`verify_library_ui.py` 74 项
    （含列宽记忆 6 项）、`verify_checkin_ui.py` 57 项（含两栏排版 5 项）、`verify_upload_ui.py` 39 项
- [x] 词库表格列宽记忆（并入上面的第 3 步；`ui.json` 里 `library_columns`）
- [ ] 词库双击列头自适应宽度
- [ ] 录入页识别后高亮"哪些字段是模型填的、哪些是空的"，方便人工校正
      —— 这件事已经并入上面「识别后自动补全」的第 4 步（浅蓝底 +「AI 补」小标）

## 工程

- [x] GitHub 远程仓库 + push（2026-11-03 完成，https://github.com/lajbhc/jp-notebook）
  - 本机推送要设 `GIT_SSH=C:/Windows/System32/OpenSSH/ssh.exe` + `GIT_SSH_VARIANT=ssh`，
    Git 自带的 MSYS ssh 在这个环境里起不来（`couldn't create signal pipe`）
  - 仓库目前是 public，想改私有要在 Settings → Danger zone → Change visibility
- [ ] CI（GitHub Actions 跑 pytest 版本那套验收脚本）