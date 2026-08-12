#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开箱即用入口（打包 exe 的入口）：
  首次运行/配置缺失 → 设置向导（引导获取 IMAP 授权码）
  配置完成 → 直接进入监听（状态窗口 + 托盘 + 验证码弹窗）
  运行中可“重新配置”，保存后自动继续监听。
"""

import argparse

import gui
import main as engine
import setup_wizard


def main() -> None:
    parser = argparse.ArgumentParser(description="QQ 邮箱验证码工具（开箱即用版）")
    parser.add_argument("--setup", action="store_true", help="强制打开设置向导")
    parser.add_argument("--catchup", action="store_true", help="启动时也处理存量未处理邮件")
    args = parser.parse_args()

    config_path = engine.DEFAULT_CONFIG_PATH

    while True:
        if args.setup or not config_path.exists():
            ok = setup_wizard.run(config_path)
            if not ok:
                return
            args.setup = False

        try:
            cfg = engine.load_config(config_path)
        except SystemExit as exc:
            ok = setup_wizard.run(config_path, error=str(exc))
            if not ok:
                return
            continue

        reason = gui.run_gui(cfg, catchup=args.catchup)
        if reason != "reconfigure":
            return


if __name__ == "__main__":
    main()
