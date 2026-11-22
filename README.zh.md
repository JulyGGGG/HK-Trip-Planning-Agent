# 香港行程规划

[English](README.md)

HK-Trip-Planning-Agent 是 **CSIT 6000V Advanced AI Computing and Systems** 的课程项目。

Streamlit 接收抵离日期、人数、预算与偏好。控制机通过 tcloud 提交 TACC 作业。STORM 对官方资料排序，Qwen 生成行程，Python 校验时间、交通及预算。网页和 Markdown 报告支持简体中文与英文。

```
浏览器 ──► Streamlit (8501) ──► 行程控制端 (8766) ──tcloud──► TACC Slurm allocation
                                      │                        ├── Qwen 27B（vLLM，2 GPU）
                                      │                        └── 行程 CPU step（STORM + 校验）
                                      └── 官方资料桥接 ────────► 港铁 / 康文署 / 1823 开放数据
```

## 项目结构

[部署架构图和行程时序图](ARCHITECTURE.zh.md)

| 文件或目录 | 职责 |
| --- | --- |
| [`trip/`](trip/) | Linux 控制端。[`ui.py`](trip/ui.py) 提供 Streamlit 网页，[`controller.py`](trip/controller.py) 启动 API、调度行程并调用 [`sources.py`](trip/sources.py) 获取官方资料，[`store.py`](trip/store.py) 管理行程版本。 |
| [`app.py`](app.py)、[`orchestrator.py`](orchestrator.py)、[`coallocated.py`](coallocated.py)、[`gateway_accounting.py`](gateway_accounting.py) | 通用 HTTP API 与 SQLite 任务存储、tcloud 命令封装、同一 allocation 内的任务投递，以及通过 SSH 独立核对 Slurm 记账。 |
| [`combined/`](combined/) 与 [`service/`](service/) | TACC 上的 supervisor 负责在同一个 Slurm allocation 中启动 Qwen 和行程 CPU step。`service/` 提供模型启动脚本与 Slurm 记账采集。 |
| [`workflow/`](workflow/) | [`trip_job.py`](workflow/trip_job.py) 是行程 CPU step 的执行入口，调用 STORM 和 Qwen，校验行程并写出结果。[`trip_schedule.py`](workflow/trip_schedule.py) 校验时间、交通与预算，[`trip_report.py`](workflow/trip_report.py) 生成 Markdown，[`storm-src/`](workflow/storm-src/) 随仓库提供固定版本的 STORM 源码。 |
| [`eval/`](eval/) | 评测用例、在线评测脚本与离线指标测试。见下方「详细说明」。 |
| [`install.sh`](install.sh)、[`trip/install.py`](trip/install.py)、[`trip/deploy.py`](trip/deploy.py)、[`.env.example`](.env.example) | Linux 安装入口、systemd 发布逻辑、可选的 SSH 发布工具和本机配置模板。 |
| [`tests/`](tests/) | API 与调度回归测试，包含双语行程测试使用的合成样本。`trip/`、`combined/`、`workflow/` 和 `eval/` 也各有模块测试。 |

请求从 Streamlit 进入 Linux 控制端，再由 tcloud 投递到 TACC。CPU step 完成后，控制端核对 Slurm 终态与产物清单，将报告存入安装目录的 `data/artifacts/`，并在 `data/state.sqlite3` 中记录任务状态。浏览器可以从控制端重新打开已保存的行程。

## 快速开始

### 前置条件

| 要求 | 说明 |
| --- | --- |
| Linux 控制机 | Ubuntu 22.04/24.04 或 WSL2。控制端使用 `fcntl` 与 POSIX 进程组。 |
| Python 3.8+ 及 `venv` | `python3 -m venv` 可用。 |
| 课程 `tcloud` 客户端（SDK v0.4.1） | 在 `PATH` 中，或把 `TCLOUD_BIN` 设为绝对路径。 |
| 具备 CSIT6000V 权限的 TACC 账号 | 运行时位于 `/mnt/home/public/course-envs/CSIT6000V/`。 |
| 已登记到 TACC 的 SSH 私钥 | `tcloud config -u USER -f /path/to/private_key`；控制端也用它在网关上只读核对记账。 |
| 控制机可访问外网 HTTPS | 依赖下载，以及港铁开放数据、康文署场馆页、1823 假期、旅舍官网。 |
| TACC `USERDIR` 空间 | 实测约 1.2 GB：862 MB 私有 STORM 虚拟环境、88 MB MiniLM 快照、184 MB pip 缓存。 |
| Git 与 `rsync` | `tcloud submit` 用 rsync 同步暂存目录。 |

