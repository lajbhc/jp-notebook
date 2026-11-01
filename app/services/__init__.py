"""业务层：识别、录入、打卡等逻辑，向上给 ui / server 提供门面。

本层不得 import PySide6 或 fastapi，也不得直接写 SQL（通过 storage 层）。
"""