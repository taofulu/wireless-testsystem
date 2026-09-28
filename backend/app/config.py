"""运行期配置。所有环境相关取值集中于此（参见工程约定：阈值/配置集中）。"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 随仓附带的演示目录（五类 kind 各至少一条 + fake 命令字典）
_DEFAULT_CATALOG_DIR = str(Path(__file__).resolve().parent / "catalog_data")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WTS_", env_file=None)

    # 默认指向本地 PostgreSQL；测试环境通过 WTS_DATABASE_URL 覆盖为 sqlite
    database_url: str = "postgresql+psycopg2://wts:wts@localhost:5432/wts"
    # 操作目录文件所在目录（需含 operations.json）；生产由 AW 团队供给路径
    catalog_dir: str = _DEFAULT_CATALOG_DIR

    # GLM CLI 接入（ADR-0006）：可执行文件路径、skill、单次调用超时秒数。
    # 本地 GLM 5.2 仅提供 CLI；延迟不可控，故调用全部异步化、超时即任务失败。
    glm_cli_path: str = "glm-cli"
    glm_elaboration_skill: str = "elaboration-skill"
    glm_mapping_skill: str = "mapping-skill"
    glm_timeout_seconds: float = 60.0

    # 执行域（T8）：Worker 心跳超时摘除秒数；Worker 崩溃/断网后任务重领窗口
    worker_heartbeat_timeout_seconds: float = 300.0

    # 沙盒调试（T9/T10）：同一可执行用例版本保留的最近调试会话次数
    # （ADR-0009 / spec Further Notes：debug_run 保留次数 N 为配置项，初值 20）
    debug_run_keep_latest: int = 20

    # LASS 环境校验（T11）：环境中台校验 API 基地址与超时。空串即未配置——
    # 校验未发生不产生任何环境结论（execute/recheck 拒绝并提示，不默认放行）
    lass_api_url: str = ""
    lass_timeout_seconds: float = 10.0

    # 场景文件供给（T12）：MBB 场景库 API 未定（外部依赖 E3）的本地降级——
    # Worker 凭 scenario_id+version 从本目录拉取文件（GET …/file 提供）
    scenario_files_dir: str = str(Path(_DEFAULT_CATALOG_DIR) / "scenario_files")


settings = Settings()
