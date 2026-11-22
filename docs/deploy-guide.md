# 部署教程：从零部署到云服务器

按这份教程，从买服务器到同事能在浏览器里打开，大约 30 分钟。全程在你自己电脑的终端里操作。

部署完成后，服务器上会运行三个容器：

| 容器 | 作用 |
|---|---|
| `recruit-ai` | 招聘助手应用（FastAPI），负责企微机器人长连接、看板、同步、定时推送 |
| `mysql` | MySQL 8.4，保存候选人和变更流水 |
| `caddy` | 对外入口，监听 80 / 443 端口，转发给应用；用域名时自动申请 HTTPS 证书 |

---

## 0. 选服务器

| 项目 | 建议 |
|---|---|
| 配置 | **2 核 2G 起步**即可运行；同一台机器还要跑别的项目时选 2 核 4G 以上 |
| 系统镜像 | **Ubuntu 24.04 LTS**（教程按它写） |
| 地域 | 用 DeepSeek、通义、混元等**国内模型**时选国内地域（北京、上海、广州等），离 HR 近、数据不出境；要调用海外模型时选香港或新加坡 |
| 时长 | 国内地域如果以后要用域名访问，需要 ICP 备案，而备案一般要求服务器剩余时长 **≥ 3 个月** |

腾讯云、阿里云的轻量应用服务器都可以。下单时：
- 登录方式选「自定义密码」并记住密码（下一步只用一次）。
- 不要勾选「自动续费」之前先看清续费价。

> **关于访问方式**：国内地域的服务器，**没有备案的域名是打不开的**（会被云厂商拦截）。所以在备案完成前，用服务器的 **IP 地址**访问（`http://服务器IP`）。备案后再按第 9 节换成域名 + HTTPS。香港等境外地域可以直接用免费域名 `<IP>.sslip.io` 开启 HTTPS。

---

## 1. 在本机生成部署密钥，并装到服务器上

部署脚本默认使用 `~/.ssh/recruit_deploy` 这把密钥。在你电脑的终端执行：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/recruit_deploy -N ""
```

把公钥装到服务器上（腾讯云 Ubuntu 镜像的默认用户是 `ubuntu`，阿里云一般是 `root`）。会提示输入一次服务器密码，**输入时屏幕上不显示字符是正常的**：

```bash
ssh-copy-id -i ~/.ssh/recruit_deploy.pub ubuntu@服务器IP
```

看到 `Number of key(s) added: 1` 就成功了。之后登录不再需要密码：

```bash
ssh -i ~/.ssh/recruit_deploy ubuntu@服务器IP
```

---

## 2. 初始化服务器（只做一次）

登录服务器后，逐条执行：

```bash
sudo timedatectl set-timezone Asia/Shanghai
```

```bash
sudo apt update && sudo apt upgrade -y
```

```bash
sudo apt install -y docker.io docker-compose-v2 git curl
```

**配置 Docker 镜像加速**（国内服务器必须做，否则拉不下 MySQL、Caddy 等镜像），同时限制容器日志大小，防止日志写满磁盘。下面是腾讯云的内网加速地址，阿里云用户请在「容器镜像服务 → 镜像加速器」里复制自己的地址替换：

```bash
echo '{"registry-mirrors":["https://mirror.ccs.tencentyun.com"],"log-driver":"json-file","log-opts":{"max-size":"50m","max-file":"3"}}' | sudo tee /etc/docker/daemon.json
```

```bash
sudo systemctl enable --now docker && sudo systemctl restart docker && sudo usermod -aG docker $USER
```

执行 `exit` 退出，再重新登录一次，让 Docker 权限生效。然后测试：

```bash
docker run --rm hello-world
```

看到 `Hello from Docker!` 就说明装好了。

**防火墙**：在云厂商控制台的「防火墙 / 安全组」里确认放行了 **22、80、443** 端口。**不要**放行 3306（MySQL）和 8000，它们只在服务器内部使用。

---

## 3. 在本机准备配置文件 `.env`

在项目目录下：

```bash
cp .env.example .env
```

用编辑器打开 `.env`，按需填写。各项的获取方法见 [企微 / 腾讯文档接入手册](wecom-setup.md)：

| 配置 | 必填？ | 说明 |
|---|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 建议填 | 不填时使用本地规则引擎，也能跑，但口语理解差很多 |
| `WECOM_AIBOT_ID` / `WECOM_AIBOT_SECRET` | 要在群里用就填 | 企微「智能机器人」，接入方式选**长连接**，不需要域名 |
| `TDOCS_*` | 要同步腾讯文档就填 | 先在本机运行 `python -m scripts.tdocs_setup` 自动建表并写入 |
| `WECOM_USER_NAMES` | 可选 | 企微账号显示为中文名，格式 `zhangsan=张三,lisi=李四` |
| `ACCESS_TOKEN`、`MYSQL_PASSWORD` | **不用填** | 部署脚本会在服务器上随机生成，只保存在服务器上 |

> `.env` 里有密钥，已被 `.gitignore` 排除，**不要提交到 Git**。部署脚本会把它复制到服务器，权限设为只有本人可读。

---

## 4. 一键部署

在**本机**的项目目录下执行（把 IP 换成你的服务器）：

```bash
PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple deploy/deploy.sh 服务器IP ubuntu http
```

参数说明：

| 参数 | 含义 |
|---|---|
| `服务器IP` | 服务器公网 IP |
| `ubuntu` | SSH 用户名（阿里云一般用 `root`） |
| `http` | 访问方式：用 `http://IP` 访问。境外地域可以不填，自动使用 `https://<IP>.sslip.io`；已备案的域名直接填域名 |
| `PIP_INDEX_URL` | pip 镜像，国内服务器加上它，安装依赖会快很多 |