控制机不需要 GPU，GPU 只由 TACC allocation 申请。

### 1. 安装并配置 tcloud

```bash
unzip linux-amd64-v0.4.1.zip -d ~/tcloud-sdk
chmod +x ~/tcloud-sdk/tcloud
export PATH="$HOME/tcloud-sdk:$PATH"

tcloud config -u 你的TACC用户名 -f /path/to/private_key
tcloud init
tcloud env ls
```

### 2. 注册课程公共 conda 路径

`service/job.sh` 要求 `CONDA_PREFIX` 指向课程共享环境，而不是个人副本。每个账号注册一次：

```bash
cat > ~/.condarc <<'EOF'
envs_dirs:
  - /mnt/home/public/course-envs/CSIT6000V/common
EOF
tcloud upload ~/.condarc ../.condarc
```

用一个一分钟的小作业打印 `$CONDA_PREFIX` 验证，结果应以 `/CSIT6000V/common/proxy` 结尾。

### 3. 预置 CPU 语义检索模型

行程 step 在 CPU 上用 `paraphrase-MiniLM-L6-v2` 做语义检索。计算节点连不上 `hf-mirror.com`，因此预置一次：

```bash
python -B prepare_embedding.py                      # 生成 data/embedding-model（92 MB）
tcloud upload data/embedding-model agent-envs/storm-e2e-v1/embedding-model
```

### 4. 在 TACC 上构建私有 STORM 环境

```bash
cd workflow
cp tuxiv-setup.conf tuxiv.conf
tcloud submit
tcloud ps -j <作业号>
tcloud cat "slurm_log/storm-env-setup-<作业号>.log" | tail -40
```

成功的标志是出现 `STORM_ENV_READY=/mnt/home/<用户>/USERDIR/agent-envs/storm-e2e-v1` 并写出 `ready.json`。该步骤把 68 个锁定版本的包装入嵌套 venv，复用只读的课程 `torch`，并真实校验 DSPy 导入与 CPU 语义检索。

如果被分区 60 分钟上限中断，直接重新提交：venv 会保留，pip 会跳过已满足的包。

### 5. 准备控制端

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r trip/requirements-control.txt
```

`sudo ./install.sh` 会把同一份代码装到 `/opt/hk-trip-planner` 并注册两个 systemd 单元。下面的手工启动方式等价，也是本次验证使用的方式。

### 6. 启动控制端与网页

```bash
export PYTHONPATH="$PWD"
export TACC_PROJECT_SESSION=hk-trip-planner
export TACC_PROJECT_RUNS=hk-trip-planner/runs
export TCLOUD_BIN="$HOME/tcloud-sdk/tcloud"
export TACC_GATEWAY_HOST=gw.turing.ust.hk
export TACC_GATEWAY_PORT=30041
export TACC_GATEWAY_USERDIR=/mnt/home/你的TACC用户名/USERDIR
export HK_TRIP_API_BIND=127.0.0.1 HK_TRIP_API_PORT=8766

nohup .venv/bin/python -B -m trip.controller --data-dir "$PWD/data" --port 8766 \
      > data/controller.log 2>&1 &

STREAMLIT_SERVER_ADDRESS=127.0.0.1 STREAMLIT_SERVER_PORT=8501 \
  nohup .venv/bin/python -m streamlit run trip/ui.py --server.headless=true \
      > data/ui.log 2>&1 &

curl -s localhost:8766/api/health
# {"ok": true, "mode": "hk-trip", "time": "..."}
```

浏览器打开 `http://localhost:8501/` 即可使用表单，也可直接调用 API。

