# 第三方组件许可声明

本项目以 PyInstaller 打包分发，二进制中包含下列第三方组件。各组件的著作权归其各自作者所有，
本项目不改变其许可条款。分发时请保留本文件与各组件许可文本。

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| RapidOCR / PP-OCR 模型 | 文字识别 | Apache-2.0 |
| onnxruntime | 推理运行时 | MIT |
| NumPy | 数值计算 | BSD-3-Clause |
| Pillow | 图像读写与缩放 | HPND（Historical Permission Notice and Disclaimer） |
| mss | 屏幕抓取 | MIT |
| psutil | 进程与优先级 | BSD-3-Clause |
| requests | HTTP 客户端 | Apache-2.0 |
| pystray | 托盘图标 | LGPL-3.0 |
| PyInstaller | 打包 | GPL-2.0-or-later，附打包与链接例外（允许分发闭源程序） |
| Python / Tcl / Tk | 运行时与界面 | PSF 许可 / Tcl-Tk 许可 |

说明：

- RapidOCR 与 requests 采用 Apache-2.0：分发时需保留版权与许可声明，并附带其 NOTICE 内容（如有）。
- pystray 采用 LGPL-3.0：本项目以库形式引用，未修改其源码；如需替换版本，可通过重新打包实现。
- PyInstaller 的打包例外允许分发打包后的程序，无需开源本项目源码；但其自身许可文本须随包提供。

完整许可文本可在各组件官方仓库或已安装包内获取。
