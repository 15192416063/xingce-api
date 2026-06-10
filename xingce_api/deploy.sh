#!/bin/bash
# 服务器更新脚本:拉最新代码 → 装新依赖 → 重启服务
# 用法(在服务器 xingce_api 目录):  bash deploy.sh
set -e
cd "$(dirname "$0")"

echo ">> 拉取最新代码 (git pull)"
git pull

echo ">> 安装依赖(有新依赖才会装)"
if [ -d venv ]; then source venv/bin/activate; fi
pip install -q -r requirements.txt

echo ">> 重启服务"
sudo systemctl restart xingce

echo ">> 完成。状态:"
sudo systemctl status xingce --no-pager -l | head -5