### 7. 端到端示例

```bash
curl -s -X POST http://127.0.0.1:8766/api/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-example-1' \
  -d '{"workflow":"hk_trip","trip":{
        "arrival":"2026-12-04T09:00","departure":"2026-12-06T20:00",
        "party_size":2,"budget_hkd":"3500.00",
        "interests":["艺术","博物馆"],"must_visit":["香港藝術館"],
        "walking":"normal","gateway":"airport"}}'
```

预期返回 `202`，包含 `run_id`、`attempt_id` 和 `status_url`。随后轮询：

```bash
curl -s http://127.0.0.1:8766/api/runs/<RUN_ID>
```

成功运行的阶段序列：

```
WAITING_MODEL → QUEUED → RUNNING（INITIALIZING → RESEARCHING → PLANNING → CHECKING → REPAIRING）→ SUCCEEDED
```

首次提交还会启动 GPU allocation，因此在进入 `RUNNING` 前通常要等约 20 分钟的模型冷启动（2026-11-22 在 2×RTX 3090 上实测）。该示例的行程 step 本身耗时 125 秒。

产物列表：

```bash
curl -s http://127.0.0.1:8766/api/runs/<RUN_ID>/artifacts
curl -s http://127.0.0.1:8766/api/runs/<RUN_ID>/artifacts/0
```

完成态应包含：`report.md`、`itinerary.json`、`evidence.json`、`trace.jsonl`、`metrics.json`、`diagnostics.json`、`result.json`（含逐文件 SHA-256 与整包摘要）。

上述示例的实测值：

```
status         SUCCEEDED
plan_status    INSUFFICIENT_EVIDENCE（住宿价格未公开；无硬约束冲突）
conflicts      0
model_calls    3（全部成功），repair_rounds 1
tokens         1,876 prompt + 544 completion
workflow       120.3 秒，step 125 秒，峰值内存 1.1 GB
evidence       抓取 70 条官方记录，报告引用 24 条
```

### 客户端截图

`docs/screenshots/` 存放项目报告使用的界面截图：

| 文件 | 内容 |
| --- | --- |
| `01-input-form.png` | 行程表单：日期、抵离时间、抵离地点、人数、预算、兴趣、必去地点、步行偏好 |
| `02-itinerary.png` | 按天行程：时间、换乘与估算标记 |
| `03-budget-summary.png` | 预算总览：上限、已计及估算小计、晚数 |
| `04-budget-items.png` | 逐项预算：数量、金额、依据 |
| `05-sources.png` | 官方来源：原文摘录与抓取时间 |
| `06-conflict-case.png` | 不可行案例：1 港币预算对 988.60 港币已计支出，附冲突横幅 |
| `07-conflict-budget.png` | 同一案例的逐项预算 |
| `08-itinerary-en.png` | 同一行程的英文界面 |

## 详细说明

### 仓库测试

以下四组测试不需要 TACC，也不需要模型：

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r trip/requirements-control.txt

