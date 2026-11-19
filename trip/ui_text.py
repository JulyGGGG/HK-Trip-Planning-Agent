"""Bilingual Streamlit labels and messages from the existing run API."""

import re

from workflow.trip_contract import PHASES
from workflow.trip_presentation import EN, localized


COPY = {
    "title": ("香港行程规划", "Hong Kong trip planner"),
    "history": ("行程记录", "Trip history"),
    "new_trip": ("新建行程", "New trip"),
    "find_run": ("查看已有行程", "Find a saved trip"),
    "pick_run": ("选择行程", "Select a trip"),
    "open_run": ("打开记录", "Open trip"),
    "form_hint": ("时间为香港当地时间。预算包括住宿、交通、活动及餐饮。",
                  "Times are in Hong Kong local time. Include lodging, transport, activities and meals in your budget."),
    "arrival_day": ("抵港日期", "Arrival date"),
    "arrival_clock": ("抵港时间", "Arrival time"),
    "departure_day": ("离港日期", "Departure date"),
    "departure_clock": ("离港时间", "Departure time"),
    "gateway": ("抵离地点", "Arrival and departure point"),
    "party_size": ("同行人数", "Travelers"),
    "budget_hkd": ("总预算（HKD）", "Total budget (HKD)"),
    "interests": ("兴趣", "Interests"),
    "interests_example": ("例如：艺术、博物馆、自然", "e.g. art, museums, nature"),
    "must_visit": ("必去地点", "Must-visit places"),
    "must_visit_example": ("例如：香港藝術館", "e.g. Hong Kong Museum of Art"),
    "walking": ("步行偏好", "Walking preference"),
    "normal": ("一般", "Standard"),
    "low": ("尽量少走路", "Less walking"),
    "generate": ("生成行程", "Generate itinerary"),
    "version": ("第 {version} 版", "Version {version}"),
    "cancel": ("取消此任务", "Cancel this run"),
    "retry": ("重试当前版本", "Retry this version"),
    "revise": ("修改行程", "Revise itinerary"),
    "feedback": ("额外调整要求", "Additional changes"),
    "feedback_example": ("例如：第二天希望改成室内活动", "e.g. plan indoor activities for day two"),
    "submit_revision": ("生成新版本", "Generate new version"),
    "download": ("下载行程报告", "Download itinerary report"),
    "artifact_pending": ("执行产物暂不可用，请稍后刷新。", "Run artifacts are temporarily unavailable. Refresh shortly."),
    "api_unavailable": ("后台 API 暂时不可用，请刷新或重试同一次提交。",
                        "The API is temporarily unavailable. Refresh or retry the same submission."),
    "invalid_response": ("后台响应无效，请稍后刷新。", "The API returned an invalid response. Refresh shortly."),
    "response_too_large": ("响应超出大小限制。", "The API response exceeded the size limit."),
    "request_failed": ("请求失败（HTTP {status}）", "Request failed (HTTP {status})."),
    "repair_round": ("根据校验结果修订行程（第 {round} 轮）", "Revising the itinerary after validation (round {round})"),
    "execution_issue": ("执行遇到问题。", "The run encountered a problem."),
    "execution_details": ("执行详情（原文）", "Execution details (original)"),
}

STATUS = {
    "ACCEPTED": ("已受理", "Accepted"),
    "PREPARING": ("准备提交", "Preparing to submit"),
    "WAITING_MODEL": ("等候模型与计算资源", "Waiting for the model and compute resources"),
    "QUEUED": ("等候 CPU step", "Waiting for the CPU step"),
    "RUNNING": ("执行中", "Running"),
    "FINALIZING": ("正在核对产物和 Slurm 记账", "Verifying artifacts and Slurm accounting"),
    "SUBMITTING": ("提交中", "Submitting"),
    "SUCCEEDED": ("已完成", "Completed"),
    "FAILED": ("失败", "Failed"),
    "CANCELLED": ("已取消", "Cancelled"),
    "CANCELLING": ("取消中", "Cancelling"),
    "TIMED_OUT": ("执行超时", "Timed out"),
    "UNKNOWN": ("等待确认集群状态", "Awaiting cluster confirmation"),
}

PHASES_EN = {
    "WAITING": "Waiting for compute resources",
    "INITIALIZING": "Preparing STORM",
    "RESEARCHING": "Researching official sources",
    "PLANNING": "Building the itinerary",
    "CHECKING": "Checking times and budget",
    "REPAIRING": "Revising the draft",
    "COMPLETED": "Itinerary saved",
    "FAILED": "Execution failed",
}

PROGRESS = {
    "已保存任务，等待后台处理": "Run saved. Waiting for the controller.",
    "新执行尝试已保存": "New attempt saved.",
    "正在核对模型 allocation 的提交记录": "Checking submission records for the model allocation.",
    "等待共享 allocation 内的 CPU step": "Waiting for a CPU step in the shared allocation.",
    "按交通、住宿和活动三个方向检索官方资料": "Searching official sources for transport, lodging and activities.",
    "生成结构化行程": "Generating a structured itinerary.",
    "核对开放时间、转乘和港币预算": "Checking opening hours, transfers and the HKD budget.",
    "行程和来源已保存": "Itinerary and sources saved.",
}