脚本会依次：
1. 把代码同步到服务器的 `/opt/recruit-ai`（不包含 `.git`、本地数据和虚拟环境）。
2. 复制本机的 `.env`。
3. 首次部署时，在服务器上随机生成**访问口令**和 **MySQL 密码**，保存在 `/opt/recruit-ai/deploy/server.env`；以后重新部署不会覆盖。
4. 构建镜像，启动应用、MySQL 和 Caddy。

第一次构建大约需要 2～5 分钟。

---

## 5. 访问和分享给同事

查看访问口令：

```bash
ssh -i ~/.ssh/recruit_deploy ubuntu@服务器IP 'grep ACCESS_TOKEN /opt/recruit-ai/deploy/server.env'
```

浏览器打开 `http://服务器IP`，输入口令登录。把**网址和口令**发给同事即可，使用方法见 [使用手册](user-guide.md)。

检查服务是否健康：

```bash
curl http://服务器IP/healthz
```

返回 `{"ok":true}` 说明正常。

> ⚠️ **企微机器人同一时间只能有一个程序连接。** 服务器启动后，**不要再在本机运行这个项目**，否则两边会互相挤掉连接，日报也会推两遍，腾讯文档里也会多出一份数据。

---

## 6. 更新代码

本机改完代码后，**重新执行第 4 节的同一条命令**即可。数据库、访问口令都会保留，服务会自动重启，中断时间只有几秒。

---

## 7. 正式启用：清空演示数据

首次部署会自动生成近 3 周的演示数据，方便熟悉功能，演示数据也会写进腾讯文档总表。正式使用前按下面步骤清空。**这些操作会删除服务器上的全部候选人数据和腾讯文档总表里的全部记录，无法恢复**，有需要先按第 8 节备份。

登录服务器：

```bash
ssh -i ~/.ssh/recruit_deploy ubuntu@服务器IP
```

```bash
cd /opt/recruit-ai
```

清空腾讯文档总表里的全部记录（没有接腾讯文档就跳过这一步）：

```bash
docker exec recruit-recruit-ai-1 python -c "from app import tdocs; print('已删除', tdocs.clear_records(), '条')"
```

关闭演示数据。改成 0 之后，网页上的「重置演示」也会被禁止，防止误删：

```bash
sed -i 's/^SEED_DEMO_DATA=.*/SEED_DEMO_DATA=0/' deploy/server.env
```

停止服务，删除数据库，再重新启动：

