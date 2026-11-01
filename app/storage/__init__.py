"""存储层：SQLite 连接与读写封装。

对外只暴露仓库（Repository）方法与数据模型，不向上层泄漏 sqlite3 细节。
"""