python -B -m unittest discover -s tests -q      # 86 项：API、幂等、调度、记账
python -B -m unittest discover -s combined -q   #  8 项：supervisor 生命周期
python -B -m unittest discover -s workflow -q   #  2 项：工作流契约
python -B -m unittest discover -s trip -q       # 17 项：控制端、安装器、双语界面
python -B -m unittest discover -s eval -q       # 11 项：用例文件与指标判定
```

`trip/test_trip_ui.py` 使用 Streamlit 的 `AppTest`，单次运行有 3 秒超时；文件系统较慢时可能有一项抖动，单独重跑该组即可确认。

`eval/test_eval.py` 是评测的离线部分：它校验 `eval/cases.json`，再对 `eval/fixtures/sample_run/`（一次真实运行的记录）执行指标与判定逻辑，无需消耗 GPU。

### 评测用例

`eval/cases.json` 共 20 条，覆盖 proposal 中的六类条件：

| 类别 | 用例 | 预期判定及其依据 |
| --- | --- | --- |
| `ordinary` | A01–A04 | `conflicts == []`。时间窗宽、预算充足，必去场馆在至少一天开放。 |
| `tight_budget` | B01–B04 | 必须出现 `over_budget`。预算低于餐饮预留（代码常量：每人每餐 50 HKD）加公开车费。 |
| `required_place_unavailable` | C01–C03 | 出现 `required_missing` 或开放时间冲突。必去馆在当天闭馆，且开放时游客已离开（艺术馆、科学馆周四闭；历史馆周二闭）。 |
| `restricted_window` | D01–D03 | 仅观测。深夜、凌晨、跨零点窗口没有确定性预期，判定记为 `skip`。 |
| `missing_information` | E01–E03 | `unknowns >= 1`。选择住宿必然记录 `room_terms` 或 `lodging_missing`，因为房价未公开。 |
| `infeasible` | F01–F03 | `over_budget`。1/50/120 HKD 的预算连餐饮预留都不够。 |

用例的**输入**是编写的，**判定**不是：每条预期都可由公开事实或代码常量推出，并在 `note` 中写明依据。兴趣匹配、步行舒适度等主观维度**不做自动评分**，留待人工复核。

### 重新运行评测

控制端与 TACC allocation 就绪后：

```bash
. .venv/bin/activate

