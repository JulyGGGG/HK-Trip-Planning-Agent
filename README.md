# 香港行程规划 · Hong Kong Trip Planner

Streamlit 接收抵离日期、人数、预算与偏好。控制机通过 tcloud 提交 TACC 作业。STORM 检索与排序资料，Qwen 生成行程，Python 校验时间、交通及预算。网页和 Markdown 报告支持简体中文与英文。

Streamlit collects travel dates, group size, budget, and preferences. The controller submits jobs to TACC through tcloud. STORM ranks source material, Qwen drafts the itinerary, and Python checks timing, transport, and costs. The web app and Markdown reports support Simplified Chinese and English.

## Linux 部署

在使用 systemd 的 Linux 控制机上准备 Git、Python 3.8 或更新版本及其 `venv` 模块，并安装课程 tcloud。供服务运行的 Linux 用户需要可用的 `~/.tcloud/.userconfig`、同一配置指向的私钥，以及经可信渠道核对后写入 `~/.ssh/known_hosts` 的课程网关主机密钥。使用控制端 Python 环境时需要访问依赖下载源。首次使用的 TACC 账号还需能下载安装 Python 客户端依赖和 MiniLM 权重，或在 TACC 提供可读的 wheelhouse 与模型目录。

```bash
git clone <repository-url> hk-trip-planner
cd hk-trip-planner
cp .env.example .env.local
# 根据控制机修改 .env.local，尤其是服务用户和 tcloud 路径。
sudo ./install.sh
```

默认安装目录是 `/opt/hk-trip-planner`，服务用户为 `root`。网页和 API 默认监听所有 IPv4 网卡。宿主机用 `http://<linux-ip>:8501/` 打开网页，用 `http://<linux-ip>:8766/api/health` 检查 API。只需本机访问时，可将两个绑定地址设为 `127.0.0.1`。能访问 API 的客户端可以提交或取消行程，双端口直连适用于受信任的单用户演示网络。

安装器优先读取 `--config` 指定的文件，其次读取仓库根目录的 `.env.local`，最后读取 `/etc/hk-trip-planner.env`。使用默认值即可连接标准课程环境。路径、端口、TACC 网关与 GPU 排除项可以覆盖，配置示例与本机状态均有明确存放位置。[配置示例](.env.example)

## Linux deployment

On a Linux controller running systemd, install Git, Python 3.8 or newer with its `venv` module, and the course tcloud client. The Linux service user needs a working `~/.tcloud/.userconfig`, the private key it references, and a TACC gateway host key verified through a trusted channel in `~/.ssh/known_hosts`. The controller needs access to its Python package index. A fresh TACC account also needs access to the client dependencies and MiniLM weights, or readable wheelhouse and model directories on TACC.

Run the commands above. The installer defaults to `/opt/hk-trip-planner` and the `root` service user. Use `http://<linux-ip>:8501/` for the app and `http://<linux-ip>:8766/api/health` for the API health check. Both listeners default to all IPv4 interfaces. Set both bind values to `127.0.0.1` for local-only access. Clients that can reach the API can submit or cancel trips.

The installer reads an explicit `--config` first, then `.env.local` in the checkout, then `/etc/hk-trip-planner.env`. Settings use literal `KEY=value` lines with optional comment lines beginning with `#`. The optional gateway overrides apply to verified SSH accounting and artifact retrieval. The course SDK uses its own gateway and identity settings. [Configuration example](.env.example)

## 升级和检查 · Updates and checks

在无待执行行程且 GPU allocation 已回收时，从目标 Git 提交运行同一安装命令。安装器使用版本化发布目录并保留 `data/` 中的 SQLite 和报告。启动或健康检查失败时，安装器恢复上次的链接和 systemd 配置。要主动回滚到旧 Git 版本，从对应提交的检出目录使用同一份本机配置重新安装。

Run the same installer from the target Git checkout after pending trips have finished and the GPU allocation has been reclaimed. Releases are versioned. SQLite and reports stay in the installation's `data/` directory. A failed startup or health check restores the previous release and systemd configuration. To roll back to an earlier Git version, run the installer from that checkout with the same local configuration.

```bash
systemctl is-active hk-trip-api hk-trip-ui
journalctl -u hk-trip-api -u hk-trip-ui -n 60 --no-pager
```

可选的 Windows SSH 发布方式使用同一个 Linux 安装器。将 SSH 别名写入本机 `.env.local` 的 `HK_TRIP_SSH_HOST`，并在目标 Linux 主机设置 `/etc/hk-trip-planner.env`。目标账号需要非交互式 sudo 权限。Optional SSH publishing uses the same Linux installer with the target host's local configuration and requires non-interactive sudo on that host.

```powershell
python -B -m trip.deploy
```

## 测试 · Tests

在仓库根目录执行。安装控制端依赖后，双语测试会读取仓库内的合成样本。Run from the repository root after installing the controller dependencies. Bilingual tests use synthetic fixtures included in the repository.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r trip/requirements-control.txt
python -B -m unittest discover -s tests -q
python -B -m unittest discover -s combined -q
python -B -m unittest discover -s workflow -q
python -B -m unittest discover -s trip -q
```

集群执行、API 输入与产物结构见工作流说明。See the workflow guide for cluster execution, API inputs, and artifacts. [STORM workflow](workflow/README.md)
