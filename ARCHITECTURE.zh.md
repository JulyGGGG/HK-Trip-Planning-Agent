# 香港行程规划架构

[English](ARCHITECTURE.md)

## 部署架构

```mermaid
flowchart TB
    Browser["Windows 宿主机浏览器"]

    subgraph Linux["Linux 控制机"]
        Web["Streamlit 网页<br/>trip/ui.py · 8501"]
        API["HTTP API 与任务调度<br/>trip/controller.py · 8766"]
        Store[("任务状态与报告<br/>SQLite · data/artifacts")]
        SDK["tcloud 命令行工具"]
        Web <-->|"回环 HTTP"| API
        API <-->|"读写"| Store
        API --> SDK
    end

    subgraph TACC["TACC Slurm 集群"]
        Shared[("共享目录 USERDIR<br/>投递、进度、证据、结果")]
        subgraph Allocation["同一 Slurm allocation"]
            Supervisor["作业监督进程<br/>combined/supervisor.py"]
            Qwen["Qwen GPU 推理服务"]
            Trip["单条行程 CPU step<br/>workflow/trip_job.py · STORM"]
            Supervisor -->|"启动"| Qwen
            Supervisor -->|"调度"| Trip
            Trip <-->|"查询与行程草稿"| Qwen
        end
        Shared -->|"读取投递"| Supervisor
        Trip <-->|"检索交换与产物"| Shared
    end

    Sources["香港官方资料<br/>场馆、交通、住宿与假期"]
    Browser -->|"访问虚拟机 IP"| Web
    SDK -->|"启动或回收"| Supervisor
    SDK <-->|"投递与回读"| Shared
    API <-->|"抓取资料"| Sources
```

Windows 浏览器访问 Linux 控制机的 Streamlit 页面。控制机负责网页、API、任务持久化和资料抓取。TACC 在同一项 Slurm 资源分配中运行 Qwen 服务和各行程的 CPU step。

## 单次行程

```mermaid
sequenceDiagram
    actor User as 用户
    participant UI as Streamlit
    participant API as Linux 控制端
    participant Files as TACC 共享目录
    participant Job as 行程 CPU step
    participant Model as Qwen GPU 服务

    User->>UI: 填写日期、人数、预算与偏好
    UI->>API: POST /api/runs
    API-->>UI: 202 Accepted + run_id
    Note over API: 控制端复用或启动 Slurm allocation
    API->>Files: 上传请求和模型绑定，最后写入 dispatch.json
    Note over Files,Job: 作业监督进程读取投递并启动 CPU step
    Job->>Model: 生成检索查询
    Job->>Files: 写入检索请求
    API->>Files: 读取请求，抓取官方资料后写回证据
    Files-->>Job: 提供证据
    Note over Job: STORM 排序检索证据
    Job->>Model: 生成结构化行程草稿
    Model-->>Job: 返回草稿
    Note over Job: Python 校验时间、交通与预算
    Job->>Files: 写出进度、报告、证据及 SHA-256 清单
    API->>Files: 拉取产物并核对清单
    Note over API: 核对执行收据与 Slurm 终态，保存任务及报告
    UI->>API: 查询进度和结果
    API-->>UI: 返回行程与下载内容
    UI-->>User: 按所选语言展示行程和报告
```

API 受理请求后立即返回任务 ID。CPU step 通过共享目录向控制端请求官方资料，用 STORM 对证据排序，再调用 Qwen 生成行程草稿。控制端核对返回的产物和 Slurm 终态后，将任务标为完成。

[中文 README](README.zh.md) · [工作流与 API 细节](workflow/README.md)