python -B -m eval.run_eval --dry-run                                   # 仅校验用例，不需 GPU
python -B -m eval.run_eval --cases eval/cases.json --out eval/results  # 完整 20 条
python -B -m eval.run_eval --only A01,B01,F01                          # 子集
python -B -m eval.run_eval --category ordinary
python -B -m eval.run_eval --limit 6
```

产出位于 `--out` 目录：

| 文件 | 内容 |
| --- | --- |
| `results.jsonl` | 每条用例一条记录：运行状态、方案状态、冲突类型、首轮与最终冲突数、unknowns、预算、引用覆盖率、来源数、token、耗时、判定 |
| `results.csv` | 同上，表格形式，可直接放进报告 |
| `summary.json` | `pass_rate`、`conflict_free_cases`、`cases_repaired`、`plan_status_counts`、token/耗时/引用覆盖率均值、分类别统计 |

耗时：模型热时每条约 2 分钟，20 条约 25–40 分钟（含一次冷启动）。控制端在 allocation 运行 45 分钟后停止接收新 step，且同时最多 2 个 CPU step，因此完整套件可能跨两个 allocation。

### 实测结果

20 条用例在同一个课程 Qwen 实例上跑完。`eval/runs.json` 记录了每条用例对应的运行号，因此不必再消耗 GPU 时间即可重建下表：

```bash
python -B -m eval.replay --runs eval/runs.json --out eval/results
```

该次评测的产物已提交在 `eval/recorded/`（`summary.json`、`results.csv`、`results.jsonl`）。

| 类别 | 用例 | 计分 | 通过 | 实际表现 |
| --- | --- | --- | --- | --- |
| `ordinary` | 4 | 4 | 4 | 零硬性冲突；各有一条未知项（住宿条款） |
| `tight_budget` | 4 | 4 | 4 | 正确报出 `over_budget` |
| `required_place_unavailable` | 3 | 3 | 3 | 报出必去点或时间冲突 |
| `restricted_window` | 3 | – | – | 仅观测，不设断言 |
| `missing_information` | 3 | 3 | 3 | `unknowns >= 1` |
| `infeasible` | 3 | 3 | 3 | 正确报出 `over_budget` |

`summary.json` 的汇总：

```
用例 20 · 计分 17 · 通过 17 · 失败 0 · 仅观测 3
方案状态       INSUFFICIENT_EVIDENCE 8 · CONFLICTS 12
零冲突         8 条
修复成功       3 条（首稿有冲突，最终方案无冲突）
单条成本       4.0 次模型调用 · 2,968 prompt + 616 completion tokens
耗时           流程 147.0 秒 · 端到端 157.6 秒
引用支持       81.5% 的预算项带有来源编号
```

两点说明。有一条时间受限用例在 allocation 第 45 分钟边界被 `SERVICE_DRAINING` 拒绝，随后在新的 allocation 上重跑，表中只计入重跑结果。模型温度固定为 0，但 vLLM 的批处理仍会使具体地点和时段在多次运行间不同；本表衡量的是硬约束判定，而该判定由检查器根据公开事实确定性推导。

### 用例修正记录

* **C02** — 可接受的冲突集合扩充为包含 `day_end` 与 `slot_overlap`。在实测中，模型把必去博物馆排在 10:00–11:00，处于开放时间内，但**已晚于 09:00 的离港时间**，因此检查器用 `day_end`/`slot_overlap` 表达"排不进去"，而不是用例最初列出的开放时间冲突。事实未变，只是修正了测试所接受的表达方式。仓库中的用例文件为修正后的版本。

### 指标与报告对应关系

| 指标（来源） | 定义 | 报告中的呈现 |
| --- | --- | --- |
| `conflicts` / `conflict_kinds`（`itinerary.json`） | 最后一轮修复后的硬约束违规数 | 硬约束通过率（`summary.pass_rate`、`conflict_free_cases`） |
| `first_round_conflicts`（`trace.jsonl` 第 0 轮） | 首稿、未修复前的违规数 | 修复效果对比的输入 |
| `cases_repaired`（`summary.json`） | 首轮有冲突且最终无冲突的用例数 | 修复成功计数 |
| `over_budget` | `trip_schedule.build_plan` 中 `已知 + 估算 > 上限` | 预算算术与超支表 |
| `unknowns`（`itinerary.json`） | 无法核实的关键事实（目前为住宿条款） | 证据不足率，单独报告，不计入成功 |
| `citation_coverage`（`eval/metrics.py`） | 带 `source_ids` 的预算项占比 | 引用支持率 |
| `model_calls`、token、`workflow_seconds`（`metrics.json`） | 单次运行成本 | 成本与延迟表 |
| `sources`、`source_errors`（`evidence.json`） | 抓取成功的官方记录与失败数 | 证据覆盖 |

评测只比较**同一次运行内**的首稿与最终方案，而不是与"关闭修复"的独立系统对照。未实现该消融：请求 schema 是封闭的，没有暴露关闭修复的开关。

### 已知限制与预期波动

* **冷启动占主导。** 新 allocation 需要约 20 分钟让 vLLM 加载与编译，随后一条行程约 2 分钟。allocation 处于热态时提交可跳过冷启动。
* **空闲会被回收。** 控制端在无待办任务 180 秒后取消 allocation 以释放 GPU（`trip/controller.py` 的 `IDLE_SECONDS`），下次提交重新支付冷启动。
* **allocation 时长。** 课程分区单作业上限 60 分钟，supervisor 在 45 分钟后停止接收新 step，因此 20 条用例可能跨两个 allocation。
* **并发。** supervisor 同时最多运行 2 个行程 step，其余停留在 `WAITING_MODEL`。
* **住宿价格未公开。** YHA 页面只列房型不给夜价，因此过夜行程通常以 `INSUFFICIENT_EVIDENCE`（含 `room_terms`）结束，而不是 `FEASIBLE_WITH_ESTIMATES`。住宿一项也没有金额，因此**不计入已计小计，预算校验无法验证这笔最大开支**。这是设计行为：系统只报告能核实的内容，不猜测房价。
* **计算节点无法访问 `hf-mirror.com`。** 新账号若不按第 3 步预置 MiniLM 快照（或把 `STORM_EMBEDDING_SOURCE` 指向 TACC 上可读目录），环境搭建会失败。
* **官方页面会变。** 车费、开放时间、闭馆日都是实时抓取。运营方改版后，原本通过的用例可能合理地改变结论——因此每条引用都记录 `fetched_at` 与原文摘录。
* **模型波动。** Qwen 以 `temperature=0` 调用，但 vLLM 的批处理与前缀缓存仍可能造成 token 级差异，具体行程可能不同；硬约束结论应保持稳定，所选地点与时段不保证。
* **私有数据不外泄。** `data/` 与 TACC `USERDIR` 下的产物属于各账号，不进入仓库。

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

见上方「仓库测试」。集群执行、API 输入与产物结构见 [工作流说明](workflow/README.md)。