"""应用入口。

职责只有三件：准备目录、建/检查数据库、把主窗口开起来。
界面细节在 app/ui/，识别与保存流程在 app/services/。
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from app import config
from app.services import upload_service
from app.storage.db import StorageError, init_db
from app.ui.main_window import WINDOW_TITLE, MainWindow


def main() -> int:
    config.ensure_dirs()

    app = QApplication(sys.argv)
    app.setApplicationName(WINDOW_TITLE)
    # 退出时把上传服务停掉，端口立刻释放，下次启动不会"端口被占"
    app.aboutToQuit.connect(upload_service.stop_server)

    try:
        init_db()
    except StorageError as exc:
        QMessageBox.critical(
            None,
            WINDOW_TITLE,
            f"数据库初始化失败：{exc}\n\n"
            "请检查 data 目录是否可写。如果是旧版本程序建的库，"
            "请先备份 data/app.db 再删除它（程序会重新建一个空库）。",
        )
        return 1

    window = MainWindow(config.get_settings())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())