"""日语错题本：生词 / 语法错题拍照录入与学习督促。

分层约定（依赖方向单一，禁止反向 import）：
    ui      -> services -> storage
    server  -> services -> storage
    services / ui / server -> config
本包根目录只放入口与配置，不写业务逻辑。
"""