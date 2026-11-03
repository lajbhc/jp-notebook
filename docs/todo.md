# 待办（先记下来，等主体功能跑顺了再做）

放这里的都是**已经想清楚、但刻意往后放**的事。不放这里的东西就是真没做。

## 识别

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
- [ ] 字段截断的兜底提示：目前 `error_reason`、`note` 偶尔被模型抄一半就停，
      界面可以在该字段旁标注"图片里似乎还有下半句"，提示手动补
      - 只做本地启发式（引号括号没闭合 / 结句助词收尾 / 长度够却没有句末标点），
        **不再调第二次 API**，也**不拦保存**，只在字段旁点一个琥珀色小标 + 顶部汇总
      - 只对句子型字段生效：`example` / `example_cn` / `error_reason` / `note`
      - 落点：`app/services/truncation.py`（纯函数）+ `capture_page.py` + `entry_edit_dialog.py`
      - 依据：同一张图三组对照（关思考 / 开思考 max=4000 / 加"别抄一半"提示词）都停在
        同样 25 个字，说明是模型对这段日语批注的固有行为，调参治不了

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

## 工程

- [x] GitHub 远程仓库 + push（2026-11-03 完成，https://github.com/lajbhc/jp-notebook）
  - 本机推送要设 `GIT_SSH=C:/Windows/System32/OpenSSH/ssh.exe` + `GIT_SSH_VARIANT=ssh`，
    Git 自带的 MSYS ssh 在这个环境里起不来（`couldn't create signal pipe`）
  - 仓库目前是 public，想改私有要在 Settings → Danger zone → Change visibility
- [ ] CI（GitHub Actions 跑 pytest 版本那套验收脚本）