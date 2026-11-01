"""测试包。

有 `__init__.py` 才能用 `from .conftest import make_entry` 这种相对导入——
pytest 会把测试模块当成包里的子模块来收集。
"""