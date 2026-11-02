"""PyInstaller 打包入口（源码运行时请用 run.bat 或 python -m screen_ocr_alert）。"""
import sys

from screen_ocr_alert.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
