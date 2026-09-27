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


settings = Settings()
