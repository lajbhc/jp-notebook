# venv 引导说明（Windows + Python 3.13 权限绕行）

## 为什么需要这一步

Python 3.13 起，Windows 上 `os.mkdir(path, mode)` 会**真正应用权限**。
标准库 `tempfile.mkdtemp()` 内部实现是：

```python
_os.mkdir(file, 0o700)
```

`0o700` 会为新目录写入一个「仅所有者」的 DACL。如果解释器运行在**受限令牌**
（restricted token）下，该令牌不在这个 DACL 的允许列表里，结果就是：

- 临时目录刚创建就**不可访问**（`PermissionError: [Errno 13]` / `WinError 5`）
- `python -m venv` 的 `ensurepip` 第一步就失败：
  `PermissionError: ... \Temp\xxx\pip-25.2-py3-none-any.whl`
- 后续 `pip`、`pytest`、FastAPI 的 `UploadFile`（依赖 `SpooledTemporaryFile`）全部连带失败

## 绕行方案

核心思路：在 venv 里放一个 `sitecustomize.py`，把 `tempfile.mkdtemp` 换成
**不传 mode** 的等价实现（不传 mode 时目录继承父目录 ACL，正常可访问）。

因为 `sitecustomize.py` 由 `site` 模块在解释器启动时自动 import，所以 venv 里
**所有** Python 进程（pip、pytest、桌面端、FastAPI）都会自动生效。

> `venv/` 在 `.gitignore` 中，这个补丁不会进入 Git 仓库。

## 完整步骤

```powershell
cd D:\dshwork\jp-notebook

# 1) 跳过 pip 引导，先建 venv
python -m venv --without-pip venv

# 2) 写入 sitecustomize.py（内容见下方）
#    保存到：venv\Lib\site-packages\sitecustomize.py

# 3) 引导 pip
.\venv\Scripts\python.exe -m ensurepip --upgrade --default-pip

# 4) 验证 tempfile 是否已修复
.\venv\Scripts\python.exe -c "import tempfile,os; d=tempfile.mkdtemp(); open(os.path.join(d,'t'),'w').write('ok'); print('tempfile OK:', d)"

# 5) 安装依赖
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## `venv\Lib\site-packages\sitecustomize.py` 内容

```python
"""修复 Python 3.13 在 Windows 上 tempfile.mkdtemp 的 0o700 权限问题。"""
import os
import tempfile as _tf


def _mkdtemp_compat(suffix=None, prefix=None, dir=None):
    prefix, suffix, dir, output_type = _tf._sanitize_params(prefix, suffix, dir)
    if dir is None:
        dir = _tf.gettempdir()
    names = _tf._get_candidate_names()
    if output_type is bytes:
        names = map(os.fsencode, names)
    for _ in range(_tf.TMP_MAX):
        name = next(names)
        path = os.path.join(dir, prefix + name + suffix)
        try:
            os.mkdir(path)          # 关键：不传 0o700
        except FileExistsError:
            continue
        return path
    raise FileExistsError("no usable temporary directory found")


_tf.mkdtemp = _mkdtemp_compat
```

## 验证记录

| 检查 | 结果 |
|---|---|
| `os.mkdir(p)` 后写入 | OK |
| `os.mkdir(p, 0o700)` 后写入 | **PermissionError**（问题复现） |
| `tempfile.mkdtemp()` 后写入 | **PermissionError**（问题复现） |
| 注入 sitecustomize 后 `mkdtemp()` | OK |
| 注入后 `ensurepip` | 装上 pip 25.2 |
| 注入后 `pip download requests` | 成功 |

## 影响范围提醒

- 只影响**本机这套受限令牌环境**下的开发；打包分发给普通用户不会遇到（普通用户
  的进程令牌不受限，`0o700` 目录自己可读）。
- 如果把项目交给别人跑，别人通常不需要 `sitecustomize` 这一步；先直接试
  `python -m venv venv`，失败再按本文绕行。