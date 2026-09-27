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


settings = Settings()
