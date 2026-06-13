#!/bin/bash
# 服务器【数据】更新:下载公共题库种子 Release → 导入 → 重建向量 → 重启服务
# 代码更新仍走 deploy.sh(git pull);本脚本只管数据,二者独立。
#
# 用法(在服务器 xingce_api 目录):
#   bash update_data.sh seed-20260613    # 指定 Release tag
#   bash update_data.sh                  # 不带 tag = 取 latest
set -e
cd "$(dirname "$0")"
TAG="${1:-}"

echo ">> 下载种子包 (Release: ${TAG:-latest})"
if command -v gh >/dev/null 2>&1; then
  if [ -n "$TAG" ]; then
    gh release download "$TAG" -p 'public_seed_*.zip' --clobber
  else
    gh release download -p 'public_seed_*.zip' --clobber
  fi
else
  echo "   未装 gh CLI。请手动 wget Release 资源直链到当前目录,例如:"
  echo "   wget https://github.com/15192416063/xingce-api/releases/download/${TAG:-<tag>}/public_seed_<date>.zip"
  echo "   下载好后重跑本脚本(它会跳过下载、直接解压导入)。"
  ls public_seed_*.zip >/dev/null 2>&1 || { echo "当前目录没有 public_seed_*.zip,退出。"; exit 1; }
fi

echo ">> 解压(用 Python zipfile,免装 unzip)"
rm -rf seed
EXTRACT_PY=$(command -v python3 || command -v python)
"$EXTRACT_PY" -c "import zipfile,glob; zipfile.ZipFile(sorted(glob.glob('public_seed_*.zip'))[-1]).extractall('seed')"
if [ ! -f seed/public_seed.db ]; then
  echo "解压失败:seed/public_seed.db 不存在"; exit 1
fi

echo ">> 激活环境(若有 venv)"
[ -d venv ] && source venv/bin/activate || true
PY=$(command -v python || command -v python3)
echo ">> 使用 Python: $PY"

echo ">> 导入公共题(自动去重,可重复跑)"
"$PY" import_public.py seed

echo ">> 用本机 embedding 重建全部向量"
"$PY" reembed.py

echo ">> 重启服务"
sudo systemctl restart xingce

echo ">> 完成。状态:"
sudo systemctl status xingce --no-pager -l | head -5
