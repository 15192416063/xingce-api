# 上云部署指南(低成本)

改用 API embedding 后,本项目很轻(无 torch),**2GB 内存的小机器即可**。

## 一、买台云服务器(国内推荐)
- **腾讯云 / 阿里云 轻量应用服务器**,选 **2GB 内存、Ubuntu 22.04**。
  - 价格:新用户/学生常有 ¥99/年 左右;正常约 ¥50–80/月。
- 安全组放行端口:**22(SSH)、80、443**(以及临时调试用的 8000)。

## 二、准备三个 key(都在 .env 里)
1. `DEEPSEEK_API_KEY` —— platform.deepseek.com(切题/分类/对话)
2. `XC_EMBED_KEY` —— **siliconflow.cn 注册,免费额度**(向量,托管 bge 模型)
3. `XC_SECRET_KEY` —— 自己随便打一长串随机字符(令牌签名)

## 三、部署(SSH 登录服务器后)
```bash
# 1) 装基础环境
sudo apt update && sudo apt install -y python3-pip python3-venv git fonts-noto-cjk

# 2) 拉代码(假设你已推到 GitHub)
git clone https://github.com/你的用户名/你的仓库.git
cd 你的仓库/xingce_api

# 3) 装依赖(很快,没有 torch 了)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 4) 配置 .env
cp .env.example .env
nano .env          # 填上面三个 key;XC_BETA_FREE=true

# 5)(可选)把本地的题库/向量传上来,或重新入库
#    若带了 xingce_demo.db:先重算向量(换了embedding模型)
python reembed.py

# 6) 起服务(先前台测试)
uvicorn main:app --host 0.0.0.0 --port 8000
# 浏览器开 http://服务器IP:8000 验证能注册登录
```

## 四、长期运行(后台 + 开机自启)
用 systemd:
```bash
sudo nano /etc/systemd/system/xingce.service
```
```ini
[Unit]
Description=Xingce
After=network.target
[Service]
WorkingDirectory=/root/你的仓库/xingce_api
ExecStart=/root/你的仓库/xingce_api/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now xingce
sudo systemctl status xingce      # 看是否 running
```

## 五、域名 + HTTPS(可选,但正式上线建议)
- 域名解析到服务器 IP(国内服务器需 **ICP 备案**)。
- 用 Caddy 自动 HTTPS(最省心):
```bash
sudo apt install -y caddy
sudo nano /etc/caddy/Caddyfile
```
```
你的域名.com {
    reverse_proxy 127.0.0.1:8000
}
```
```bash
sudo systemctl restart caddy
```
没域名也行:直接用 `http://服务器IP:8000` 发给内测用户(浏览器会提示不安全,内测可接受)。

## 六、上线后必看
- **第一个注册的账号 = 管理员**,进「题库管理」传真题、发资讯。
- AI 每日上限默认 50 次/人(`XC_AI_DAILY_CAP`),防滥用焊死成本。
- 盯 DeepSeek / 硅基流动 余额。
- Docker 用户:仓库根 `docker build -f xingce_api/Dockerfile -t xingce .` 也行(已配 Linux 中文字体)。

## 成本小结(2GB 轻量机)
| 项 | 费用 |
|---|---|
| 服务器 | ~¥50–80/月(或学生 ¥99/年) |
| DeepSeek + embedding | 按量,有每日上限,通常每月几元~几十元 |
| 域名 | ~¥30–50/年(可选) |