```bash
docker compose --env-file deploy/server.env -f deploy/docker-compose.prod.yml down
```

```bash
docker volume rm recruit_mysql_data
```

```bash
docker compose --env-file deploy/server.env -f deploy/docker-compose.prod.yml up -d
```

---

## 8. 日常运维

以下命令都在服务器的 `/opt/recruit-ai` 目录下执行。先设一个别名，后面的命令会短很多：

```bash
alias dc='docker compose --env-file deploy/server.env -f deploy/docker-compose.prod.yml'
```

| 要做什么 | 命令 |
|---|---|
| 查看运行状态 | `dc ps` |
| 查看应用日志（实时） | `dc logs -f --tail 100 recruit-ai` |
| 重启应用 | `dc restart recruit-ai` |
| 停止全部服务 | `dc down`（数据保留） |
| 启动全部服务 | `dc up -d` |
| 接入自检（模型 / 企微 / 表格） | `docker exec recruit-recruit-ai-1 python -m scripts.doctor` |
| 数据一致性检查 | `docker exec recruit-recruit-ai-1 python -m scripts.check_db` |
| 查看资源占用 | `docker stats --no-stream` |

**备份数据库**（建议定期执行，并把备份文件下载到本机）：

```bash
docker exec recruit-mysql-1 sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" recruit' > ~/recruit-$(date +%F).sql
```

**从备份恢复**：

```bash
docker exec -i recruit-mysql-1 sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" recruit' < ~/recruit-备份日期.sql
```

服务器重启后，三个容器会自动启动（`restart: unless-stopped`），不需要手动处理。

---

## 9. 换成域名 + HTTPS

用 IP 访问时是 HTTP，**访问口令会明文传输**。正式给更多人使用时，建议换成域名 + HTTPS：

1. 购买域名，把 **A 记录**解析到服务器 IP。
2. 国内地域的服务器：在云厂商控制台提交 **ICP 备案**，等待通过（一般 1～3 周）。
3. 在本机把第 4 节命令的最后一个参数从 `http` 换成你的域名：

```bash
PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple deploy/deploy.sh 服务器IP ubuntu hr.example.com
```

Caddy 会自动申请并续期 HTTPS 证书，之后用 `https://hr.example.com` 访问。

---

## 10. 常见问题

| 现象 | 原因和解决办法 |
|---|---|
| `ssh-copy-id` 提示 `Permission denied` | 密码输错了。忘了密码就在控制台「重置密码」 |
| 拉镜像时卡住或超时 | 没配置 Docker 镜像加速，按第 2 节配置后重启 Docker |
| 构建时 `pip install` 很慢或失败 | 部署命令前面加上 `PIP_INDEX_URL=...` |
| 浏览器打不开 `http://IP` | 检查控制台防火墙是否放行 80 端口；在服务器上执行 `dc ps` 看 caddy 是否在运行 |
| 用域名打开显示「网站未备案」 | 国内地域的域名必须先备案，备案前请用 IP 访问 |
| 日志里提示机器人被挤下线 | 还有别的地方（比如本机）在用同一个机器人，关掉另一边。程序会暂停 5 分钟后自动重连 |
| 腾讯文档里出现重复记录 | 两个环境（比如本机和服务器）都往同一张表写了数据。先只保留服务器这一个环境，然后执行下面的「以服务器为准重写表格」 |
| 页面打开了但数据不对 / 按钮没反应 | 浏览器强制刷新（Mac：`Cmd + Shift + R`） |
| 磁盘越来越满 | `docker system prune` 清理旧镜像；检查是否配置了第 2 节的日志大小限制 |

**以服务器为准重写腾讯文档表格**：清空表格里的全部记录和服务器上的「候选人 ↔ 表格行」对应关系，再重启应用；应用启动时发现对应关系为空，会把服务器上的全部候选人重新写一遍。表格里原有的记录会被删除，无法恢复。

```bash
docker exec recruit-recruit-ai-1 python -c "from app import tdocs; from app.db import db; print('已删除', tdocs.clear_records(), '条'); db.exec('DELETE FROM tdocs_map')"
```

```bash
docker restart recruit-recruit-ai-1
```