API_ERRORS_EN = {
    "行程资料包含无效字段": "The trip contains invalid fields.",
    "请选择跨 2–4 个香港日历日的行程": "Choose a trip spanning 2–4 Hong Kong calendar days.",
    "人数须为 1–30 的整数": "Enter a whole number of travelers from 1 to 30.",
    "预算须为港币数字，最多两位小数": "Enter an HKD budget with up to two decimal places.",
    "预算须大于零，最多 HK$1,000,000 和两位小数": "Enter a budget from HK$0.01 to HK$1,000,000 with up to two decimal places.",
    "步行偏好或抵离地点无效": "Choose a valid walking preference and arrival/departure point.",
    "提交须包含 workflow=hk_trip 和 trip": "The request must include workflow=hk_trip and trip.",
    "修订须包含 changes 或 feedback": "The revision must include trip changes or feedback.",
    "修订包含无效字段": "The revision contains invalid fields.",
    "请提供要修改的资料或修订要求": "Change at least one trip detail or add a revision request.",
    "行程资料没有变化，请填写修订要求": "Enter a revision request when the trip details stay the same.",
    "请等待当前版本执行结束": "Wait for the current version to finish.",
    "已有后续行程版本，请从最新版本修订": "A newer version is available. Revise the latest version.",
    "执行尚未确认结束；不能重复提交": "Wait for the current attempt to finish before retrying.",
    "必须提供有效的 Idempotency-Key 请求头": "The request needs a valid Idempotency-Key header.",
    "相同幂等键对应不同请求": "This submission key already belongs to different trip details. Open the saved trip or start a new submission.",
    "Host 不受信任": "The API host is not trusted.",
    "不接受跨站请求": "The API rejected a cross-origin request.",
    "请求类型必须为 application/json": "Send the request as application/json.",
    "请求体为空或过大": "The request body is empty or too large.",
    "此接口请求体应为空对象": "This endpoint expects an empty object.",
    "任务或产物不存在": "The run or artifact could not be found.",
    "任务不存在": "The run could not be found.",
    "未找到": "The resource could not be found.",
    "产物尚不可用或校验未通过": "The run artifacts are unavailable or have not passed verification.",
}

FIELD_NAMES = {
    "抵港时间": "Arrival time", "离港时间": "Departure time", "兴趣": "Interests",
    "必去地点": "Must-visit places", "修订要求": "Revision request",
}
FIELD_RULES_EN = {
    "无效": "{field} is invalid.",
    "须为香港时间，格式 YYYY-MM-DDTHH:MM": "Use Hong Kong local time for {field} in YYYY-MM-DDTHH:MM format.",
    "须为最多 8 项的列表": "{field} can have up to 8 entries.",
}

EXECUTION_ERRORS = {
    "Qwen 未返回可解析的行程": ("Qwen 未返回可解析的行程。", "Qwen did not return a readable itinerary."),
    "官方资料未返回可用的交通、住宿或景点证据": (
        "此次执行未收到可用的官方资料。", "This run did not receive usable official evidence."),
    "官方资料未返回可选活动": ("官方资料未返回可选活动。", "Official sources did not return available activities."),
    "CPU step failed": ("CPU step 执行失败。", "The CPU step failed."),
    "Execution ended before creating a step": ("执行在创建 CPU step 前结束。", "The run ended before creating a CPU step."),
    "CPU execution ended; awaiting independent Slurm step accounting": (
        "CPU step 已结束，正在等待独立 Slurm 记账。", "The CPU step ended. Waiting for independent Slurm accounting."),
    "Bound allocation ended before validated workflow completion; retry creates a new attempt": (
        "所属 allocation 在行程通过验证前结束。重试会创建新的执行尝试。",
        "The allocation ended before verification completed. Retry to create a new attempt."),
    "Cancellation marker upload failed; retrying": ("取消请求上传失败，正在重试。", "Retrying delivery of the cancellation request."),
}


def ui_text(key, language, **values):
    return localized(COPY[key], language).format(**values)


def status_text(status, language):
    return localized(STATUS[status], language) if status in STATUS else status


def phase_text(phase, language):
    return (PHASES_EN if language == EN else PHASES).get(phase, phase or "")


def progress_text(message, language):
    if message == "Preparing the STORM client":
        return localized(("正在准备 STORM 客户端", message), language)
    if message in PROGRESS:
        return message if language != EN else PROGRESS[message]
    if match := re.fullmatch(r"根据校验结果修订行程（第 (\d+) 轮）", message or ""):
        return ui_text("repair_round", language, round=match[1])
    return ""


def api_error(message, status, language):
    if message and language != EN:
        return message
    if message in API_ERRORS_EN:
        return API_ERRORS_EN[message]
    for prefix, field in FIELD_NAMES.items():
        if message.startswith(prefix):
            rule = message[len(prefix):]
            if rule in FIELD_RULES_EN:
                return FIELD_RULES_EN[rule].format(field=field)
            if match := re.fullmatch(r"须为不超过 (\d+) 字的文本", rule):
                return f"{field} must be text of at most {match[1]} characters."
    return ui_text("request_failed", language, status=status)


def execution_error(detail, language):
    core = re.sub(r"^[A-Za-z]+(?:Error|Exception)\s*[:·]\s+", "", detail)
    if core in EXECUTION_ERRORS:
        return localized(EXECUTION_ERRORS[core], language), False
    return ui_text("execution_issue", language), True
