# 香港行程规划

[English](README.md)

HK-Trip-Planning-Agent 是 **CSIT 6000V Advanced AI Computing and Systems** 的课程项目。

Streamlit 接收抵离日期、人数、预算与偏好。控制机通过 tcloud 提交 TACC 作业。STORM 检索与排序资料，Qwen 生成行程，Python 校验时间、交通及预算。网页和 Markdown 报告支持简体中文与英文。

## 项目结构

[部署架构图和行程时序图](ARCHITECTURE.zh.md)

| 文件或目录 | 职责 |
| --- | --- |
| [`trip/`](trip/) | Linux 控制端。[`ui.py`](trip/ui.py) 提供 Streamlit 网页，[`controller.py`](trip/controller.py) 启动 API、调度行程并调用 `sources.py` 获取官方资料，`store.py` 管理行程版本。 |
| [`app.py`](app.py)、[`orchestrator.py`](orchestrator.py)、[`coallocated.py`](coallocated.py)、[`gateway_accounting.py`](gateway_accounting.py) | 通用 HTTP API 与 SQLite 任务存储、tcloud 命令封装、同一 allocation 内的任务投递，以及通过 SSH 独立核对 Slurm 记账。 |
| [`combined/`](combined/) 与 [`service/`](service/) | TACC 上的 supervisor 负责在同一个 Slurm allocation 中启动 Qwen 和行程 CPU step。`service/` 提供模型启动脚本与 Slurm 记账采集。 |
| [`workflow/`](workflow/) | [`trip_job.py`](workflow/trip_job.py) 是行程 CPU step 的执行入口，调用 STORM 和 Qwen，校验行程并写出结果。`trip_schedule.py` 校验时间、交通与预算，`trip_report.py` 生成 Markdown，`storm-src/` 随仓库提供固定版本的 STORM 源码。 |
| [`install.sh`](install.sh)、[`trip/install.py`](trip/install.py)、[`trip/deploy.py`](trip/deploy.py)、[`.env.example`](.env.example) | Linux 安装入口、systemd 发布逻辑、可选的 SSH 发布工具和本机配置模板。 |
| [`tests/`](tests/) | API 与调度回归测试，包含双语行程测试使用的合成样本。`trip/`、`combined/` 和 `workflow/` 也各有模块测试。 |

请求从 Streamlit 进入 Linux 控制端，再由 tcloud 投递到 TACC。CPU step 完成后，控制端核对 Slurm 终态与产物清单，将报告存入安装目录的 `data/artifacts/`，并在 `data/state.sqlite3` 中记录任务状态。浏览器可以从控制端重新打开已保存的行程。

## Linux 部署

在使用 systemd 的 Linux 控制机上准备 Git、Python 3.8 或更新版本及其 `venv` 模块，并安装课程 tcloud。供服务运行的 Linux 用户需要可用的 `~/.tcloud/.userconfig`、同一配置指向的私钥，以及经可信渠道核对后写入 `~/.ssh/known_hosts` 的课程网关主机密钥。使用控制端 Python 环境时需要访问依赖下载源。首次使用的 TACC 账号还需能下载安装 Python 客户端依赖和 MiniLM 权重，或在 TACC 提供可读的 wheelhouse 与模型目录。

首次克隆这个私有仓库前，请使用有仓库读取权限的 GitHub 账号完成 Git 身份验证。

```bash
git clone https://github.com/JulyGGGG/HK-Trip-Planning-Agent.git hk-trip-planner
cd hk-trip-planner
cp .env.example .env.local
# 根据控制机修改 .env.local，尤其是服务用户和 tcloud 路径。
sudo ./install.sh
```

默认安装目录是 `/opt/hk-trip-planner`，服务用户为 `root`。网页和 API 默认监听所有 IPv4 网卡。宿主机用 `http://<linux-ip>:8501/` 打开网页，用 `http://<linux-ip>:8766/api/health` 检查 API。只需本机访问时，可将两个绑定地址设为 `127.0.0.1`。能访问 API 的客户端可以提交或取消行程，双端口直连适用于受信任的单用户演示网络。

安装器优先读取 `--config` 指定的文件，其次读取仓库根目录的 `.env.local`，最后读取 `/etc/hk-trip-planner.env`。配置采用 `KEY=value` 格式，可用 `#` 开头的行添加注释。使用默认值即可连接标准课程环境。路径、端口、TACC 网关和 GPU 排除项可以覆盖。网关配置用于通过 SSH 核对记账和读取产物，课程 SDK 使用自己的网关和身份配置。控制端依赖源默认为 PyPI，可通过 `HK_TRIP_PIP_INDEX_URL` 指定镜像。[配置示例](.env.example)

## 升级和检查

在无待执行行程且 GPU allocation 已回收时，从目标 Git 提交运行同一安装命令。安装器使用版本化发布目录并保留 `data/` 中的 SQLite 和报告。启动或健康检查失败时，安装器恢复上次的链接和 systemd 配置。要主动回滚到旧 Git 版本，从对应提交的检出目录使用同一份本机配置重新安装。

```bash
systemctl is-active hk-trip-api hk-trip-ui
journalctl -u hk-trip-api -u hk-trip-ui -n 60 --no-pager
```

可选的 Windows SSH 发布方式使用同一个 Linux 安装器。将 SSH 别名写入本机 `.env.local` 的 `HK_TRIP_SSH_HOST`，并在目标 Linux 主机设置 `/etc/hk-trip-planner.env`。目标账号需要非交互式 sudo 权限。

```powershell
python -B -m trip.deploy
```

## 测试

在仓库根目录执行。安装控制端依赖后，双语测试会读取仓库内的合成样本。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r trip/requirements-control.txt
python -B -m unittest discover -s tests -q
python -B -m unittest discover -s combined -q
python -B -m unittest discover -s workflow -q
python -B -m unittest discover -s trip -q
```

集群执行、API 输入与产物结构见工作流说明。[STORM 工作流](workflow/README.md)
